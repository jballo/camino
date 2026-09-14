from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import requests
from sqlmodel import Session, select

from app.models.code import RepoIndexState
from app.models.github_connection import GithubConnections
from app.services.github_app import installation_access_token
from app.services.jobs import normalize_repository_name

_GITHUB_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT = (10, 30)


class RepoAccessError(RuntimeError):
    """Base failure while resolving repository access."""


class RepoAccessDenied(RepoAccessError):
    """The user has no GitHub connection or cannot access the repository."""


class RepoAccessUnavailable(RepoAccessError):
    """GitHub could not be reached or returned an unexpected response."""


@dataclass(frozen=True)
class RepoAccess:
    installation_id: int
    visibility: str


def resolve_repo_access(
    session: Session,
    user_id: str,
    repo_name: str,
) -> RepoAccess:
    """Probe a repository with the user's installation token.

    GitHub installation tokens can read every public repository and each
    private repository selected for the installation. A 404 therefore means
    the repository is either missing or not visible to this user.
    """
    connection = session.exec(
        select(GithubConnections).where(GithubConnections.userId == user_id)
    ).first()
    if connection is None:
        raise RepoAccessDenied("GitHub connection not found for user")

    try:
        token = installation_access_token(connection.installationId)
        response = requests.get(
            f"https://api.github.com/repos/{normalize_repository_name(repo_name)}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as error:
        raise RepoAccessUnavailable("GitHub repository access check failed") from error

    try:
        if response.status_code == 404:
            raise RepoAccessDenied("Repository not found")
        if response.status_code != 200:
            raise RepoAccessUnavailable(
                f"GitHub repository access check returned {response.status_code}"
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise RepoAccessUnavailable(
                "GitHub repository access check returned invalid data"
            ) from error
    finally:
        response.close()

    if not isinstance(payload, Mapping):
        raise RepoAccessUnavailable(
            "GitHub repository access check returned invalid data"
        )

    return RepoAccess(
        installation_id=connection.installationId,
        visibility="private" if payload.get("private") else "public",
    )


def authorize_index_read(
    session: Session,
    user_id: str,
    index_state: RepoIndexState,
) -> None:
    """Authorize a read of one shared index entry."""
    if index_state.visibility == "public":
        return
    if index_state.visibility != "private":
        raise RepoAccessDenied("Repository index has invalid visibility")
    resolve_repo_access(session, user_id, index_state.repo_name)
