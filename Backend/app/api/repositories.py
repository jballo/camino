from fastapi import APIRouter, Depends, HTTPException
from github import Auth, GithubException, GithubIntegration
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc, func
from sqlmodel import select

import logging

from app.config import settings
from app.services.embeddings import EmbeddingError
from app.models.code import CodeChunkModel
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobType
from app.rate_limit import (
    REPOSITORY_INGEST_RATE_LIMIT,
    REPOSITORY_SEARCH_RATE_LIMIT,
)
from app.security import get_authenticated_user_id
from app.services.jobs import enqueue_job, repository_ingest_dedupe_key
from app.services.search import hybrid_search

logger = logging.getLogger(__name__)

router = APIRouter()

class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    repoName: str
    limit: int = Field(default=10, ge=1, le=100)


class SearchResultResponse(BaseModel):
    chunk_id: int
    repo_name: str
    file_path: str
    symbol_name: str
    symbol_type: str
    language: str
    start_line: int
    end_line: int
    source_code: str
    signature: str
    docstring: str | None
    score: float



class RepoIngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repoName: str


class RepoIngestJobResponse(BaseModel):
    id: int
    status: str


class RepoIngestStatusResponse(BaseModel):
    id: int
    status: str
    repoName: str
    attempts: int
    result: dict | None = None
    error: str | None = None


@router.get("")
async def list_repositories(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[str]:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id
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
        return [repo.full_name for repo in repos]
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")


@router.get("/processed")
async def list_processed_repositories(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[dict]:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        rows = session.exec(
            select(
                CodeChunkModel.repo_name,
                func.count(CodeChunkModel.id),
            )
            .where(CodeChunkModel.installation_id == gh_connection.installationId)
            .group_by(CodeChunkModel.repo_name)
        ).all()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    return [{"repo_name": repo_name, "chunk_count": count} for repo_name, count in rows]


@router.post("/ingest", dependencies=[Depends(REPOSITORY_INGEST_RATE_LIMIT)])
async def process_repository(
    payload: RepoIngestBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestJobResponse:
    try:
        gh_connection = session.exec(
            select(GithubConnections).where(
                GithubConnections.userId == auth_user_id
            )
        ).one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        job, created = enqueue_job(
            session,
            user_id=auth_user_id,
            installation_id=gh_connection.installationId,
            repo_name=payload.repoName,
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key=repository_ingest_dedupe_key(
                installation_id=gh_connection.installationId,
                repo_name=payload.repoName,
            ),
        )
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "repository ingest job %s | id=%s repo=%r",
        "queued" if created else "deduplicated",
        job.id,
        job.repo_name,
    )
    return RepoIngestJobResponse(id=job.id, status=job.status)


@router.get("/ingest/{job_id}")
async def get_repository_ingest(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestStatusResponse:
    try:
        job = session.get(Job, job_id)
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job is None or job.job_type != JobType.REPOSITORY_INGEST:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    if job.userId != auth_user_id:
        try:
            connection = session.exec(
                select(GithubConnections).where(
                    GithubConnections.userId == auth_user_id,
                    GithubConnections.installationId == job.installation_id,
                )
            ).first()
        except exc.SQLAlchemyError:
            session.rollback()
            raise HTTPException(status_code=500, detail="Database error")
        if connection is None:
            raise HTTPException(status_code=403, detail="Forbidden")

    return RepoIngestStatusResponse(
        id=job.id,
        status=job.status,
        repoName=job.repo_name,
        attempts=job.attempts,
        result=job.artifact,
        error=job.error,
    )


@router.post("/search", dependencies=[Depends(REPOSITORY_SEARCH_RATE_LIMIT)])
async def search_repository(
    payload: SearchBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> list[SearchResultResponse]:
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == auth_user_id
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        results = await hybrid_search(
            session,
            payload.query,
            payload.repoName,
            installation_id=gh_connection.installationId,
            limit=payload.limit,
        )
    except EmbeddingError as e:
        logger.error(
            "Embedding service failed during search: %s | query=%r repo=%r",
            e, payload.query, payload.repoName,
        )
        raise HTTPException(status_code=502, detail="Embedding service unavailable")
    except exc.SQLAlchemyError as e:
        session.rollback()
        logger.error(
            "Database error during search: %s | query=%r repo=%r",
            e, payload.query, payload.repoName,
        )
        raise HTTPException(status_code=502, detail="Database service error")
    except Exception as e:
        logger.exception(
            "Unexpected error during search | query=%r repo=%r",
            payload.query, payload.repoName,
        )
        raise HTTPException(status_code=500, detail="Internal search error")

    if not results:
        return []
    return [
        SearchResultResponse(
            chunk_id=r.chunk_id,
            repo_name=r.repo_name,
            file_path=r.file_path,
            symbol_name=r.symbol_name,
            symbol_type=r.symbol_type,
            language=r.language,
            start_line=r.start_line,
            end_line=r.end_line,
            source_code=r.source_code,
            signature=r.signature,
            docstring=r.docstring,
            score=r.score,
        )
        for r in results
    ]