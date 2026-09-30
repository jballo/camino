import hashlib
import hmac
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.security import get_authenticated_user_id
from app.services.authorization_revocation import AuthorizationRevocationError
from app.services.installation_deletion import InstallationDeletionError
from app.services.installation_state import InstallationStateError
from app.worker import JOB_AUTHORIZED_SQL


WEBHOOK_URL = "/webhooks/github"
INSTALLATION_ID = 101


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


def _signed(body: bytes, event: str = "installation") -> dict[str, str]:
    digest = hmac.new(
        settings.gh_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-github-event": event,
        "x-hub-signature-256": f"sha256={digest}",
        "content-type": "application/json",
    }


@pytest.fixture
def client_and_session():
    session = MagicMock()

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_authorization_revoked_deletes_only_matching_connections():
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
                    userId="user-1",
                    githubUsername="octocat",
                    githubUserId=501,
                    installationId=101,
                ),
                GithubConnections(
                    userId="user-2",
                    githubUsername="octocat",
                    githubUserId=501,
                    installationId=102,
                ),
                GithubConnections(
                    userId="user-3",
                    githubUsername="other",
                    githubUserId=777,
                    installationId=103,
                ),
            ]
        )
        session.add_all(
            [
                Job(
                    userId="user-1",
                    installation_id=101,
                    repo_name="org/repo",
                    ref="main",
                    status=JobStatus.PENDING,
                ),
                Job(
                    userId="user-3",
                    installation_id=103,
                    repo_name="org/other",
                    ref="main",
                    status=JobStatus.RUNNING,
                    claimed_by="worker",
                ),
            ]
        )
        session.commit()

        def _session():
            yield session

        app.dependency_overrides[get_session] = _session
        client = TestClient(app)
        body = json.dumps({"action": "revoked", "sender": {"id": 501}}).encode()

        try:
            response = client.post(
                WEBHOOK_URL,
                content=body,
                headers=_signed(body, "github_app_authorization"),
            )
        finally:
            client.close()
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.json() == "github app authorization revoked"

        connections = session.exec(
            select(GithubConnections).order_by(GithubConnections.userId)
        ).all()
        assert [connection.userId for connection in connections] == ["user-3"]
        assert connections[0].active is True
        jobs = session.exec(select(Job).order_by(Job.userId)).all()
        assert jobs[0].status == JobStatus.CANCELLED
        assert jobs[0].error == "GitHub authorization revoked"
        assert jobs[1].status == JobStatus.RUNNING

    engine.dispose()


def _post_installation_event(client: TestClient, action: str) -> None:
    body = json.dumps(
        {"action": action, "installation": {"id": INSTALLATION_ID}}
    ).encode()
    response = client.post(WEBHOOK_URL, content=body, headers=_signed(body))
    assert response.status_code == 200


def _post_revocation(client: TestClient, github_user_id: int) -> None:
    body = json.dumps(
        {"action": "revoked", "sender": {"id": github_user_id}}
    ).encode()
    response = client.post(
        WEBHOOK_URL,
        content=body,
        headers=_signed(body, "github_app_authorization"),
    )
    assert response.status_code == 200


def _worker_job_is_authorized(
    session: Session,
    *,
    user_id: str,
) -> bool:
    return bool(
        session.execute(
            JOB_AUTHORIZED_SQL,
            {"user_id": user_id, "installation_id": INSTALLATION_ID},
        ).scalar_one()
    )


def test_unsuspend_does_not_restore_revoked_user_on_shared_installation():
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
                    userId="revoked-user",
                    githubUsername="revoked",
                    githubUserId=501,
                    installationId=INSTALLATION_ID,
                ),
                GithubConnections(
                    userId="remaining-user",
                    githubUsername="remaining",
                    githubUserId=777,
                    installationId=INSTALLATION_ID,
                ),
            ]
        )
        session.commit()

        def _session():
            yield session

        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[get_authenticated_user_id] = lambda: "revoked-user"
        client = TestClient(app)
        try:
            _post_revocation(client, 501)
            _post_installation_event(client, "suspend")
            _post_installation_event(client, "unsuspend")
            status = client.get("/api/v1/github/connection")
        finally:
            client.close()
            app.dependency_overrides.clear()

        connections = session.exec(
            select(GithubConnections).order_by(GithubConnections.userId)
        ).all()
        assert [connection.userId for connection in connections] == [
            "remaining-user"
        ]
        assert connections[0].active is True
        assert _worker_job_is_authorized(
            session,
            user_id="revoked-user",
        ) is False
        assert status.status_code == 200
        assert status.json() == {"connected": False, "githubUsername": None}

    engine.dispose()


def test_suspend_unsuspend_does_not_restore_revoked_sole_user():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    GithubConnections.__table__.create(engine)
    Job.__table__.create(engine)

    with Session(engine) as session:
        session.add(
            GithubConnections(
                userId="revoked-user",
                githubUsername="revoked",
                githubUserId=501,
                installationId=INSTALLATION_ID,
            )
        )
        session.commit()

        def _session():
            yield session

        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[get_authenticated_user_id] = lambda: "revoked-user"
        client = TestClient(app)
        try:
            _post_revocation(client, 501)
            _post_installation_event(client, "suspend")
            _post_installation_event(client, "unsuspend")
            status = client.get("/api/v1/github/connection")
        finally:
            client.close()
            app.dependency_overrides.clear()

        assert session.exec(select(GithubConnections)).all() == []
        assert _worker_job_is_authorized(
            session,
            user_id="revoked-user",
        ) is False
        assert status.status_code == 200
        assert status.json() == {"connected": False, "githubUsername": None}

    engine.dispose()


@patch("app.webhooks.github.deactivate_user_connections")
def test_authorization_revoked_requires_sender_id(deactivate, client_and_session):
    client, _ = client_and_session
    body = json.dumps({"action": "revoked", "sender": {}}).encode()

    response = client.post(
        WEBHOOK_URL,
        content=body,
        headers=_signed(body, "github_app_authorization"),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}
    deactivate.assert_not_called()


@patch("app.webhooks.github.deactivate_user_connections")
def test_authorization_revocation_failure_is_retryable(
    deactivate,
    client_and_session,
):
    client, _ = client_and_session
    deactivate.side_effect = AuthorizationRevocationError()
    body = json.dumps({"action": "revoked", "sender": {"id": 501}}).encode()

    response = client.post(
        WEBHOOK_URL,
        content=body,
        headers=_signed(body, "github_app_authorization"),
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Failed to deactivate GitHub connections"
    }


@patch("app.webhooks.github.delete_installation_local_data")
def test_installation_deleted_cleans_up(delete_local, client_and_session):
    client, session = client_and_session
    body = json.dumps(
        {"action": "deleted", "installation": {"id": INSTALLATION_ID}}
    ).encode()

    response = client.post(WEBHOOK_URL, content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == "github installation deleted"
    delete_local.assert_called_once_with(session, INSTALLATION_ID)


@patch("app.webhooks.github.delete_installation_local_data")
def test_replay_is_successful(delete_local, client_and_session):
    client, _ = client_and_session
    body = json.dumps(
        {"action": "deleted", "installation": {"id": INSTALLATION_ID}}
    ).encode()
    headers = _signed(body)

    first = client.post(WEBHOOK_URL, content=body, headers=headers)
    second = client.post(WEBHOOK_URL, content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert delete_local.call_count == 2


@pytest.mark.parametrize(
    ("action", "active", "message"),
    [
        ("suspend", False, "github installation suspended"),
        ("unsuspend", True, "github installation unsuspended"),
    ],
)
@patch("app.webhooks.github.set_installation_active")
def test_installation_state_events_update_all_connections(
    set_active,
    action,
    active,
    message,
    client_and_session,
):
    client, session = client_and_session
    body = json.dumps(
        {"action": action, "installation": {"id": INSTALLATION_ID}}
    ).encode()

    response = client.post(WEBHOOK_URL, content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == message
    set_active.assert_called_once_with(
        session,
        INSTALLATION_ID,
        active=active,
    )


@patch("app.webhooks.github.set_installation_active")
def test_installation_state_failure_is_retryable(set_active, client_and_session):
    client, _ = client_and_session
    set_active.side_effect = InstallationStateError()
    body = json.dumps(
        {"action": "suspend", "installation": {"id": INSTALLATION_ID}}
    ).encode()

    response = client.post(WEBHOOK_URL, content=body, headers=_signed(body))

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to update installation state"}


@patch("app.webhooks.github.delete_installation_local_data")
def test_database_failure_is_retryable(delete_local, client_and_session, caplog):
    client, _ = client_and_session
    body = json.dumps(
        {"action": "deleted", "installation": {"id": INSTALLATION_ID}}
    ).encode()

    def fail_deletion(*_args):
        try:
            raise SQLAlchemyError("database unavailable")
        except SQLAlchemyError as error:
            raise InstallationDeletionError() from error

    delete_local.side_effect = fail_deletion

    with caplog.at_level(logging.ERROR, logger="app.webhooks.github"):
        response = client.post(WEBHOOK_URL, content=body, headers=_signed(body))

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to delete installation"}
    assert f"Installation deletion failed for installation {INSTALLATION_ID}" in caplog.text


def test_invalid_signature_is_rejected(client_and_session):
    client, _ = client_and_session
    body = json.dumps(
        {"action": "deleted", "installation": {"id": INSTALLATION_ID}}
    ).encode()

    response = client.post(
        WEBHOOK_URL,
        content=body,
        headers={
            "x-github-event": "installation",
            "x-hub-signature-256": "sha256=deadbeef",
            "content-type": "application/json",
        },
    )

    assert response.status_code == 401


def test_installation_deleted_hands_shared_ingest_to_waiting_brief_owner():
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
                    userId="departing",
                    githubUsername="departing",
                    githubUserId=501,
                    installationId=INSTALLATION_ID,
                ),
                GithubConnections(
                    userId="teammate",
                    githubUsername="teammate",
                    githubUserId=777,
                    installationId=202,
                ),
            ]
        )
        ingest = Job(
            userId="departing",
            installation_id=INSTALLATION_ID,
            repo_name="org/repo",
            ref="main",
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key="repository_ingest:org/repo:main",
            status=JobStatus.RUNNING,
            claimed_by="worker",
        )
        session.add(ingest)
        session.flush()
        brief = Job(
            userId="teammate",
            installation_id=202,
            repo_name="org/repo",
            ref="main",
            job_type=JobType.ISSUE_BRIEF,
            status=JobStatus.PENDING,
            blocked_by_job_id=ingest.id,
        )
        session.add(brief)
        session.commit()
        brief_id = brief.id

        def _session():
            yield session

        app.dependency_overrides[get_session] = _session
        client = TestClient(app)
        body = json.dumps(
            {"action": "deleted", "installation": {"id": INSTALLATION_ID}}
        ).encode()
        try:
            response = client.post(WEBHOOK_URL, content=body, headers=_signed(body))
        finally:
            client.close()
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert session.exec(
            select(Job).where(Job.userId == "departing")
        ).all() == []
        assert session.exec(
            select(Job).where(Job.installation_id == INSTALLATION_ID)
        ).all() == []
        replacement = session.exec(
            select(Job).where(
                Job.job_type == JobType.REPOSITORY_INGEST,
                Job.status == JobStatus.PENDING,
            )
        ).one()
        assert replacement.userId == "teammate"
        assert replacement.installation_id == 202
        brief = session.get(Job, brief_id)
        assert brief.status == JobStatus.PENDING
        assert brief.blocked_by_job_id == replacement.id

    engine.dispose()
