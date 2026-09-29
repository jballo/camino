from __future__ import annotations

from sqlalchemy import update
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services.jobs import enqueue_job, repository_ingest_dedupe_key


def _active_other_user_dependents(
    session: Session,
    *,
    ingest: Job,
    sidelined_user_ids: set[str],
) -> list[Job]:
    """Return authorized active dependents in stable ownership order."""
    statement = (
        select(Job)
        .join(
            GithubConnections,
            (GithubConnections.userId == Job.userId)
            & (GithubConnections.installationId == Job.installation_id),
        )
        .where(
            Job.blocked_by_job_id == ingest.id,
            Job.status.in_(JobStatus.ACTIVE),
            Job.userId.notin_(sidelined_user_ids),
            GithubConnections.active.is_(True),
        )
        .order_by(Job.createdAt, Job.id)
        .with_for_update()
    )
    return list(session.exec(statement).all())


def reassign_shared_ingests(
    session: Session,
    sidelined_user_ids: set[str],
    *,
    error: str,
    repoint_sidelined_dependents: bool,
) -> None:
    """Hand sidelined users' active shared ingests to an authorized dependent.

    Call after the sidelined users' connection rows are removed or marked
    inactive in the same transaction, so they cannot be chosen as new owners.
    ``repoint_sidelined_dependents`` also moves the sidelined users' own
    dependents onto a replacement ingest; set it when their work should survive
    (suspension) rather than be cancelled (revocation).
    """
    if not sidelined_user_ids:
        return

    ingests = session.exec(
        select(Job)
        .where(
            Job.userId.in_(sidelined_user_ids),
            Job.job_type == JobType.REPOSITORY_INGEST,
            Job.status.in_(JobStatus.ACTIVE),
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

        new_owner = dependents[0]
        if ingest.status == JobStatus.PENDING:
            ingest.userId = new_owner.userId
            ingest.installation_id = new_owner.installation_id
            session.add(ingest)
            continue

        # A running ingest may already hold a token minted for the sidelined
        # owner. Cancel it before enqueueing the replacement so the active
        # dedupe constraint permits a fresh, authorized job.
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
        if repoint_sidelined_dependents:
            repoint_filter = (
                Job.blocked_by_job_id == ingest.id,
                Job.status.in_(JobStatus.ACTIVE),
            )
        else:
            repoint_filter = (Job.id.in_([dependent.id for dependent in dependents]),)
        session.exec(
            update(Job).where(*repoint_filter).values(blocked_by_job_id=replacement.id)
        )
