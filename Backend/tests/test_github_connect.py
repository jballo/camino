import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from psycopg2.errorcodes import NOT_NULL_VIOLATION, UNIQUE_VIOLATION
from github import GithubException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.security import get_authenticated_user_id
from app.services.installation_state import SUSPENSION_ERROR


CONNECT_URL = "/api/v1/github/connect"
GITHUB_USER_ID = 4242


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


def _noop_verify():
    return "user_123"


def _access_token():
    token = MagicMock()
    token.token = "access-token"
    token.expires_in = 28800
    token.refresh_token = "refresh-token"
    token.refresh_expires_in = 15897600
    token.created = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)
    return token


def _patch_github(
    installation_ids: tuple[int, ...] = (99,),
    *,
    installations_error: Exception | None = None,
    suspended_at: dt.datetime | None = None,
):
    oauth_app = MagicMock()
    oauth_app.get_access_token.return_value = _access_token()

    github_user = MagicMock()
    github_user.login = "octocat"
    github_user.id = GITHUB_USER_ID
    if installations_error is None:
        github_user.get_installations.return_value = [
            SimpleNamespace(id=installation_id, suspended_at=suspended_at)
            for installation_id in installation_ids
        ]
    else:
        github_user.get_installations.side_effect = installations_error

    anonymous = MagicMock()
    anonymous.get_oauth_application.return_value = oauth_app

    authenticated = MagicMock()
    authenticated.get_user.return_value = github_user

    return patch("app.api.github.Github", side_effect=[anonymous, authenticated])


@pytest.fixture
def client_and_session():
    session = MagicMock()

    def _session():
        yield session

    app.dependency_overrides[get_authenticated_user_id] = _noop_verify
    app.dependency_overrides[get_session] = _session
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _integrity_error(pgcode: str) -> IntegrityError:
    orig = Exception("constraint violated")
    orig.pgcode = pgcode
    return IntegrityError("INSERT", {}, orig)


def test_connect_persists_github_user_id(client_and_session):
    client, session = client_and_session
    session.exec.return_value.one_or_none.return_value = None

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    added = session.add.call_args.args[0]
    assert isinstance(added, GithubConnections)
    assert added.userId == "user_123"
    assert added.githubUsername == "octocat"
    assert added.githubUserId == GITHUB_USER_ID
    assert added.installationId == 99
    assert added.active is True


def test_connect_updates_github_user_id_on_existing_row(client_and_session):
    client, session = client_and_session
    existing = MagicMock()
    session.exec.return_value.one_or_none.return_value = existing

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    assert existing.githubUserId == GITHUB_USER_ID
    assert existing.githubUsername == "octocat"
    assert existing.installationId == 99
    assert existing.active is True


@pytest.mark.parametrize("existing", [False, True])
def test_connect_marks_suspended_installation_inactive(
    existing,
    client_and_session,
):
    client, session = client_and_session
    existing_connection = MagicMock() if existing else None
    session.exec.return_value.one_or_none.return_value = existing_connection

    with _patch_github(suspended_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    connection = existing_connection or session.add.call_args.args[0]
    assert connection.active is False


def test_unique_user_conflict_returns_409(client_and_session):
    client, session = client_and_session
    session.exec.return_value.one_or_none.return_value = None
    session.commit.side_effect = _integrity_error(UNIQUE_VIOLATION)

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Already connected"}
    session.rollback.assert_called_once_with()


def test_not_null_integrity_error_returns_500(client_and_session):
    client, session = client_and_session
    session.exec.return_value.one_or_none.return_value = None
    session.commit.side_effect = _integrity_error(NOT_NULL_VIOLATION)

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error"}
    session.rollback.assert_called_once_with()


def test_foreign_installation_is_rejected_without_persisting(client_and_session):
    client, session = client_and_session

    with _patch_github((12, 34)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Installation not accessible to this GitHub user"
    }
    session.exec.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_foreign_installation_cannot_replace_existing_connection(
    client_and_session,
):
    client, session = client_and_session
    existing = SimpleNamespace(installationId=77)
    session.exec.return_value.one_or_none.return_value = existing

    with _patch_github((12, 34)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 403
    assert existing.installationId == 77
    session.exec.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_installation_lookup_error_returns_502_without_persisting(
    client_and_session,
):
    client, session = client_and_session
    error = GithubException(500, {"message": "upstream failed"}, None)

    with _patch_github(installations_error=error):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Github error"}
    session.exec.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


@pytest.fixture
def sqlite_client_and_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    GithubConnections.__table__.create(engine)
    Job.__table__.create(engine)
    with Session(engine) as session:
        session.add_all(
            [
                GithubConnections(
                    userId="user_123",
                    githubUsername="octocat",
                    githubUserId=GITHUB_USER_ID,
                    installationId=98,
                ),
                GithubConnections(
                    userId="teammate",
                    githubUsername="teammate",
                    githubUserId=777,
                    installationId=202,
                ),
            ]
        )
        session.commit()

        def _session():
            yield session

        app.dependency_overrides[get_authenticated_user_id] = _noop_verify
        app.dependency_overrides[get_session] = _session
        client = TestClient(app)
        try:
            yield client, session
        finally:
            client.close()
            app.dependency_overrides.clear()
    engine.dispose()


def _seed_reconnect_jobs(session) -> tuple[int, int, int]:
    """Seed a running shared ingest, a pending brief, and a teammate's brief."""
    ingest = Job(
        userId="user_123",
        installation_id=98,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key="repository_ingest:org/repo:main",
        status=JobStatus.RUNNING,
        claimed_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        claimed_by="worker",
        attempts=2,
    )
    session.add(ingest)
    session.flush()
    own_brief = Job(
        userId="user_123",
        installation_id=98,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.ISSUE_BRIEF,
        status=JobStatus.PENDING,
        blocked_by_job_id=ingest.id,
    )
    teammate_brief = Job(
        userId="teammate",
        installation_id=202,
        repo_name="org/repo",
        ref="main",
        job_type=JobType.ISSUE_BRIEF,
        status=JobStatus.PENDING,
        blocked_by_job_id=ingest.id,
    )
    session.add_all([own_brief, teammate_brief])
    session.commit()
    return ingest.id, own_brief.id, teammate_brief.id


def test_reconnect_to_different_active_installation_moves_jobs(
    sqlite_client_and_session,
):
    client, session = sqlite_client_and_session
    ingest_id, own_brief_id, teammate_brief_id = _seed_reconnect_jobs(session)

    with _patch_github():
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    ingest = session.get(Job, ingest_id)
    assert ingest.userId == "user_123"
    assert ingest.installation_id == 99
    assert ingest.status == JobStatus.PENDING
    assert ingest.claimed_at is None
    assert ingest.claimed_by is None
    assert ingest.attempts == 1
    own_brief = session.get(Job, own_brief_id)
    assert own_brief.installation_id == 99
    assert own_brief.status == JobStatus.PENDING
    assert own_brief.blocked_by_job_id == ingest_id
    teammate_brief = session.get(Job, teammate_brief_id)
    assert teammate_brief.installation_id == 202
    assert teammate_brief.blocked_by_job_id == ingest_id


def test_reconnect_to_suspended_installation_cancels_jobs_and_hands_over_ingest(
    sqlite_client_and_session,
):
    client, session = sqlite_client_and_session
    ingest_id, own_brief_id, teammate_brief_id = _seed_reconnect_jobs(session)

    with _patch_github(suspended_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 99},
        )

    assert response.status_code == 200
    for job_id in (ingest_id, own_brief_id):
        job = session.get(Job, job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.error == SUSPENSION_ERROR
    replacement = session.exec(
        select(Job).where(
            Job.job_type == JobType.REPOSITORY_INGEST,
            Job.status == JobStatus.PENDING,
        )
    ).one()
    assert replacement.userId == "teammate"
    assert replacement.installation_id == 202
    teammate_brief = session.get(Job, teammate_brief_id)
    assert teammate_brief.status == JobStatus.PENDING
    assert teammate_brief.blocked_by_job_id == replacement.id


def test_reconnect_to_same_installation_leaves_jobs_untouched(
    sqlite_client_and_session,
):
    client, session = sqlite_client_and_session
    ingest_id, own_brief_id, _ = _seed_reconnect_jobs(session)

    with _patch_github(installation_ids=(98,)):
        response = client.post(
            CONNECT_URL,
            json={"code": "oauth-code", "installationId": 98},
        )

    assert response.status_code == 200
    ingest = session.get(Job, ingest_id)
    assert ingest.installation_id == 98
    assert ingest.status == JobStatus.RUNNING
    assert ingest.claimed_by == "worker"
    assert ingest.attempts == 2
    own_brief = session.get(Job, own_brief_id)
    assert own_brief.installation_id == 98
    assert own_brief.status == JobStatus.PENDING
