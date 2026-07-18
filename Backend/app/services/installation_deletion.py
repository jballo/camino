from sqlalchemy import delete
from sqlmodel import Session

from app.models.code import CodeChunkModel
from app.models.github_connection import GithubConnections
from app.models.tour_job import TourJob


class InstallationDeletionError(Exception):
    """Raised when installation-scoped data could not be deleted atomically."""


def delete_installation_local_data(session: Session, installation_id: int) -> None:
    """Delete all local rows for a GitHub App installation.

    Set-based so shared org installations and webhook replays are safe.
    """
    try:
        session.exec(
            delete(GithubConnections).where(
                GithubConnections.installationId == installation_id
            )
        )
        session.exec(
            delete(TourJob).where(TourJob.installation_id == installation_id)
        )
        session.exec(
            delete(CodeChunkModel).where(
                CodeChunkModel.installation_id == installation_id
            )
        )
        session.commit()
    except Exception as error:
        session.rollback()
        raise InstallationDeletionError from error
