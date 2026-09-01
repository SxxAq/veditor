#!/usr/bin/env python3
"""
Seed the VEditor database with demo talks across all official pipeline statuses,
and generate real title slates, outros, and preview videos so the studio player works out of the box.

Usage:
    uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make sure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import models
from app.config import settings
from app.db import SessionLocal
from app.routes.ui import _execute_full_processing_pipeline
from app.storage import LocalDiskBackend

# ── Config ──────────────────────────────────────────────────────
API_KEY = "demo-secret-key-1234"
EVENT_NAME = "FOSSASIA Summit 2026"
DATA_DIR = settings.data_dir

STATUSES = [
    "waiting_for_files",
    "pending_approval",
    "cutting",
    "generating_previews",
    "preview",
    "transcoding",
    "uploading",
    "done",
    "needs_work",
    "rejected",
    "broken",
]

TALKS = [
    ("Opening Keynote & Welcome", "Main Hall", "09:00", "09:45"),
    ("Building OSS Hardware in 2026", "Room A", "10:00", "10:45"),
    ("Python Async & Concurrency", "Room B", "11:00", "11:30"),
    ("CI/CD Pipeline Automation", "Main Hall", "11:45", "12:30"),
    ("RISC-V Architecture Workshop", "Workshop A", "14:00", "15:30"),
    ("Edge AI & Computer Vision", "Room A", "14:00", "14:45"),
    ("Contributing to Linux Kernel", "Main Hall", "15:00", "16:00"),
    ("Closing Panel: Future of Open Source", "Main Hall", "16:30", "17:30"),
]

BASE_DATE = datetime(2026, 5, 15, tzinfo=UTC)


def hh_mm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def main() -> None:
    db = SessionLocal()
    storage = LocalDiskBackend(DATA_DIR)

    print("🌱 Seeding VEditor demo data…\n")

    # ── 1. Event ────────────────────────────────────────────────
    event = db.query(models.Event).filter_by(name=EVENT_NAME).first()
    if not event:
        event = models.Event(name=EVENT_NAME)
        db.add(event)
        db.commit()
        db.refresh(event)
        print(f"  ✓ Created event: {EVENT_NAME!r} (id={event.id})")
    else:
        print(f"  · Event already exists: {EVENT_NAME!r} (id={event.id})")

    # ── 2. API Client ───────────────────────────────────────────
    hashed = hashlib.sha256(API_KEY.encode()).hexdigest()
    client = db.query(models.Client).filter_by(hashed_key=hashed).first()
    if not client:
        client = models.Client(hashed_key=hashed, event_ids=[event.id])
        db.add(client)
        db.commit()
        db.refresh(client)
        print(f"  ✓ Created API client (key: {API_KEY!r})")
    else:
        if event.id not in client.event_ids:
            client.event_ids = client.event_ids + [event.id]
            db.commit()
        print(f"  · API client already exists (key: {API_KEY!r})")

    # ── 3. Talks ─────────────────────────────────────────────────
    print("\n  Creating talks:")
    created_talks = []
    for i, (title, room, start_str, end_str) in enumerate(TALKS):
        sh, sm = hh_mm(start_str)
        eh, em = hh_mm(end_str)
        start_dt = BASE_DATE + timedelta(hours=sh, minutes=sm)
        end_dt = BASE_DATE + timedelta(hours=eh, minutes=em)
        status = STATUSES[i % len(STATUSES)]

        talk = (
            db.query(models.Talk)
            .filter_by(event_id=event.id, title=title, start=start_dt)
            .first()
        )
        if not talk:
            talk = models.Talk(
                event_id=event.id,
                title=title,
                room=room,
                start=start_dt,
                end=end_dt,
                status=status,
            )
            db.add(talk)
            db.commit()
            db.refresh(talk)
            print(f"    ✓ [{status:22s}] {title}")
        else:
            talk.status = status
            db.commit()
            db.refresh(talk)
            print(f"    · [{status:22s}] {title} (updated)")

        created_talks.append(talk)

    # ── 4. Generate Real Pipeline Media Assets (Intro, Outro, Preview) ──
    print("\n  Generating real pipeline media assets (intro slates, outros, previews):")
    for talk in created_talks:
        if talk.status in ("preview", "done", "pending_approval"):
            print(
                f"    ⚙ Rendering real pipeline for talk #{talk.id} ({talk.title[:25]})…",
                end=" ",
                flush=True,
            )
            _execute_full_processing_pipeline(talk, storage, db)
            db.commit()
            print("done ✓")

    total = db.query(models.Talk).filter_by(event_id=event.id).count()
    db.close()

    print(f"""
✅ Done! Seeded {total} talks for event #{event.id}.

  API Key   : {API_KEY}
  Dashboard : http://localhost:8000/ui
""")


if __name__ == "__main__":
    main()
