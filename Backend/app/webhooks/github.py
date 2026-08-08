import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db import SessionDep
from app.services.authorization_revocation import (
    AuthorizationRevocationError,
    delete_revoked_user_connections,
)
from app.services.installation_deletion import (
    InstallationDeletionError,
    delete_installation_local_data,
)


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("")
async def github_webhook_handler(request: Request, session: SessionDep):
    headers = request.headers
    gh_event = headers.get("x-github-event", "")
    payload = await request.body()

    signature_header = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(
        settings.gh_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = parsed_payload.get("action")

    if gh_event == "installation":
        installation = parsed_payload.get("installation")
        if installation is None:
            raise HTTPException(status_code=400, detail="Invalid request")
        installation_id = installation.get("id")
        if installation_id is None:
            raise HTTPException(status_code=400, detail="Invalid request")

        if action == "deleted":
            try:
                delete_installation_local_data(session, installation_id)
                return "github installation deleted"
            except InstallationDeletionError:
                logger.exception(
                    "Installation deletion failed for installation %s",
                    installation_id,
                )
                raise HTTPException(
                    status_code=500, detail="Failed to delete installation"
                )

    if gh_event == "github_app_authorization" and action == "revoked":
        sender = parsed_payload.get("sender")
        if sender is None:
            raise HTTPException(status_code=400, detail="Invalid request")
        github_user_id = sender.get("id")
        if github_user_id is None:
            raise HTTPException(status_code=400, detail="Invalid request")

        try:
            delete_revoked_user_connections(session, github_user_id)
            return "github authorization revoked"
        except AuthorizationRevocationError:
            logger.exception(
                "Authorization revocation failed for GitHub user %s",
                github_user_id,
            )
            raise HTTPException(
                status_code=500, detail="Failed to revoke authorization"
            )

    return "Unknown event"
