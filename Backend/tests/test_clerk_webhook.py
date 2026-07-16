import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from svix.webhooks import WebhookVerificationError

from app.db import get_session
from app.main import app
from app.services.account_deletion import AccountDeletionError


WEBHOOK_URL = "/webhooks/clerk"
USER_ID = "user_123"


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
