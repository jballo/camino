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
from svix.webhooks import WebhookVerificationError

from app.db import get_session
from app.main import app
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.models.rate_limit import RateLimit
from app.models.repo_follow import UserRepoFollow
from app.models.user import User
from app.services.account_deletion import AccountDeletionError


WEBHOOK_URL = "/webhooks/clerk"
USER_ID = "user_123"


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def client_and_session():
    session = MagicMock()

    def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    yield TestClient(app), session
    app.dependency_overrides.clear()


@patch("app.webhooks.clerk.delete_local_account_data")
@patch("app.webhooks.clerk.Webhook")
def test_deleted_user_runs_full_cleanup(
    webhook_class, delete_local, client_and_session
):
    client, session = client_and_session
    webhook_class.return_value.verify.return_value = {
        "type": "user.deleted",
        "data": {"id": USER_ID},
    }

    response = client.post(WEBHOOK_URL, content=b"payload")

    assert response.status_code == 200
    assert response.json() == "user deleted"
    delete_local.assert_called_once_with(session, USER_ID)


@patch("app.webhooks.clerk.delete_local_account_data")
@patch("app.webhooks.clerk.Webhook")
def test_duplicate_deleted_user_delivery_is_successful(
    webhook_class, delete_local, client_and_session
):
    client, _ = client_and_session
    webhook_class.return_value.verify.return_value = {
        "type": "user.deleted",
        "data": {"id": USER_ID},
    }

    first = client.post(WEBHOOK_URL, content=b"payload")
    second = client.post(WEBHOOK_URL, content=b"payload")

    assert first.status_code == 200
    assert second.status_code == 200
    assert delete_local.call_count == 2


@patch("app.webhooks.clerk.delete_local_account_data")
@patch("app.webhooks.clerk.Webhook")
def test_database_failure_remains_retryable(
    webhook_class, delete_local, client_and_session, caplog
):
    client, _ = client_and_session
    webhook_class.return_value.verify.return_value = {
        "type": "user.deleted",
        "data": {"id": USER_ID},
    }

    def fail_deletion(*_args):
        try:
            raise SQLAlchemyError("database unavailable")
        except SQLAlchemyError as error:
            raise AccountDeletionError() from error

    delete_local.side_effect = fail_deletion

    with caplog.at_level(logging.ERROR, logger="app.webhooks.clerk"):
        response = client.post(WEBHOOK_URL, content=b"payload")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to delete user"}
    assert f"Account deletion failed for user {USER_ID}" in caplog.text
    assert "database unavailable" in caplog.text


@patch("app.webhooks.clerk.Webhook")
def test_invalid_signature_is_rejected(webhook_class, client_and_session):
    client, _ = client_and_session
    webhook_class.return_value.verify.side_effect = WebhookVerificationError(
        "invalid signature"
    )

    response = client.post(WEBHOOK_URL, content=b"payload")

    assert response.status_code == 400


@patch("app.webhooks.clerk.Webhook")
def test_deleted_user_hands_shared_ingest_to_waiting_brief_owner(webhook_class):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for model in (GithubConnections, Job, RateLimit, UserRepoFollow, User):
        model.__table__.create(engine)
    webhook_class.return_value.verify.return_value = {
        "type": "user.deleted",
        "data": {"id": USER_ID},
    }

    with Session(engine) as session:
        session.add_all(
            [
                GithubConnections(
                    userId=USER_ID,
                    githubUsername="departing",
                    githubUserId=501,
                    installationId=101,
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
            userId=USER_ID,
            installation_id=101,
            repo_name="org/repo",
            ref="main",
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key="repository_ingest:org/repo:main",
            status=JobStatus.PENDING,
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
        own_brief = Job(
            userId=USER_ID,
            installation_id=101,
            repo_name="org/repo",
            ref="main",
            job_type=JobType.ISSUE_BRIEF,
            status=JobStatus.PENDING,
            blocked_by_job_id=ingest.id,
        )
        session.add_all([brief, own_brief])
        session.commit()
        ingest_id, brief_id = ingest.id, brief.id

        def _session():
            yield session

        app.dependency_overrides[get_session] = _session
        client = TestClient(app)
        try:
            response = client.post(WEBHOOK_URL, content=b"payload")
        finally:
            client.close()
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert session.exec(select(Job).where(Job.userId == USER_ID)).all() == []
        ingest = session.get(Job, ingest_id)
        assert ingest.userId == "teammate"
        assert ingest.installation_id == 202
        assert ingest.status == JobStatus.PENDING
        brief = session.get(Job, brief_id)
        assert brief.status == JobStatus.PENDING
        assert brief.blocked_by_job_id == ingest_id

    engine.dispose()
