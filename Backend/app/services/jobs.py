from __future__ import annotations

from sqlalchemy import exc, update
from sqlmodel import Session, select

from app.models.job import Job, JobStatus, JobType


def normalize_repository_name(repo_name: str) -> str:
    """Return the stable, case-insensitive identity used for a GitHub repository."""
    return repo_name.casefold()


def tour_dedupe_key(
    *, user_id: str, repo_name: str, ref: str, topic: str
) -> str:
    normalized_repo_name = normalize_repository_name(repo_name)
    return (
        f"{JobType.TOUR}:{user_id}:{normalized_repo_name}:{ref}:{topic}"
    )


def repository_ingest_dedupe_key(
    *, repo_name: str, ref: str
) -> str:
    normalized_repo_name = normalize_repository_name(repo_name)
    return f"{JobType.REPOSITORY_INGEST}:{normalized_repo_name}:{ref}"


def issue_brief_dedupe_key(
    *,
    user_id: str,
    repo_name: str,
    ref: str,
    issue_repo: str,
    issue_number: int,
) -> str:
    normalized_repo_name = normalize_repository_name(repo_name)
    normalized_issue_repo = normalize_repository_name(issue_repo)
    return (
        f"{JobType.ISSUE_BRIEF}:{user_id}:{normalized_repo_name}:"
        f"{ref}:{normalized_issue_repo}:{issue_number}"
    )


def cancel_job(session: Session, job_id: int) -> bool:
    """Atomically cancel a pending or running job. Returns True if it transitioned."""
    result = session.exec(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_(JobStatus.ACTIVE),
        )
        .values(
            status=JobStatus.CANCELLED,
            claimed_at=None,
            claimed_by=None,
        )
    )
    session.commit()
    return result.rowcount == 1


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
    ref: str,
    job_type: str,
    dedupe_key: str,
    topic: str | None = None,
    issue_repo: str | None = None,
    issue_number: int | None = None,
    blocked_by_job_id: int | None = None,
) -> tuple[Job, bool]:
    """Return the active equivalent job, or atomically enqueue a new one.

    The pre-insert lookup handles the common case. The partial unique index on
    ``dedupe_key`` closes the concurrent-enqueue race; its loser reloads the row
    inserted by the winner.
    """
    repo_name = normalize_repository_name(repo_name)
    existing = _active_job(session, dedupe_key)
    if existing is not None:
        return existing, False

    job = Job(
        userId=user_id,
        installation_id=installation_id,
        repo_name=repo_name,
        ref=ref,
        job_type=job_type,
        dedupe_key=dedupe_key,
        topic=topic,
        issue_repo=(
            normalize_repository_name(issue_repo) if issue_repo is not None else None
        ),
        issue_number=issue_number,
        blocked_by_job_id=blocked_by_job_id,
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
