import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc

from app.db import get_session
from app.main import app
from app.models.tour_job import TourJobStatus
from app.rate_limit import JOURNEY_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id


def _noop_verify():
    return "user_123"


FAKE_INSTALLATION_ID = 12345


def _fake_session():
    """Session whose GithubConnections lookup succeeds and whose refresh assigns an id."""
    session = MagicMock()
    gh_conn = MagicMock()
    gh_conn.installationId = FAKE_INSTALLATION_ID
    session.exec.return_value.one.return_value = gh_conn

    def _assign_id(obj):
        obj.id = 1

    session.refresh.side_effect = _assign_id
    yield session


@pytest.fixture(autouse=True)
def _override_deps():
    app.dependency_overrides[get_authenticated_user_id] = _noop_verify
    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[JOURNEY_CREATE_RATE_LIMIT] = lambda: None
    yield
    app.dependency_overrides.clear()


client = TestClient(app)

JOURNEYS_URL = "/api/v1/journeys"


def _body(**overrides):
    base = {
        "repoName": "org/repo",
        "topic": "authentication flow",
    }
    base.update(overrides)
    return base


def _make_job(**overrides):
    job = MagicMock()
    job.id = overrides.get("id", 1)
    job.status = overrides.get("status", TourJobStatus.COMPLETE)
    job.userId = overrides.get("userId", "user_123")
    job.repo_name = overrides.get("repo_name", "org/repo")
    job.topic = overrides.get("topic", "authentication flow")
    job.artifact = overrides.get("artifact", None)
    job.error = overrides.get("error", None)
    job.createdAt = overrides.get("createdAt", dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    return job


# ── create (POST) ───────────────────────────────────────────────────

def test_create_returns_pending_job():
    resp = client.post(JOURNEYS_URL, json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["status"] == TourJobStatus.PENDING


@patch("app.worker.run_job")
@patch("app.worker.generate_tour")
def test_create_does_not_invoke_generation(mock_generate, mock_run):
    resp = client.post(JOURNEYS_URL, json=_body())
    assert resp.status_code == 200
    assert resp.json()["status"] == TourJobStatus.PENDING
    mock_generate.assert_not_called()
    mock_run.assert_not_called()


def test_create_rejects_deprecated_user_id_field():
    resp = client.post(JOURNEYS_URL, json=_body(userId="user_123"))
    assert resp.status_code == 422


def test_create_missing_topic_returns_422():
    resp = client.post(JOURNEYS_URL, json={"repoName": "org/repo"})
    assert resp.status_code == 422


def test_create_empty_topic_returns_422():
    resp = client.post(JOURNEYS_URL, json=_body(topic=""))
    assert resp.status_code == 422


def test_create_topic_too_long_returns_422():
    resp = client.post(JOURNEYS_URL, json=_body(topic="x" * 501))
    assert resp.status_code == 422


def test_create_no_github_connection_returns_404():
    def _no_conn_session():
        session = MagicMock()
        session.exec.return_value.one.side_effect = exc.NoResultFound()
        yield session

    app.dependency_overrides[get_session] = _no_conn_session
    resp = client.post(JOURNEYS_URL, json=_body())
    assert resp.status_code == 404


# ── read (GET /{id}) ────────────────────────────────────────────────

def test_get_returns_job():
    job = _make_job(status=TourJobStatus.COMPLETE, artifact={"title": "Tour", "steps": []})

    def _session_with_job():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = _session_with_job
    resp = client.get(f"{JOURNEYS_URL}/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["status"] == TourJobStatus.COMPLETE
    assert data["repoName"] == "org/repo"
    assert data["artifact"] == {"title": "Tour", "steps": []}


def test_get_not_found_returns_404():
    def _session_no_job():
        session = MagicMock()
        session.get.return_value = None
        yield session

    app.dependency_overrides[get_session] = _session_no_job
    resp = client.get(f"{JOURNEYS_URL}/999")
    assert resp.status_code == 404


def test_get_forbidden_when_owner_mismatch():
    job = _make_job(userId="someone_else")

    def _session_other_owner():
        session = MagicMock()
        session.get.return_value = job
        yield session

    app.dependency_overrides[get_session] = _session_other_owner
    resp = client.get(f"{JOURNEYS_URL}/1")
    assert resp.status_code == 403


# ── list (GET) ──────────────────────────────────────────────────────

def test_list_returns_summaries():
    jobs = [_make_job(id=2, topic="request lifecycle"), _make_job(id=1)]

    def _session_with_jobs():
        session = MagicMock()
        session.exec.return_value.all.return_value = jobs
        yield session

    app.dependency_overrides[get_session] = _session_with_jobs
    resp = client.get(JOURNEYS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert {d["id"] for d in data} == {1, 2}
    assert "artifact" not in data[0]
