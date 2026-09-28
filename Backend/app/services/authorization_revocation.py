from sqlalchemy import update
from sqlmodel import Session

from app.models.github_connection import GithubConnections


class AuthorizationRevocationError(Exception):
    """Raised when a user's GitHub connections could not be deactivated."""


def deactivate_user_connections(session: Session, github_user_id: int) -> None:
    """Deactivate every local connection owned by a GitHub user."""
    try:
        session.exec(
            update(GithubConnections)
            .where(GithubConnections.githubUserId == github_user_id)
            .values(active=False)
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise AuthorizationRevocationError from error
