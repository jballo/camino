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
from app.models.job import Job, JobStatus
from app.services.authorization_revocation import AuthorizationRevocationError
from app.services.installation_deletion import InstallationDeletionError
from app.services.installation_state import InstallationStateError


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


def test_authorization_revoked_deactivates_only_matching_connections():
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
        assert [connection.active for connection in connections] == [False, False, True]
        jobs = session.exec(select(Job).order_by(Job.userId)).all()
        assert jobs[0].status == JobStatus.CANCELLED
        assert jobs[0].error == "GitHub authorization revoked"
        assert jobs[1].status == JobStatus.RUNNING

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
