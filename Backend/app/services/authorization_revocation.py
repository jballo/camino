from __future__ import annotations

from sqlalchemy import delete
from sqlmodel import Session

from app.models.github_connection import GithubConnections
from app.services.shared_ingests import release_user_jobs


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

        release_user_jobs(
            session,
            user_ids,
            error=REVOCATION_ERROR,
            dispose="cancel",
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise AuthorizationRevocationError from error


def deactivate_user_connections(session: Session, github_user_id: int) -> None:
    """Backward-compatible alias for authorization revocation."""
    revoke_user_authorization(session, github_user_id)
