import logging

from fastapi import APIRouter, Depends, HTTPException
from github import (
    AccessToken,
    Auth,
    BadCredentialsException,
    Github,
    GithubException,
    RateLimitExceededException,
)
from psycopg2.errorcodes import UNIQUE_VIOLATION
from pydantic import BaseModel, ConfigDict
from sqlalchemy import exc
from sqlmodel import select

from app.config import settings
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.security import get_authenticated_user_id
from app.services.installation_state import SUSPENSION_ERROR
from app.services.shared_ingests import (
    move_user_jobs_to_installation,
    release_user_jobs,
)


logger = logging.getLogger(__name__)
router = APIRouter()


class GithubConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    installationId: int


class GithubConnectionStatus(BaseModel):
    connected: bool
    githubUsername: str | None = None


@router.get("/connection")
async def get_github_connection(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> GithubConnectionStatus:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id,
            GithubConnections.active.is_(True),
        )
        connection = session.exec(statement).one_or_none()
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if connection is None:
        return GithubConnectionStatus(connected=False)
    return GithubConnectionStatus(
        connected=True, githubUsername=connection.githubUsername
    )


@router.post("/connect")
async def add_github_connection(
    payload: GithubConnectBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> str:
    try:
        g = Github()
        oauth_app = g.get_oauth_application(
            settings.gh_app_client_id, settings.gh_app_secret
        )
        access_token_obj: AccessToken = oauth_app.get_access_token(payload.code)
        g = Github(
            auth=Auth.AppUserAuth(
                client_id=settings.gh_app_client_id,
                client_secret=settings.gh_app_secret,
                token=access_token_obj.token,
            )
        )
        github_user = g.get_user()
        username = github_user.login
        github_user_id = github_user.id
        expires_in: int | None = access_token_obj.expires_in
        refresh_token: str | None = access_token_obj.refresh_token
        refresh_expires_in: int | None = access_token_obj.refresh_expires_in
    except BadCredentialsException:
        raise HTTPException(status_code=400, detail="Invalid Github code")
    except RateLimitExceededException:
        raise HTTPException(status_code=429, detail="GitHub rate limit exceeded")
    except GithubException as e:
        if e.status in (400, 401, 403):
            raise HTTPException(status_code=400, detail="Invalid Github code")
        raise HTTPException(status_code=502, detail="Github error")

    try:
        installation = next(
            (
                candidate
                for candidate in github_user.get_installations()
                if candidate.id == payload.installationId
            ),
            None,
        )
    except GithubException:
        raise HTTPException(status_code=502, detail="Github error")
    if installation is None:
        raise HTTPException(
            status_code=403,
            detail="Installation not accessible to this GitHub user",
        )
    installation_is_active = getattr(installation, "suspended_at", None) is None

    if (
        refresh_token is None
        or expires_in is None
        or refresh_expires_in is None
        or expires_in <= 0
        or refresh_expires_in <= 0
    ):
        raise HTTPException(
            status_code=502,
            detail="Github returned a non-expiring or already-expired token. Expected an expiring user-to-server token",
        )

    try:
        existing = session.exec(
            select(GithubConnections)
            .where(GithubConnections.userId == auth_user_id)
            .with_for_update()
        ).one_or_none()

        if existing is not None:
            previous_installation_id = existing.installationId
            existing.githubUsername = username
            existing.githubUserId = github_user_id
            existing.installationId = payload.installationId
            existing.active = installation_is_active
            session.add(existing)
            session.flush()
            if not installation_is_active:
                release_user_jobs(
                    session,
                    {auth_user_id},
                    error=SUSPENSION_ERROR,
                    dispose="cancel",
                )
            elif previous_installation_id != payload.installationId:
                move_user_jobs_to_installation(
                    session,
                    user_id=auth_user_id,
                    installation_id=payload.installationId,
                )
            session.commit()
            return "Successfully updated github connection"

        connection = GithubConnections(
            userId=auth_user_id,
            githubUsername=username,
            githubUserId=github_user_id,
            installationId=payload.installationId,
            active=installation_is_active,
        )
        session.add(connection)
        session.commit()
        session.refresh(connection)
        return "Successfully added github connection"
    except exc.IntegrityError as e:
        session.rollback()
        pgcode = getattr(getattr(e, "orig", None), "pgcode", None)
        if pgcode == UNIQUE_VIOLATION:
            raise HTTPException(status_code=409, detail="Already connected")
        logger.exception("Failed to persist GitHub connection")
        raise HTTPException(status_code=500, detail="Database error")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")
