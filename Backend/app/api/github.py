import datetime as dt
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
from app.security import encrypt_token, get_authenticated_user_id


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
            GithubConnections.userId == auth_user_id
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
        access_token: str = access_token_obj.token
        expires_in: int | None = access_token_obj.expires_in
        refresh_token: str | None = access_token_obj.refresh_token
        refresh_expires_in: int | None = access_token_obj.refresh_expires_in
        created_at: dt.datetime = access_token_obj.created
    except BadCredentialsException:
        raise HTTPException(status_code=400, detail="Invalid Github code")
    except RateLimitExceededException:
        raise HTTPException(status_code=429, detail="GitHub rate limit exceeded")
    except GithubException as e:
        if e.status in (400, 401, 403):
            raise HTTPException(status_code=400, detail="Invalid Github code")
        raise HTTPException(status_code=502, detail="Github error")

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

    encrypted_access_token = encrypt_token(access_token)
    encrypted_refresh_token = encrypt_token(refresh_token)

    try:
        token_expires_at = created_at + dt.timedelta(seconds=expires_in)
        refresh_token_expires_at = created_at + dt.timedelta(seconds=refresh_expires_in)

        existing = session.exec(
            select(GithubConnections).where(
                GithubConnections.userId == auth_user_id
            )
        ).one_or_none()

        if existing is not None:
            existing.githubUsername = username
            existing.githubUserId = github_user_id
            existing.installationId = payload.installationId
            existing.encryptedAccessToken = encrypted_access_token
            existing.encryptedRefreshToken = encrypted_refresh_token
            existing.tokenExpiresAt = token_expires_at
            existing.refreshTokenExpiresAt = refresh_token_expires_at
            session.add(existing)
            session.commit()
            return "Successfully updated github connection"

        connection = GithubConnections(
            userId=auth_user_id,
            githubUsername=username,
            githubUserId=github_user_id,
            installationId=payload.installationId,
            encryptedAccessToken=encrypted_access_token,
            encryptedRefreshToken=encrypted_refresh_token,
            tokenExpiresAt=token_expires_at,
            refreshTokenExpiresAt=refresh_token_expires_at,
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
