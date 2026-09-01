from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import models
from app.main import app
from app.storage import StorageBackend, get_storage_backend


@pytest.fixture
def client():
    return TestClient(app)


def test_root_redirect(client: TestClient):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui"


def test_static_assets(client: TestClient):
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers.get("content-type", "")

    dash_js = client.get("/static/js/dashboard.js")
    assert dash_js.status_code == 200

    studio_js = client.get("/static/js/studio.js")
    assert studio_js.status_code == 200


from app.db import SessionLocal, get_db


@pytest.fixture
def db_session():
    db = SessionLocal()
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_dashboard_page(client: TestClient, db_session):
    event = models.Event(name="Test UI Event")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Test UI Dashboard Talk",
        room="Auditorium",
        start=now,
        end=now + timedelta(minutes=45),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Test UI Dashboard Talk" in response.text
    assert "Auditorium" in response.text


def test_talk_studio_page(client: TestClient, db_session):
    event = models.Event(name="Test Studio Event")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Test Studio Detail Talk",
        room="Room 101",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    response = client.get(f"/ui/talks/{talk.id}")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Test Studio Detail Talk" in response.text
    assert "Room 101" in response.text


def test_talk_studio_not_found(client: TestClient):
    response = client.get("/ui/talks/999999")
    assert response.status_code == 404


def test_media_serving(client: TestClient, tmp_path):
    from tests.conftest import generate_clip

    clip = generate_clip(0.5, output_dir=tmp_path)
    storage: StorageBackend = app.dependency_overrides.get(
        get_storage_backend, get_storage_backend()
    )
    storage.put("999/preview/preview.mp4", clip)

    response = client.get("/ui/media/999/preview.mp4")
    assert response.status_code == 200
    assert "video/mp4" in response.headers.get("content-type", "")

    not_found = client.get("/ui/media/999/missing.mp4")
    assert not_found.status_code == 404
