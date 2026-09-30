from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services.jobs import (
    enqueue_job,
    repository_ingest_dedupe_key,
    repository_ingest_waiter_dedupe_key,
)


class SharedIngestRaceError(Exception):
    """Raised when a primary ingest keeps ending between lookup and lock."""


def _lock_job(session: Session, job_id: int) -> Job | None:
    """Row-lock a job and refresh the identity-mapped instance."""
    return session.exec(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


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

    Returns ``(requester_job, primary, created)``. The primary holds the global
    dedupe key and does the work. A requester who does not own it gets a
    waiting row blocked on it when ``waiting_row`` is set, so handoff finds them
    if the owner loses access; otherwise ``requester_job`` is the primary.
    ``created`` reports whether ``requester_job`` was inserted by this call.
    """
    waiter_key = repository_ingest_waiter_dedupe_key(
        user_id=user_id, repo_name=repo_name, ref=ref
    )
    waiting = session.exec(
        select(Job)
        .where(
            Job.dedupe_key == waiter_key,
            Job.status.in_(JobStatus.ACTIVE),
        )
        .order_by(Job.createdAt)
    ).first()
    if waiting is not None:
        primary = (
            session.get(Job, waiting.blocked_by_job_id)
            if waiting.blocked_by_job_id is not None
            else None
        )
        return waiting, primary or waiting, False

    primary_key = repository_ingest_dedupe_key(repo_name=repo_name, ref=ref)
    for _ in range(2):
        primary, created = enqueue_job(
            session,
            user_id=user_id,
            installation_id=installation_id,
            repo_name=repo_name,
            ref=ref,
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key=primary_key,
            commit=False,
        )
        if created or primary.userId == user_id:
            if commit:
                session.commit()
            return primary, primary, created

        # Handoff and owner cancel lock the primary before choosing a new
        # owner. Holding the same lock until the waiting row exists means they
        # either see this requester or finished before the lock was granted.
        locked = _lock_job(session, primary.id)
        if locked is not None and locked.status in JobStatus.ACTIVE:
            break
    else:
        raise SharedIngestRaceError(
            f"Primary ingest for {repo_name}@{ref} ended during enqueue"
        )

    if not waiting_row:
        if commit:
            session.commit()
        return locked, locked, False

    requester_job, created = enqueue_job(
        session,
        user_id=user_id,
        installation_id=installation_id,
        repo_name=repo_name,
        ref=ref,
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=waiter_key,
        blocked_by_job_id=locked.id,
        commit=False,
    )
    if commit:
        session.commit()
    return requester_job, locked, created


def _active_other_user_dependents(
    session: Session,
    *,
    ingest: Job,
    sidelined_user_ids: Collection[str],
) -> list[Job]:
    """Return authorized active dependents in stable ownership order.

    Locks each candidate's dependent job and connection row so a concurrent
    removal cannot sideline the chosen owner before this transaction commits.
    Connections are locked first, matching the removal handlers, which update
    a user's connection before touching their jobs.
    """
    candidate_filter = (
        Job.blocked_by_job_id == ingest.id,
        Job.status.in_(JobStatus.ACTIVE),
        Job.userId.notin_(sidelined_user_ids),
        GithubConnections.active.is_(True),
    )
    join_condition = (GithubConnections.userId == Job.userId) & (
        GithubConnections.installationId == Job.installation_id
    )
    session.exec(
        select(GithubConnections.id)
        .join(Job, join_condition)
        .where(*candidate_filter)
        .order_by(GithubConnections.id)
        .with_for_update(of=GithubConnections)
    ).all()
    statement = (
        select(Job)
        .join(GithubConnections, join_condition)
        .where(*candidate_filter)
        .order_by(Job.createdAt, Job.id)
        .with_for_update(of=[Job, GithubConnections])
    )
    return list(session.exec(statement).all())


def _hand_over(
    session: Session,
    ingest: Job,
    dependents: list[Job],
    *,
    error: str | None,
    replace_pending: bool,
) -> None:
    """Give ``ingest`` to the user who owns the oldest of ``dependents``.

    A pending ingest is re-owned in place and keeps its queue position unless
    ``replace_pending`` is set. Otherwise it is cancelled and replaced, and
    ``dependents`` follow the replacement.
    """
    new_owner = dependents[0]
    if ingest.status == JobStatus.PENDING and not replace_pending:
        ingest.userId = new_owner.userId
        ingest.installation_id = new_owner.installation_id
        session.add(ingest)
        return

    # A running ingest may already hold a token minted for the previous
    # owner. Cancel it before enqueueing the replacement so the active dedupe
    # constraint permits a fresh, authorized job.
    ingest.status = JobStatus.CANCELLED
    ingest.claimed_at = None
    ingest.claimed_by = None
    ingest.error = error
    session.add(ingest)
    session.flush()

    replacement, _ = enqueue_job(
        session,
        user_id=new_owner.userId,
        installation_id=new_owner.installation_id,
        repo_name=ingest.repo_name,
        ref=ingest.ref,
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=(
            ingest.dedupe_key
            or repository_ingest_dedupe_key(
                repo_name=ingest.repo_name,
                ref=ingest.ref,
            )
        ),
        commit=False,
    )
    session.exec(
        update(Job)
        .where(Job.id.in_([dependent.id for dependent in dependents]))
        .values(blocked_by_job_id=replacement.id)
    )


def reassign_shared_ingests(
    session: Session,
    sidelined_user_ids: set[str],
    *,
    error: str,
) -> None:
    """Hand sidelined users' active primary ingests to an authorized dependent.

    Call after the sidelined users' connection rows are removed or marked
    inactive in the same transaction, so they cannot be chosen as new owners.
    Only other users' dependents follow a replacement ingest; the sidelined
    users' own jobs are left for the caller to cancel or delete.
    """
    if not sidelined_user_ids:
        return

    ingests = session.exec(
        select(Job)
        .where(
            Job.userId.in_(sidelined_user_ids),
            Job.job_type == JobType.REPOSITORY_INGEST,
            Job.status.in_(JobStatus.ACTIVE),
            Job.blocked_by_job_id.is_(None),
        )
        .order_by(Job.createdAt, Job.id)
        .with_for_update()
    ).all()

    for ingest in ingests:
        dependents = _active_other_user_dependents(
            session,
            ingest=ingest,
            sidelined_user_ids=sidelined_user_ids,
        )
        if not dependents:
            continue

        _hand_over(
            session,
            ingest,
            dependents,
            error=error,
            replace_pending=False,
        )


def release_user_jobs(
    session: Session,
    user_ids: set[str],
    *,
    error: str,
    dispose: Literal["cancel", "delete"],
) -> None:
    """Hand over users' shared ingests, then cancel or delete their other jobs.

    The shared step for every event that removes users' ability to run jobs.
    Call after their connection rows are removed or deactivated in the same
    transaction. Never commits; the caller owns the transaction.
    """
    if not user_ids:
        return

    reassign_shared_ingests(session, user_ids, error=error)

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


def cancel_shared_ingest(session: Session, job: Job) -> None:
    """Cancel an owner's ingest row without stranding other users' work.

    A waiting row is simply cancelled. A primary with other users' active
    dependents is cancelled and replaced for the oldest of them, and only
    their dependents follow; the owner's own dependents fail as before.
    Never commits.
    """
    locked = _lock_job(session, job.id)
    if locked is None or locked.status not in JobStatus.ACTIVE:
        return

    if locked.blocked_by_job_id is None:
        dependents = _active_other_user_dependents(
            session,
            ingest=locked,
            sidelined_user_ids={locked.userId},
        )
        if dependents:
            _hand_over(
                session,
                locked,
                dependents,
                error=None,
                replace_pending=True,
            )
            return

    locked.status = JobStatus.CANCELLED
    locked.claimed_at = None
    locked.claimed_by = None
    session.add(locked)
