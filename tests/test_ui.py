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


def test_ui_post_routes_require_authentication(client: TestClient, db_session):
    """Mutating UI endpoints must return 401 if unauthenticated."""
    res = client.post("/ui/talks/1/status", json={"status": "waiting_for_files"})
    assert res.status_code == 401

    res = client.post("/ui/talks/1/approve")
    assert res.status_code == 401

    res = client.post("/ui/talks/1/reject")
    assert res.status_code == 401

    res = client.post("/ui/talks/1/retry")
    assert res.status_code == 401

    res = client.post("/ui/talks/1/delete")
    assert res.status_code == 401


def test_ui_post_routes_event_scoping(client: TestClient, db_session):
    """Mutating talks belonging to other events returns 404."""
    import uuid

    from app.auth import hash_api_key

    # Client has access only to event 1
    api_key = f"test_ui_api_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[1])
    db_session.add(client_model)

    event2 = models.Event(name=f"Other Event {uuid.uuid4().hex}")
    db_session.add(event2)
    db_session.commit()
    db_session.refresh(event2)

    now = datetime.now(tz=UTC)
    talk_other = models.Talk(
        event_id=event2.id,
        title=f"Other Event Talk {uuid.uuid4().hex}",
        room="Hall B",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk_other)
    db_session.commit()
    db_session.refresh(talk_other)

    # Attempt mutation with X-API-Key
    headers = {"X-API-Key": api_key}
    res = client.post(
        f"/ui/talks/{talk_other.id}/status",
        json={"status": "needs_work"},
        headers=headers,
    )
    assert res.status_code == 404


def test_ui_post_routes_authenticated_success(client: TestClient, db_session):
    """Authenticated UI mutations succeed with valid key."""
    import uuid

    from app.auth import hash_api_key

    event = models.Event(name=f"Allowed Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"valid_ui_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title=f"Authorized Talk {uuid.uuid4().hex}",
        room="Hall A",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    # Header authentication
    res = client.post(
        f"/ui/talks/{talk.id}/status",
        json={"status": "pending_approval", "note": "Reviewed"},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    assert res.json()["new_status"] == "pending_approval"

    # Cookie authentication
    client.cookies.set("veditor_api_key", api_key)
    res_cookie = client.post(
        f"/ui/talks/{talk.id}/edit",
        json={"title": "Updated Title", "room": "Hall C"},
    )
    assert res_cookie.status_code == 200
    assert res_cookie.json()["title"] == "Updated Title"
    client.cookies.clear()
