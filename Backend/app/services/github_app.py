"""Shared GitHub App authentication helpers."""

from github import Auth, GithubException, GithubIntegration

from app.config import settings


INVALID_CONNECTION_MESSAGE = "GitHub connection is no longer valid — reconnect"


class GithubConnectionInvalid(RuntimeError):
    """The stored installation no longer exists or cannot mint tokens."""


def github_integration() -> GithubIntegration:
    """Build an authenticated GitHub App integration client."""
    app_auth = Auth.AppAuth(
        app_id=settings.gh_app_id,
        private_key=settings.gh_app_private_key,
    )
    return GithubIntegration(auth=app_auth)


def installation_access_token(
    installation_id: int,
    *,
    integration: GithubIntegration | None = None,
) -> str:
    """Mint an installation token for GitHub REST API calls."""
    client = integration or github_integration()
    try:
        return client.get_access_token(installation_id).token
    except GithubException as error:
        if error.status == 404:
            raise GithubConnectionInvalid(INVALID_CONNECTION_MESSAGE) from error
        raise
