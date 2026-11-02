"""Shared repository ingests.

The shared ingest for a ``repo@ref`` belongs to nobody: it stores neither a
user nor an installation. Every requester holds their own waiting row blocked
on it, and the worker runs it under the installation of an eligible waiting
job. User- and installation-scoped updates and deletes therefore never touch
it. It only ends as ``complete`` or ``failed``; when nobody eligible is
waiting it goes back to ``pending`` and sits idle.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.models.job import Job, JobStatus, JobType
from app.services.jobs import (
    enqueue_job,
    repository_ingest_dedupe_key,
    repository_ingest_waiter_dedupe_key,
)


MISSING_SHARED_INGEST_ERROR = "shared ingest no longer exists; request it again"


def waiting_row_outcome(
    shared: Job | None,
) -> tuple[str, dict | None, str | None] | None:
    """Terminal ``(status, artifact, error)`` a waiting row inherits.

    Returns ``None`` while the shared ingest is still active.
    """
    if shared is None:
        return JobStatus.FAILED, None, MISSING_SHARED_INGEST_ERROR
    if shared.status in JobStatus.ACTIVE:
        return None
    if shared.status == JobStatus.COMPLETE:
        return JobStatus.COMPLETE, shared.artifact, None
    reason = shared.error or shared.status
    return JobStatus.FAILED, None, f"ingest failed: {reason}"


def _settle_waiting_row(
    session: Session,
    job_id: int,
    outcome: tuple[str, dict | None, str | None],
) -> None:
    status, artifact, error = outcome
    session.exec(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_(JobStatus.ACTIVE),
        )
        .values(
            status=status,
            artifact=artifact,
            error=error,
            claimed_at=None,
            claimed_by=None,
        )
    )


def enqueue_shared_ingest(
    session: Session,
    *,
    user_id: str,
    installation_id: int,
    repo_name: str,
    ref: str,
    waiting_row: bool = True,
    commit: bool = True,
) -> tuple[Job, Job, bool]:
    """Join or start the shared ingest for ``repo@ref``.

    Returns ``(requester_job, shared, created)``. With ``waiting_row`` the
    requester gets their own waiting row blocked on the shared ingest, and a
    row left over from a finished ingest is settled first so the request starts
    fresh. Without it (briefs) ``requester_job`` is the shared ingest and the
    caller blocks its own job on it in the same transaction. ``created``
    reports whether ``requester_job`` was inserted by this call, so without a
    waiting row it says whether this call started a new shared ingest rather
    than joining an active one.
    """
    waiter_key = repository_ingest_waiter_dedupe_key(
        user_id=user_id, repo_name=repo_name, ref=ref
    )
    if waiting_row:
        waiting = session.exec(
            select(Job)
            .where(
                Job.dedupe_key == waiter_key,
                Job.status.in_(JobStatus.ACTIVE),
            )
            .order_by(Job.createdAt)
        ).first()
        if waiting is not None:
            shared = (
                session.get(Job, waiting.blocked_by_job_id)
                if waiting.blocked_by_job_id is not None
                else None
            )
            outcome = waiting_row_outcome(shared)
            if outcome is None:
                return waiting, shared, False
            _settle_waiting_row(session, waiting.id, outcome)

    shared, shared_created = enqueue_job(
        session,
        user_id=None,
        installation_id=None,
        repo_name=repo_name,
        ref=ref,
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=repository_ingest_dedupe_key(repo_name=repo_name, ref=ref),
        commit=False,
    )
    if not waiting_row:
        if commit:
            session.commit()
        return shared, shared, shared_created

    requester_job, created = enqueue_job(
        session,
        user_id=user_id,
        installation_id=installation_id,
        repo_name=repo_name,
        ref=ref,
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=waiter_key,
        blocked_by_job_id=shared.id,
        commit=False,
    )
    if commit:
        session.commit()
    return requester_job, shared, created


def release_user_jobs(
    session: Session,
    user_ids: set[str],
    *,
    error: str,
    dispose: Literal["cancel", "delete"],
) -> None:
    """Cancel or delete users' active jobs.

    The shared step for every event that removes users' ability to run jobs.
    Shared ingests have no user, so they are never touched; one whose last
    eligible waiting job goes away stops at its next save point. Never
    commits; the caller owns the transaction.
    """
    if not user_ids:
        return

    if dispose == "cancel":
        session.exec(
            update(Job)
            .where(
                Job.userId.in_(user_ids),
                Job.status.in_(JobStatus.ACTIVE),
            )
            .values(
                status=JobStatus.CANCELLED,
                claimed_at=None,
                claimed_by=None,
                error=error,
            )
        )
    elif dispose == "delete":
        session.exec(delete(Job).where(Job.userId.in_(user_ids)))
    else:
        raise ValueError(f"Unknown dispose mode: {dispose!r}")


def move_user_jobs_to_installation(
    session: Session,
    *,
    user_id: str,
    installation_id: int,
) -> None:
    """Point a reconnected user's active jobs at their new installation.

    Running jobs return to the queue without spending an attempt; their
    current worker loses its claim and discards its outcome.
    """
    session.exec(
        update(Job)
        .where(
            Job.userId == user_id,
            Job.status == JobStatus.PENDING,
        )
        .values(installation_id=installation_id)
    )
    session.exec(
        update(Job)
        .where(
            Job.userId == user_id,
            Job.status == JobStatus.RUNNING,
        )
        .values(
            installation_id=installation_id,
            status=JobStatus.PENDING,
            claimed_at=None,
            claimed_by=None,
            attempts=Job.attempts - 1,
        )
    )
