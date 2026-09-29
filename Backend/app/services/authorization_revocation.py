from __future__ import annotations

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services.jobs import enqueue_job, repository_ingest_dedupe_key


REVOCATION_ERROR = "GitHub authorization revoked"


class AuthorizationRevocationError(Exception):
    """Raised when a user's GitHub authorization could not be revoked."""


def _active_other_user_dependents(
    session: Session,
    *,
    ingest: Job,
    revoked_user_ids: set[str],
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
            Job.userId.notin_(revoked_user_ids),
            GithubConnections.active.is_(True),
        )
        .order_by(Job.createdAt, Job.id)
        .with_for_update()
    )
    return list(session.exec(statement).all())


def _reassign_shared_ingests(
    session: Session,
    revoked_user_ids: set[str],
) -> None:
    if not revoked_user_ids:
        return

    ingests = session.exec(
        select(Job)
        .where(
            Job.userId.in_(revoked_user_ids),
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
            revoked_user_ids=revoked_user_ids,
        )
        if not dependents:
            continue

        new_owner = dependents[0]
        if ingest.status == JobStatus.PENDING:
            ingest.userId = new_owner.userId
            ingest.installation_id = new_owner.installation_id
            session.add(ingest)
            continue

        # A running ingest may already hold a token minted for the revoked
        # request. Cancel it before enqueueing the replacement so the active
        # dedupe constraint permits a fresh, authorized job.
        ingest.status = JobStatus.CANCELLED
        ingest.claimed_at = None
        ingest.claimed_by = None
        ingest.error = REVOCATION_ERROR
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


def revoke_user_authorization(session: Session, github_user_id: int) -> None:
    """Remove a GitHub user and cancel or transfer their active work."""
    try:
        user_ids = set(
            session.exec(
                delete(GithubConnections)
                .where(GithubConnections.githubUserId == github_user_id)
                .returning(GithubConnections.userId)
            )
            .scalars()
            .all()
        )

        _reassign_shared_ingests(session, user_ids)

        if user_ids:
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
                    error=REVOCATION_ERROR,
                )
            )
        session.commit()
    except Exception as error:
        session.rollback()
        raise AuthorizationRevocationError from error


def deactivate_user_connections(session: Session, github_user_id: int) -> None:
    """Backward-compatible alias for authorization revocation."""
    revoke_user_authorization(session, github_user_id)
