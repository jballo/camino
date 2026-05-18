from fastapi import APIRouter, Depends, HTTPException
from github import Auth, GithubException, GithubIntegration
from pydantic import BaseModel
from sqlalchemy import exc
from sqlmodel import select

import os

from app.config import settings
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.security import verify_api_key
from app.services.parser import LANGUAGES, MAX_FILE_BYTES, SKIP_DIRS, parse_file


router = APIRouter()


class RepoIngestBody(BaseModel):
    repoName: str
    userId: str


@router.get("/{userId}", dependencies=[Depends(verify_api_key)])
async def list_repositories(userId: str, session: SessionDep) -> list[str]:
    try:
        statement = select(GithubConnections).where(GithubConnections.userId == userId)
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id, private_key=settings.gh_app_private_key
        )
        gi = GithubIntegration(auth=app_auth)
        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()
        return [repo.full_name for repo in repos]
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")


@router.post("/ingest", dependencies=[Depends(verify_api_key)])
async def process_repository(payload: RepoIngestBody, session: SessionDep):
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == payload.userId
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id, private_key=settings.gh_app_private_key
        )
        gi = GithubIntegration(auth=app_auth)
        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()

        repo_selected = None
        for repo in repos:
            if repo.full_name == payload.repoName:
                repo_selected = repo
                break

        if repo_selected is None:
            raise HTTPException(status_code=404, detail="Repo not found")

        contents = repo_selected.get_contents("")
        all_chunks = []
        while contents:
            file_content = contents.pop(0)

            path_parts = file_content.path.replace("\\", "/").split("/")
            if any(part in SKIP_DIRS for part in path_parts):
                continue

            if file_content.type == "dir":
                contents.extend(repo_selected.get_contents(file_content.path))
                continue

            if file_content.type != "file":
                continue

            ext = os.path.splitext(file_content.path)[1]
            if ext not in LANGUAGES:
                continue

            if file_content.size and file_content.size > MAX_FILE_BYTES:
                continue

            try:
                source_bytes = file_content.decoded_content
            except AssertionError:
                continue

            if not source_bytes:
                continue

            chunks = parse_file(file_content.path, source_bytes)
            all_chunks.extend(chunks)

        return [
            {
                "symbol_name": c.symbol_name,
                "symbol_type": c.symbol_type,
                "file_path": c.file_path,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "language": c.language,
            }
            for c in all_chunks
        ]
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")
