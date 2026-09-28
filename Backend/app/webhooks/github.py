import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db import SessionDep
from app.services.installation_deletion import (
    InstallationDeletionError,
    delete_installation_local_data,
)
from app.services.installation_state import (
    InstallationStateError,
    set_installation_active,
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

    installation = parsed_payload.get("installation")
    if installation is None:
        raise HTTPException(status_code=400, detail="Invalid request")
    installation_id = installation.get("id")
    if installation_id is None:
        raise HTTPException(status_code=400, detail="Invalid request")
    installation_event = parsed_payload.get("action")

    if gh_event == "installation" and installation_event == "deleted":
        try:
            delete_installation_local_data(session, installation_id)
            return "github installation deleted"
        except InstallationDeletionError:
            logger.exception(
                "Installation deletion failed for installation %s", installation_id
            )
            raise HTTPException(
                status_code=500, detail="Failed to delete installation"
            )

    if gh_event == "installation" and installation_event in {"suspend", "unsuspend"}:
        active = installation_event == "unsuspend"
        try:
            set_installation_active(
                session,
                installation_id,
                active=active,
            )
            return f"github installation {installation_event}ed"
        except InstallationStateError:
            logger.exception(
                "Installation state update failed for installation %s",
                installation_id,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to update installation state",
            )

    return "Unknown event"
