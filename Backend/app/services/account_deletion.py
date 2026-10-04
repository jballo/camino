from sqlalchemy import delete
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job
from app.models.rate_limit import RateLimit
from app.models.repo_follow import UserRepoFollow
from app.models.user import User
from app.services.shared_ingests import release_user_jobs


ACCOUNT_DELETION_ERROR = "Account deleted"


class AccountDeletionError(Exception):
    """Raised when local account data could not be deleted atomically."""


def delete_local_account_data(session: Session, user_id: str) -> None:
    """Delete one user's local data, preserving shared GitHub installations.

    The operation intentionally uses set-based deletes so webhook replays and a
    direct-delete/webhook race are harmless. Shared ingests the user owns are
    handed to an authorized dependent before the user's job rows are deleted.
    """
    try:
        installation_ids = set(
            session.exec(
                select(GithubConnections.installationId).where(
                    GithubConnections.userId == user_id
                )
            ).all()
        )

        if installation_ids:
            session.exec(
                select(GithubConnections.id)
                .where(
                    GithubConnections.installationId.in_(installation_ids)
                )
                .order_by(
                    GithubConnections.installationId,
                    GithubConnections.id,
                )
                .with_for_update()
            ).all()

        session.exec(
            delete(GithubConnections).where(GithubConnections.userId == user_id)
        )
        release_user_jobs(
            session,
            {user_id},
            error=ACCOUNT_DELETION_ERROR,
            dispose="delete",
        )
        session.exec(delete(RateLimit).where(RateLimit.user_id == user_id))
        session.exec(
            delete(UserRepoFollow).where(UserRepoFollow.userId == user_id)
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
                    delete(Job).where(
                        Job.installation_id.in_(
                            unreferenced_installation_ids
                        )
                    )
                )
        session.commit()
    except Exception as error:
        session.rollback()
        raise AccountDeletionError from error
