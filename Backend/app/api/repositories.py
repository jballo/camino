from fastapi import APIRouter, Depends, HTTPException
from github import Auth, GithubException, GithubIntegration
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc, text
from sqlmodel import Session, select

import logging

from app.config import settings
from app.services.embeddings import EmbeddingError
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import (
    REPOSITORY_INGEST_RATE_LIMIT,
    REPOSITORY_SEARCH_RATE_LIMIT,
)
from app.security import get_authenticated_user_id
from app.services.jobs import (
    cancel_job,
    enqueue_job,
    normalize_repository_name,
    repository_ingest_dedupe_key,
)
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


def _get_authorized_repository_ingest(
    session: Session,
    job_id: int,
    auth_user_id: str,
) -> Job:
    try:
        job = session.get(Job, job_id)
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job is None or job.job_type != JobType.REPOSITORY_INGEST:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    if job.userId == auth_user_id:
        return job

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
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id,
            private_key=settings.gh_app_private_key,
        )
        installation = GithubIntegration(
            auth=app_auth
        ).get_app_installation(connection.installationId)
        target_repo = normalize_repository_name(job.repo_name)
        repository_is_accessible = any(
            normalize_repository_name(repo.full_name) == target_repo
            for repo in installation.get_repos()
        )
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")

    if not repository_is_accessible:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    return job


def _repository_ingest_response(job: Job) -> RepoIngestStatusResponse:
    return RepoIngestStatusResponse(
        id=job.id,
        status=job.status,
        repoName=job.repo_name,
        attempts=job.attempts,
        result=job.artifact,
        error=job.error,
    )


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
        rows = session.execute(
            text("""
                SELECT repo_name, count(id) AS chunk_count
                FROM live_code_chunks
                WHERE installation_id = :installation_id
                GROUP BY repo_name
            """),
            {"installation_id": gh_connection.installationId},
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
    job = _get_authorized_repository_ingest(session, job_id, auth_user_id)
    return _repository_ingest_response(job)


@router.post("/ingest/{job_id}/cancel")
async def cancel_repository_ingest(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> RepoIngestStatusResponse:
    job = _get_authorized_repository_ingest(session, job_id, auth_user_id)
    if job.userId != auth_user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the job owner can cancel this ingestion",
        )

    if job.status == JobStatus.CANCELLED:
        return _repository_ingest_response(job)
    if job.status not in JobStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel ingestion job with status '{job.status}'",
        )

    try:
        cancel_job(session, job_id)
        session.refresh(job)
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job.status != JobStatus.CANCELLED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel ingestion job with status '{job.status}'",
        )
    return _repository_ingest_response(job)


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