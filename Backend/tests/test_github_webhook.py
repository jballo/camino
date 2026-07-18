import hashlib
import hmac
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import get_session
from app.main import app
from app.services.installation_deletion import InstallationDeletionError


WEBHOOK_URL = "/webhooks/github"
INSTALLATION_ID = 101


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(
        settings.gh_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-github-event": "installation",
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
