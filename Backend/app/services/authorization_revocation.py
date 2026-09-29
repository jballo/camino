from __future__ import annotations

from sqlalchemy import delete, update
from sqlmodel import Session

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus
from app.services.shared_ingests import reassign_shared_ingests


REVOCATION_ERROR = "GitHub authorization revoked"


class AuthorizationRevocationError(Exception):
    """Raised when a user's GitHub authorization could not be revoked."""


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

        # Revoked users' own jobs are cancelled below, so only other users'
        # dependents need to follow a replacement ingest.
        reassign_shared_ingests(
            session,
            user_ids,
            error=REVOCATION_ERROR,
            repoint_sidelined_dependents=False,
        )

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
