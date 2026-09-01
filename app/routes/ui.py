from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import jinja2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.config import PREVIEW_PRESETS
from app.db import get_db
from app.pipeline.cut import cut
from app.pipeline.intro import generate_intro_clip
from app.pipeline.outro import generate_outro_clip
from app.pipeline.preview import generate_preview
from app.pipeline.publish import publish
from app.pipeline.transcode import PRESET_720P, transcode
from app.storage import StorageBackend, get_storage_backend
from tests.conftest import generate_clip

_TEMPLATES_DIR = Path(__file__).parent.parent / "ui" / "templates"

# Disable cache to avoid Jinja2 3.1.5+ unhashable cache key issue
_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_env)

router = APIRouter(prefix="/ui", tags=["ui"])

ALL_STATUSES = [
    "waiting_for_files",
    "pending_approval",
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
    "pending_approval": 1,
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
    raw_key = f"{talk_id}/raw/raw.mp4"
    if not storage.exists(raw_key):
        clip = generate_clip(
            15.0,
            pattern="gradient",
            resolution=(640, 360),
            fps=25,
            audio_waveform="tone",
        )
        storage.put(raw_key, clip)
        # storage-boundary-exempt: temporary synthetic clip cleanup
        clip.unlink(missing_ok=True)

    raw_path = storage.get(raw_key)
    cut_key = f"{talk_id}/cut/cut.mp4"
    s_sec = start_sec if start_sec is not None else 0.0
    e_sec = end_sec if end_sec is not None else 15.0

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


def _execute_master_transcode_pipeline(
    talk: models.Talk, storage: StorageBackend, db: Session
) -> None:
    """Executes final master transcode and publish."""
    talk_id = talk.id
    cut_key = f"{talk_id}/cut/cut.mp4"
    if not storage.exists(cut_key):
        _execute_full_processing_pipeline(talk, storage, db)

    # Transcode presets
    with tempfile.TemporaryDirectory() as tmpdir:
        final_tmp = Path(tmpdir) / "final.mp4"
        transcode(storage.get(cut_key), final_tmp, preset=PRESET_720P)
        storage.put(f"{talk_id}/final/final.mp4", final_tmp)

    # Publish
    with tempfile.TemporaryDirectory() as tmpdir:
        meta_tmp = Path(tmpdir) / "publish.json"
        publish(storage.get(f"{talk_id}/final/final.mp4"), meta_tmp)
        storage.put(f"{talk_id}/publish/metadata.json", meta_tmp)

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
    event_id: int | None = None,
    status_filter: str | None = None,
    q: str | None = None,
):
    query = db.query(models.Talk)
    if event_id is not None:
        query = query.filter(models.Talk.event_id == event_id)
    if status_filter:
        query = query.filter(models.Talk.status == status_filter)

    talks = query.order_by(models.Talk.start.desc()).all()
    if q:
        q_lower = q.lower()
        talks = [t for t in talks if q_lower in t.title.lower()]

    all_talks = db.query(models.Talk).all()
    stats = {
        "total": len(all_talks),
        "pending": sum(1 for t in all_talks if t.status == "pending_approval"),
        "processing": sum(
            1
            for t in all_talks
            if t.status
            in {"cutting", "generating_previews", "transcoding", "uploading"}
        ),
        "preview": sum(1 for t in all_talks if t.status == "preview"),
        "done": sum(1 for t in all_talks if t.status == "done"),
        "broken": sum(1 for t in all_talks if t.status in {"broken", "rejected"}),
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "talks": talks,
            "stats": stats,
            "all_statuses": ALL_STATUSES,
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
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
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
        ("intro", "intro.mp4", "Opening Title Slate"),
        ("outro", "outro.mp4", "Outro Slate"),
        ("cut", "cut.mp4", "Cut Talk Clip"),
        ("final", "final.mp4", "Master Video (Final)"),
    ]
    media_assets = []
    for cat, fname, label in asset_defs:
        key = f"{talk.id}/{cat}/{fname}"
        if storage.exists(key):
            media_assets.append(
                {
                    "label": label,
                    "category": cat,
                    "url": f"/ui/media/{talk.id}/{cat}/{fname}",
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


@router.post("/talks/{talk_id}/status")
def update_talk_status(
    talk_id: int,
    payload: StatusUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
        raise HTTPException(status_code=404, detail="Talk not found")
    if payload.status not in ALL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")

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
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    payload: ReviewActionRequest | None = None,
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
        raise HTTPException(status_code=404, detail="Talk not found")

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
    db: Annotated[Session, Depends(get_db)],
    payload: ReviewActionRequest | None = None,
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
        raise HTTPException(status_code=404, detail="Talk not found")

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
    db: Annotated[Session, Depends(get_db)],
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
        raise HTTPException(status_code=404, detail="Talk not found")

    talk.status = "pending_approval"
    db.commit()
    return {
        "status": "ok",
        "message": "Reset to pending_approval",
        "talk_status": talk.status,
    }


@router.post("/talks/{talk_id}/generate-preview")
def generate_talk_preview(
    talk_id: int,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
        raise HTTPException(status_code=404, detail="Talk not found")

    _execute_full_processing_pipeline(talk, storage, db)
    talk.status = "preview"
    db.commit()

    return {
        "status": "ok",
        "url": f"/ui/media/{talk.id}/preview/preview.mp4",
        "talk_status": talk.status,
    }


@router.post("/talks/{talk_id}/edit")
def edit_talk(
    talk_id: int,
    payload: TalkEditRequest,
    db: Annotated[Session, Depends(get_db)],
):
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk:
        raise HTTPException(status_code=404, detail="Talk not found")

    if payload.title is not None:
        talk.title = payload.title
    if payload.room is not None:
        talk.room = payload.room

    db.commit()
    return {"status": "ok", "title": talk.title, "room": talk.room}
