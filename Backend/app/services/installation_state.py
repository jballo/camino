from sqlalchemy import update
from sqlmodel import Session

from app.models.github_connection import GithubConnections
from app.services.shared_ingests import reassign_shared_ingests


SUSPENSION_ERROR = "GitHub installation suspended"


class InstallationStateError(Exception):
    """Raised when an installation's local active state could not be changed."""


def set_installation_active(
    session: Session,
    installation_id: int,
    *,
    active: bool,
) -> None:
    """Set installation suspension state for remaining owner rows.

    ``active`` represents installation suspension only. Users who revoked their
    authorization have no connection row and cannot be restored by unsuspend.
    Suspension hands shared ingests owned by the installation's users to an
    authorized dependent; the suspended users' own jobs stay pending until
    unsuspend makes them claimable again.
    """
    try:
        user_ids = set(
            session.exec(
                update(GithubConnections)
                .where(GithubConnections.installationId == installation_id)
                .values(active=active)
                .returning(GithubConnections.userId)
            )
            .scalars()
            .all()
        )

        if not active:
            reassign_shared_ingests(
                session,
                user_ids,
                error=SUSPENSION_ERROR,
                repoint_sidelined_dependents=True,
            )
        session.commit()
    except Exception as error:
        session.rollback()
        raise InstallationStateError from error
