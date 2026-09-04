from __future__ import annotations

from sqlalchemy import exc
from sqlmodel import Session, select

from app.models.job import Job, JobStatus, JobType


def tour_dedupe_key(
    *, user_id: str, installation_id: int, repo_name: str, topic: str
) -> str:
    return f"{JobType.TOUR}:{user_id}:{installation_id}:{repo_name}:{topic}"


def repository_ingest_dedupe_key(
    *, installation_id: int, repo_name: str
) -> str:
    return f"{JobType.REPOSITORY_INGEST}:{installation_id}:{repo_name}"


def _active_job(session: Session, dedupe_key: str) -> Job | None:
    return session.exec(
        select(Job)
        .where(
            Job.dedupe_key == dedupe_key,
            Job.status.in_(JobStatus.ACTIVE),
        )
        .order_by(Job.createdAt)
    ).first()


def enqueue_job(
    session: Session,
    *,
    user_id: str,
    installation_id: int,
    repo_name: str,
    job_type: str,
    dedupe_key: str,
    topic: str | None = None,
) -> tuple[Job, bool]:
    """Return the active equivalent job, or atomically enqueue a new one.

    The pre-insert lookup handles the common case. The partial unique index on
    ``dedupe_key`` closes the concurrent-enqueue race; its loser reloads the row
    inserted by the winner.
    """
    existing = _active_job(session, dedupe_key)
    if existing is not None:
        return existing, False

    job = Job(
        userId=user_id,
        installation_id=installation_id,
        repo_name=repo_name,
        job_type=job_type,
        dedupe_key=dedupe_key,
        topic=topic,
        status=JobStatus.PENDING,
    )
    try:
        session.add(job)
        session.commit()
        session.refresh(job)
        return job, True
    except exc.IntegrityError:
        session.rollback()
        existing = _active_job(session, dedupe_key)
        if existing is None:
            raise
        return existing, False
