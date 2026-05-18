import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import exc
from sqlmodel import select

from app.config import settings
from app.db import SessionDep
from app.models.github_connection import GithubConnections


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
            statement = select(GithubConnections).where(
                GithubConnections.installationId == installation_id
            )
            connection = session.exec(statement).one()
            session.delete(connection)
            session.commit()
            return "github connection deleted"
        except exc.NoResultFound:
            session.rollback()
            raise HTTPException(status_code=404, detail="User not found")
        except exc.MultipleResultsFound:
            session.rollback()
            raise HTTPException(status_code=500, detail="Duplicate installation")
        except exc.IntegrityError:
            session.rollback()
            raise HTTPException(status_code=500, detail="Failed to delete user")
        except exc.OperationalError:
            session.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return "Unknown event"
