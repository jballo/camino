from sqlalchemy import delete
from sqlmodel import Session, select

from app.models.code import CodeChunkModel
from app.models.github_connection import GithubConnections
from app.models.rate_limit import RateLimit
from app.models.tour_job import TourJob
from app.models.user import User


class AccountDeletionError(Exception):
    """Raised when local account data could not be deleted atomically."""


def delete_local_account_data(session: Session, user_id: str) -> None:
    """Delete one user's local data, preserving shared GitHub installations.

    The operation intentionally uses set-based deletes so webhook replays and a
    direct-delete/webhook race are harmless.
    """
    try:
        installation_ids = set(
            session.exec(
                select(GithubConnections.installationId).where(
                    GithubConnections.userId == user_id
                )
            ).all()
        )

        session.exec(delete(TourJob).where(TourJob.userId == user_id))
        session.exec(delete(RateLimit).where(RateLimit.user_id == user_id))
        session.exec(
            delete(GithubConnections).where(GithubConnections.userId == user_id)
        )
        session.exec(delete(User).where(User.id == user_id))

        if installation_ids:
            retained_installation_ids = set(
                session.exec(
                    select(GithubConnections.installationId).where(
                        GithubConnections.installationId.in_(installation_ids)
                    )
                ).all()
            )
            unreferenced_installation_ids = (
                installation_ids - retained_installation_ids
            )
            if unreferenced_installation_ids:
                session.exec(
                    delete(CodeChunkModel).where(
                        CodeChunkModel.installation_id.in_(
                            unreferenced_installation_ids
                        )
                    )
                )

        session.commit()
    except Exception as error:
        session.rollback()
        raise AccountDeletionError from error
