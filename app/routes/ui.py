import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import jinja2
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key
from app.config import PREVIEW_PRESETS
from app.db import get_db
from app.pipeline.cut import cut
from app.pipeline.intro import generate_intro_clip
from app.pipeline.outro import generate_outro_clip
from app.pipeline.preview import generate_preview
from app.pipeline.publish import publish
from app.pipeline.transcode import PRESET_720P, transcode
from app.storage import StorageBackend, get_storage_backend

_TEMPLATES_DIR = Path(__file__).parent.parent / "ui" / "templates"

# Disable cache to avoid Jinja2 3.1.5+ unhashable cache key issue
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_env)

router = APIRouter(prefix="/studio", tags=["studio"])


def get_ui_client(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> models.Client:
    """Dependency that extracts API Key from Header, Cookie, or Query Param."""
    api_key = (
        request.headers.get("X-API-Key")
        or request.cookies.get("veditor_api_key")
        or request.query_params.get("api_key")
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API Key. Please provide X-API-Key header, veditor_api_key cookie, or api_key query param.",
        )
    hashed_key = hash_api_key(api_key)
    client = (
        db.query(models.Client).filter(models.Client.hashed_key == hashed_key).first()
    )
    if not client:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return client


def get_optional_ui_client(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> models.Client | None:
    """Optional client dependency for public read pages."""
    api_key = (
        request.headers.get("X-API-Key")
        or request.cookies.get("veditor_api_key")
        or request.query_params.get("api_key")
    )
    if not api_key:
        return None
    hashed_key = hash_api_key(api_key)
    return (
        db.query(models.Client).filter(models.Client.hashed_key == hashed_key).first()
    )


ALL_STATUSES = [
    "waiting_for_files",
    "detecting",
    "pending_approval",
    "pending_bounds",
    "cutting",
    "generating_previews",
    "preview",
    "transcoding",
    "uploading",
    "needs_work",
    "done",
    "rejected",
    "broken",
]

MILESTONES_DEF = [
    {
        "num": 1,
        "title": "Ingest & Detect",
        "desc": "Recording ingestion & talk bounds detection",
    },
    {
        "num": 2,
        "title": "Timestamp Review (Gate 1)",
        "desc": "Human verification of speaker In/Out points",
    },
    {
        "num": 3,
        "title": "Processing & Preview (Gate 2)",
        "desc": "Cut, loudness, title slates & low-res preview",
    },
    {
        "num": 4,
        "title": "Transcode & Publish",
        "desc": "Final quality master encode & upload",
    },
]

STAGE_MILESTONE_MAP = {
    "waiting_for_files": 0,
    "detecting": 0,
    "pending_approval": 1,
    "pending_bounds": 1,
    "rejected": 1,
    "cutting": 2,
    "generating_previews": 2,
    "preview": 2,
    "needs_work": 2,
    "transcoding": 3,
    "uploading": 3,
    "done": 4,  # All milestones complete
    "broken": 3,
}


def get_evaluated_milestones(status: str) -> list[dict]:
    current_idx = STAGE_MILESTONE_MAP.get(status, 0)
    result = []
    for idx, m in enumerate(MILESTONES_DEF):
        item = dict(m)
        if status == "done" or current_idx > idx:
            item["state"] = "completed"
        elif current_idx == idx:
            if status in ("rejected", "broken"):
                item["state"] = "failed"
            else:
                item["state"] = "active"
        else:
            item["state"] = "pending"
        result.append(item)
    return result


def _execute_full_processing_pipeline(
    talk: models.Talk,
    storage: StorageBackend,
    db: Session,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> None:
    """Executes the real VEditor pipeline: Intro slate + Outro slate + Cut + Preview."""
    talk_id = talk.id
    event_name = talk.event.name if talk.event else "Open Source Event"
    room_date = (
        f"{talk.room} • {talk.start.strftime('%Y-%m-%d')}"
        if talk.room and talk.start
        else "Main Track"
    )

    # 1. Generate Real Opening Title Slate
    with tempfile.TemporaryDirectory() as tmpdir:
        intro_tmp = Path(tmpdir) / "intro.mp4"
        generate_intro_clip(
            intro_tmp,
            title=talk.title,
            event_name=event_name,
            room_date=room_date,
            duration_seconds=4.0,
        )
        storage.put(f"{talk_id}/intro/intro.mp4", intro_tmp)

    # 2. Generate Real Outro Slate
    with tempfile.TemporaryDirectory() as tmpdir:
        outro_tmp = Path(tmpdir) / "outro.mp4"
        generate_outro_clip(
            outro_tmp,
            event_name=event_name,
            duration_seconds=3.0,
        )
        storage.put(f"{talk_id}/outro/outro.mp4", outro_tmp)

    # 3. Ensure Raw recording exists and Cut
    raw_keys = storage.list_keys(f"{talk_id}/raw")
    raw_key = raw_keys[0] if raw_keys else f"{talk_id}/raw/raw.mp4"
    if not storage.exists(raw_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No raw recording found for talk. Please upload or ingest a recording first.",
        )

    raw_path = storage.get(raw_key)
    cut_key = f"{talk_id}/cut/cut.mp4"
    s_sec = start_sec if start_sec is not None else 0.0
    e_sec = end_sec if end_sec is not None else (talk.raw_duration_seconds or 15.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        cut_tmp = Path(tmpdir) / "cut.mp4"
        cut(raw_path, cut_tmp, s_sec, e_sec)
        storage.put(cut_key, cut_tmp)

    # 4. Generate Low-Res Preview
    preset = PREVIEW_PRESETS.get("small_video")
    with tempfile.TemporaryDirectory() as tmpdir:
        prev_tmp = Path(tmpdir) / "preview.mp4"
        generate_preview(storage.get(cut_key), prev_tmp, preset=preset)
        storage.put(f"{talk_id}/preview/preview.mp4", prev_tmp)

    # Log completed jobs
    for kind in ("cut", "intro", "outro", "preview"):
        job = models.Job(
            talk_id=talk_id,
            kind=kind,
            status="done",
            log_path=f"{talk_id}/logs/{kind}.log",
        )
        db.add(job)


def _concat_clips(
    clip_paths: list[Path], output_path: Path, target_resolution=(1280, 720), fps=25
) -> None:
    """Concatenates intro + talk + outro clips into a single video."""
    import av

    with av.open(
        str(output_path), mode="w", options={"movflags": "faststart"}
    ) as out_container:
        v_stream = out_container.add_stream("libx264", rate=fps, options={"crf": "22"})
        v_stream.width, v_stream.height = target_resolution
        v_stream.pix_fmt = "yuv420p"

        a_stream = out_container.add_stream("aac", rate=48000)
        a_stream.layout = "stereo"
        resampler = av.AudioResampler(format="s16p", layout="stereo", rate=48000)

        for clip in clip_paths:
            if not clip.exists():
                continue
            with av.open(str(clip)) as in_container:
                if in_container.streams.video:
                    for frame in in_container.decode(in_container.streams.video[0]):
                        img = frame.to_image().resize(target_resolution)
                        new_frame = av.VideoFrame.from_image(img)
                        for packet in v_stream.encode(new_frame):
                            out_container.mux(packet)
                if in_container.streams.audio:
                    for frame in in_container.decode(in_container.streams.audio[0]):
                        for resampled in resampler.resample(frame):
                            for packet in a_stream.encode(resampled):
                                out_container.mux(packet)

        for resampled in resampler.resample(None):
            for packet in a_stream.encode(resampled):
                out_container.mux(packet)
        for packet in v_stream.encode():
            out_container.mux(packet)
        for packet in a_stream.encode():
            out_container.mux(packet)


def _execute_master_transcode_pipeline(
    talk: models.Talk, storage: StorageBackend, db: Session
) -> None:
    """Executes final master transcode with attached Intro & Outro slates and publishes."""
    talk_id = talk.id
    cut_key = f"{talk_id}/cut/cut.mp4"
    intro_key = f"{talk_id}/intro/intro.mp4"
    outro_key = f"{talk_id}/outro/outro.mp4"

    if not storage.exists(cut_key):
        _execute_full_processing_pipeline(talk, storage, db)

    # Assemble [intro, cut, outro] and transcode
    with tempfile.TemporaryDirectory() as tmpdir:
        composite_tmp = Path(tmpdir) / "composite.mp4"
        final_tmp = Path(tmpdir) / "final.mp4"

        clips_to_join = []
        if storage.exists(intro_key):
            clips_to_join.append(storage.get(intro_key))
        if storage.exists(cut_key):
            clips_to_join.append(storage.get(cut_key))
        if storage.exists(outro_key):
            clips_to_join.append(storage.get(outro_key))

        if clips_to_join:
            _concat_clips(clips_to_join, composite_tmp)
            transcode(composite_tmp, final_tmp, preset=PRESET_720P)
        else:
            transcode(storage.get(cut_key), final_tmp, preset=PRESET_720P)

        publish(final_tmp, talk_id, storage)

    for kind in ("transcode", "publish"):
        job = models.Job(
            talk_id=talk_id,
            kind=kind,
            status="done",
            log_path=f"{talk_id}/logs/{kind}.log",
        )
        db.add(job)


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    client: Annotated[models.Client | None, Depends(get_optional_ui_client)] = None,
    event_id: int | None = None,
    status_filter: str | None = None,
    q: str | None = None,
):
    query = db.query(models.Talk)
    if client is not None:
        query = query.filter(models.Talk.event_id.in_(client.event_ids))
    if event_id is not None:
        if client is not None and event_id not in client.event_ids:
            query = query.filter(models.Talk.id == -1)
        else:
            query = query.filter(models.Talk.event_id == event_id)
    if status_filter:
        query = query.filter(models.Talk.status == status_filter)

    talks = query.order_by(models.Talk.start.desc()).all()
    if q:
        q_lower = q.lower()
        talks = [t for t in talks if q_lower in t.title.lower()]

    if client is not None:
        all_talks = (
            db.query(models.Talk)
            .filter(models.Talk.event_id.in_(client.event_ids))
            .all()
        )
    else:
        all_talks = db.query(models.Talk).all()

    all_rooms = sorted({t.room for t in all_talks if t.room})
    status_counts: dict[str, int] = {}
    for t in all_talks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    stats = {
        "total": len(all_talks),
        "pending": status_counts.get("pending_approval", 0),
        "processing": sum(
            status_counts.get(s, 0)
            for s in ("cutting", "generating_previews", "transcoding", "uploading")
        ),
        "preview": status_counts.get("preview", 0),
        "done": status_counts.get("done", 0),
        "broken": status_counts.get("broken", 0) + status_counts.get("rejected", 0),
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "talks": talks,
            "stats": stats,
            "all_statuses": ALL_STATUSES,
            "all_rooms": all_rooms,
            "q": q or "",
            "status_filter": status_filter or "",
            "event_id": event_id,
        },
    )


@router.get("/media/{talk_id}/{filename}")
def get_talk_media_default(
    talk_id: int,
    filename: str,
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    candidate_keys = [
        f"{talk_id}/preview/{filename}",
        f"{talk_id}/raw/{filename}",
        f"{talk_id}/intro/{filename}",
        f"{talk_id}/outro/{filename}",
        f"{talk_id}/cut/{filename}",
        f"{talk_id}/final/{filename}",
    ]
    for key in candidate_keys:
        if storage.exists(key):
            path = storage.get(key)
            return FileResponse(path, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Media not found")


@router.get("/media/{talk_id}/{category}/{filename}")
def get_talk_media_categorized(
    talk_id: int,
    category: str,
    filename: str,
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    key = f"{talk_id}/{category}/{filename}"
    if not storage.exists(key):
        raise HTTPException(status_code=404, detail=f"Media {key} not found")
    path = storage.get(key)
    return FileResponse(path, media_type="video/mp4")


@router.get("/talks/{talk_id}", response_class=HTMLResponse)
def studio(
    request: Request,
    talk_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    client: Annotated[models.Client | None, Depends(get_optional_ui_client)] = None,
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or (client is not None and talk.event_id not in client.event_ids):
        raise HTTPException(status_code=404, detail="Talk not found")

    jobs = (
        db.query(models.Job)
        .filter(models.Job.talk_id == talk_id)
        .order_by(models.Job.id.desc())
        .limit(10)
        .all()
    )

    duration_seconds = None
    if talk.start and talk.end:
        duration_seconds = int((talk.end - talk.start).total_seconds())

    # Build categorized media assets that can be watched in the studio
    asset_defs = [
        ("preview", "preview.mp4", "Preview Video"),
        ("raw", "raw.mp4", "Raw Recording"),
        ("intro", "intro.mp4", "Opening Title Slate"),
        ("outro", "outro.mp4", "Outro Slate"),
        ("cut", "cut.mp4", "Cut Talk Clip"),
        ("final", "final.mp4", "Master Video (Final)"),
    ]
    media_assets = []
    seen_urls = set()

    for cat, fname, label in asset_defs:
        key = f"{talk.id}/{cat}/{fname}"
        if storage.exists(key):
            url = f"/studio/media/{talk.id}/{cat}/{fname}"
            if url not in seen_urls:
                seen_urls.add(url)
                media_assets.append(
                    {
                        "label": label,
                        "category": cat,
                        "url": url,
                    }
                )

    # Check for any dynamic raw filenames
    for rk in storage.list_keys(f"{talk.id}/raw"):
        fname = Path(rk).name
        url = f"/studio/media/{talk.id}/raw/{fname}"
        if url not in seen_urls:
            seen_urls.add(url)
            media_assets.append(
                {
                    "label": "Raw Recording",
                    "category": "raw",
                    "url": url,
                }
            )

    preview_urls = [a["url"] for a in media_assets]

    return templates.TemplateResponse(
        request,
        "studio.html",
        {
            "talk": talk,
            "jobs": jobs,
            "milestones": get_evaluated_milestones(talk.status),
            "duration_seconds": duration_seconds,
            "media_assets": media_assets,
            "preview_urls": preview_urls,
            "all_statuses": ALL_STATUSES,
        },
    )


# ── Interactive Pipeline Actions ─────────────────────────────────


class StatusUpdateRequest(BaseModel):
    status: str
    note: str | None = None


class ReviewActionRequest(BaseModel):
    decision: str = "approved"
    note: str | None = None
    start_sec: float | None = None
    end_sec: float | None = None


class TalkEditRequest(BaseModel):
    title: str | None = None
    room: str | None = None


def _get_scoped_talk(talk_id: int, client: models.Client, db: Session) -> models.Talk:
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Talk not found",
        )
    return talk


@router.post("/talks/{talk_id}/status")
def update_talk_status(
    talk_id: int,
    payload: StatusUpdateRequest,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
):
    talk = _get_scoped_talk(talk_id, client, db)
    if payload.status not in ALL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status: {payload.status}",
        )

    talk.status = payload.status
    if payload.note:
        job = models.Job(
            talk_id=talk.id,
            kind="manual_override",
            status="done",
            log_path=f"Status set to {payload.status}: {payload.note}",
        )
        db.add(job)
    db.commit()
    return {"status": "ok", "new_status": talk.status}


@router.post("/talks/{talk_id}/approve")
def approve_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    payload: ReviewActionRequest | None = None,
):
    talk = _get_scoped_talk(talk_id, client, db)

    decision = payload.decision if payload else "approved"
    note = payload.note if payload else "Approved in studio"

    review = models.Review(talk_id=talk.id, decision=decision, note=note)
    db.add(review)

    # Gate 1: pending_approval -> execute processing pipeline -> preview
    if talk.status in ("pending_approval", "waiting_for_files", "needs_work"):
        _execute_full_processing_pipeline(
            talk,
            storage,
            db,
            start_sec=payload.start_sec if payload else None,
            end_sec=payload.end_sec if payload else None,
        )
        talk.status = "preview"
    # Gate 2: preview -> execute transcode & publish -> done
    elif talk.status == "preview":
        _execute_master_transcode_pipeline(talk, storage, db)
        talk.status = "done"
    else:
        _execute_full_processing_pipeline(talk, storage, db)
        talk.status = "preview"

    db.commit()
    return {
        "status": "ok",
        "message": "Pipeline step completed",
        "talk_status": talk.status,
    }


@router.post("/talks/{talk_id}/reject")
def reject_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    payload: ReviewActionRequest | None = None,
):
    talk = _get_scoped_talk(talk_id, client, db)

    decision = payload.decision if payload else "rejected"
    note = payload.note if payload else "Rejected by reviewer"

    review = models.Review(talk_id=talk.id, decision=decision, note=note)
    db.add(review)

    talk.status = "rejected"
    db.commit()
    return {
        "status": "ok",
        "message": "Talk rejected",
        "talk_status": talk.status,
    }


@router.post("/talks/{talk_id}/retry")
def retry_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = _get_scoped_talk(talk_id, client, db)

    # Clean up stale derived artifacts while preserving the raw recording in raw/
    for derived_stage in ("cut", "intro", "outro", "preview", "final", "publish"):
        storage.delete(f"{talk_id}/{derived_stage}")

    talk.status = "pending_approval"
    db.commit()
    return {
        "status": "ok",
        "message": "Reset to pending_approval and cleared derived artifacts",
        "talk_status": talk.status,
    }


@router.post("/talks/{talk_id}/generate-preview")
def generate_talk_preview(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = _get_scoped_talk(talk_id, client, db)

    _execute_full_processing_pipeline(talk, storage, db)
    talk.status = "preview"
    db.commit()

    return {
        "status": "ok",
        "url": f"/studio/media/{talk.id}/preview/preview.mp4",
        "talk_status": talk.status,
    }


@router.post("/talks/{talk_id}/edit")
def edit_talk(
    talk_id: int,
    payload: TalkEditRequest,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
):
    talk = _get_scoped_talk(talk_id, client, db)

    if payload.title is not None:
        talk.title = payload.title
    if payload.room is not None:
        talk.room = payload.room

    db.commit()
    return {"status": "ok", "title": talk.title, "room": talk.room}


@router.post("/schedule/import")
async def import_schedule(
    request: Request,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile | None, File()] = None,
):
    data = None
    if file and file.filename:
        content = await file.read()
        data = json.loads(content.decode("utf-8"))
    else:
        body = await request.json()
        data = body.get("schedule") or body

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty schedule data"
        )

    event_name = "Conference Event"
    talks_to_create = []

    if isinstance(data, dict):
        if "schedule" in data and "conference" in data["schedule"]:
            conf = data["schedule"]["conference"]
            event_name = conf.get("title") or event_name
            for day in conf.get("days", []):
                for room_name, room_talks in day.get("rooms", {}).items():
                    for t in room_talks:
                        talks_to_create.append(
                            {
                                "title": t.get("title", "Untitled Session"),
                                "room": room_name,
                            }
                        )
        elif "talks" in data:
            event_name = data.get("event_name") or event_name
            talks_to_create = data["talks"]
        elif "title" in data:
            talks_to_create = [data]
    elif isinstance(data, list):
        talks_to_create = data

    event = db.query(models.Event).filter(models.Event.name == event_name).first()
    if not event:
        event = models.Event(name=event_name)
        db.add(event)
        db.commit()
        db.refresh(event)

    # Ensure client has access to this event
    if event.id not in client.event_ids:
        client.event_ids = list(set(client.event_ids + [event.id]))
        db.commit()

    created_count = 0
    now = datetime.now(UTC)
    for t_info in talks_to_create:
        title = t_info.get("title") or "Untitled Talk"
        room = t_info.get("room") or "Main Hall"
        talk = models.Talk(
            event_id=event.id,
            title=title,
            room=room,
            start=now + timedelta(minutes=created_count * 45),
            end=now + timedelta(minutes=(created_count + 1) * 45),
            status="waiting_for_files",
        )
        db.add(talk)
        created_count += 1

    db.commit()
    return {
        "status": "ok",
        "event_id": event.id,
        "event_name": event.name,
        "imported_count": created_count,
    }


@router.post("/talks/create")
async def create_single_talk(
    request: Request,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
):
    body = await request.json()
    event_name = (body.get("event_name") or "").strip() or "General Event"
    title = (body.get("title") or "").strip() or "Untitled Session"
    room = (body.get("room") or "").strip() or "Room 1"

    event = db.query(models.Event).filter(models.Event.name == event_name).first()
    if not event:
        event = models.Event(name=event_name)
        db.add(event)
        db.commit()
        db.refresh(event)

    if event.id not in client.event_ids:
        client.event_ids = list(set(client.event_ids + [event.id]))
        db.commit()

    now = datetime.now(UTC)
    talk = models.Talk(
        event_id=event.id,
        title=title,
        room=room,
        start=now,
        end=now + timedelta(minutes=45),
        status="waiting_for_files",
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return {"status": "ok", "talk_id": talk.id}


@router.post("/talks/{talk_id}/upload-recording")
async def upload_talk_recording(
    talk_id: int,
    file: Annotated[UploadFile, File()],
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    import av

    talk = _get_scoped_talk(talk_id, client, db)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / (file.filename or "recording.mp4")
        content = await file.read()
        # storage-boundary-exempt: upload staging
        tmp_path.write_bytes(content)

        try:
            with av.open(str(tmp_path)) as container:
                if not container.streams.video:
                    raise ValueError("File contains no video stream")
                duration = float(container.duration) / av.time_base
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid video file: {exc}",
            ) from exc

        raw_key = f"{talk_id}/raw/raw.mp4"
        storage.put(raw_key, tmp_path)

    talk.status = "pending_approval"
    talk.cut_start = 0.0
    talk.cut_end = duration
    talk.raw_duration_seconds = duration
    db.commit()

    return {
        "status": "ok",
        "talk_status": talk.status,
        "url": f"/studio/media/{talk_id}/raw/raw.mp4",
        "duration": duration,
    }


@router.post("/room/attach-recording")
async def attach_room_recording(
    room: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    event_id: Annotated[int | None, Form()] = None,
):
    import av

    room_clean = room.strip()
    query = db.query(models.Talk).filter(
        models.Talk.room == room_clean,
        models.Talk.event_id.in_(client.event_ids),
    )
    if event_id is not None:
        if event_id not in client.event_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Client is not authorized to access this event",
            )
        query = query.filter(models.Talk.event_id == event_id)

    talks_in_room = query.all()
    if not talks_in_room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No talks found in room '{room_clean}'",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / (file.filename or "room_recording.mp4")
        content = await file.read()
        # storage-boundary-exempt: upload staging
        tmp_path.write_bytes(content)

        try:
            with av.open(str(tmp_path)) as container:
                if not container.streams.video:
                    raise ValueError("Uploaded file contains no video stream")
                duration = float(container.duration) / av.time_base
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid video file: {exc}",
            ) from exc

        for talk in talks_in_room:
            raw_key = f"{talk.id}/raw/raw.mp4"
            storage.put(raw_key, tmp_path)
            talk.status = "pending_approval"
            talk.cut_start = 0.0
            talk.cut_end = duration
            talk.raw_duration_seconds = duration

    db.commit()
    return {
        "status": "ok",
        "room": room_clean,
        "attached_count": len(talks_in_room),
        "talk_ids": [t.id for t in talks_in_room],
    }


class BulkDeleteRequest(BaseModel):
    talk_ids: list[int]


@router.post("/talks/{talk_id}/delete")
def delete_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = _get_scoped_talk(talk_id, client, db)

    storage.delete(f"{talk_id}")
    db.query(models.Review).filter(models.Review.talk_id == talk_id).delete()
    db.query(models.Job).filter(models.Job.talk_id == talk_id).delete()
    db.delete(talk)
    db.commit()

    return {"status": "ok", "deleted_id": talk_id}


@router.post("/talks/bulk-delete")
def bulk_delete_talks(
    payload: BulkDeleteRequest,
    client: Annotated[models.Client, Depends(get_ui_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    if not payload.talk_ids:
        return {"status": "ok", "deleted_count": 0}

    valid_talks = (
        db.query(models.Talk)
        .filter(
            models.Talk.id.in_(payload.talk_ids),
            models.Talk.event_id.in_(client.event_ids),
        )
        .all()
    )
    valid_ids = [t.id for t in valid_talks]
    if not valid_ids:
        return {"status": "ok", "deleted_count": 0}

    for tid in valid_ids:
        storage.delete(f"{tid}")
        db.query(models.Review).filter(models.Review.talk_id == tid).delete()
        db.query(models.Job).filter(models.Job.talk_id == tid).delete()

    deleted_count = (
        db.query(models.Talk)
        .filter(models.Talk.id.in_(valid_ids))
        .delete(synchronize_session=False)
    )
    db.commit()

    return {"status": "ok", "deleted_count": deleted_count}
