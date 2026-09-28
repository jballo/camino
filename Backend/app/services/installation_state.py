from sqlalchemy import update
from sqlmodel import Session

from app.models.github_connection import GithubConnections


class InstallationStateError(Exception):
    """Raised when an installation's local active state could not be changed."""


def set_installation_active(
    session: Session,
    installation_id: int,
    *,
    active: bool,
) -> None:
    """Apply a GitHub installation suspend/unsuspend event to every owner row."""
    try:
        session.exec(
            update(GithubConnections)
            .where(GithubConnections.installationId == installation_id)
            .values(active=active)
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise InstallationStateError from error
