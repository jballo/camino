import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc
from sqlmodel import select

from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.rate_limit import JOURNEY_CREATE_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.jobs import (
    cancel_job,
    enqueue_job,
    normalize_repository_name,
    tour_dedupe_key,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateJourneyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repoName: str
    topic: str = Field(min_length=1, max_length=500)


class JourneyCreatedResponse(BaseModel):
    id: int
    status: str


class JourneyResponse(BaseModel):
    id: int
    status: str
    repoName: str
    topic: str
    artifact: dict | None = None
    error: str | None = None


class JourneySummaryResponse(BaseModel):
    id: int
    status: str
    repoName: str
    topic: str
    createdAt: dt.datetime


def _get_authorized_journey(
    session: SessionDep,
    job_id: int,
    auth_user_id: str,
) -> Job:
    try:
        job = session.get(Job, job_id)
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    if job is None:
        raise HTTPException(status_code=404, detail="Journey not found")
    if job.userId != auth_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if job.job_type != JobType.TOUR:
        raise HTTPException(status_code=404, detail="Journey not found")
    return job


def _journey_response(job: Job) -> JourneyResponse:
    return JourneyResponse(
        id=job.id,
        status=job.status,
        repoName=job.repo_name,
        topic=job.topic,
        artifact=job.artifact,
        error=job.error,
    )


@router.post("", dependencies=[Depends(JOURNEY_CREATE_RATE_LIMIT)])
async def create_journey(
    payload: CreateJourneyBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> JourneyCreatedResponse:
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
            job_type=JobType.TOUR,
            dedupe_key=tour_dedupe_key(
                user_id=auth_user_id,
                installation_id=gh_connection.installationId,
                repo_name=payload.repoName,
                topic=payload.topic,
            ),
            topic=payload.topic,
        )
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "tour job %s | id=%s topic=%r repo=%r",
        "queued" if created else "deduplicated",
        job.id,
        payload.topic,
        payload.repoName,
    )
    return JourneyCreatedResponse(id=job.id, status=job.status)


@router.get("/{job_id}")
async def get_journey(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> JourneyResponse:
    job = _get_authorized_journey(session, job_id, auth_user_id)
    return _journey_response(job)


@router.post("/{job_id}/cancel")
async def cancel_journey(
    job_id: int,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> JourneyResponse:
    job = _get_authorized_journey(session, job_id, auth_user_id)

    if job.status == JobStatus.CANCELLED:
        return _journey_response(job)
    if job.status not in JobStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel journey with status '{job.status}'",
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
            detail=f"Cannot cancel journey with status '{job.status}'",
        )
    return _journey_response(job)


@router.get("")
async def list_journeys(
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
    repo: str | None = None,
) -> list[JourneySummaryResponse]:
    statement = select(Job).where(
        Job.userId == auth_user_id,
        Job.job_type == JobType.TOUR,
    )
    if repo:
        statement = statement.where(
            Job.repo_name == normalize_repository_name(repo)
        )
    statement = statement.order_by(Job.createdAt.desc())

    try:
        jobs = session.exec(statement).all()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    return [
        JourneySummaryResponse(
            id=job.id,
            status=job.status,
            repoName=job.repo_name,
            topic=job.topic,
            createdAt=job.createdAt,
        )
        for job in jobs
    ]
