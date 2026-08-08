from sqlalchemy import delete
from sqlmodel import Session

from app.models.github_connection import GithubConnections


class AuthorizationRevocationError(Exception):
    """Raised when revoked GitHub user connections could not be deleted."""


def delete_revoked_user_connections(
    session: Session, github_user_id: int
) -> None:
    """Delete every connection associated with a revoked GitHub user."""
    try:
        session.exec(
            delete(GithubConnections).where(
                GithubConnections.githubUserId == github_user_id
            )
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise AuthorizationRevocationError from error
