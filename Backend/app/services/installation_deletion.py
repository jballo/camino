from sqlalchemy import delete
from sqlmodel import Session

from app.models.github_connection import GithubConnections
from app.models.job import Job
from app.services.shared_ingests import release_user_jobs


INSTALLATION_DELETION_ERROR = "GitHub installation deleted"


class InstallationDeletionError(Exception):
    """Raised when installation-scoped data could not be deleted atomically."""


def delete_installation_local_data(session: Session, installation_id: int) -> None:
    """Delete all local rows for a GitHub App installation.

    Set-based so shared org installations and webhook replays are safe. Shared
    ingests owned by the installation's users are handed to an authorized
    dependent before the users' job rows are deleted.
    """
    try:
        user_ids = set(
            session.exec(
                delete(GithubConnections)
                .where(GithubConnections.installationId == installation_id)
                .returning(GithubConnections.userId)
            )
            .scalars()
            .all()
        )
        release_user_jobs(
            session,
            user_ids,
            error=INSTALLATION_DELETION_ERROR,
            dispose="delete",
        )
        session.exec(
            delete(Job).where(Job.installation_id == installation_id)
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise InstallationDeletionError from error
