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


def _is_suspended_installation(error: GithubException) -> bool:
    """GitHub refuses tokens for a suspended installation with a 403."""
    if error.status != 403 or not isinstance(error.data, dict):
        return False
    message = error.data.get("message")
    return isinstance(message, str) and "suspended" in message.casefold()


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
        if error.status == 404 or _is_suspended_installation(error):
            raise GithubConnectionInvalid(INVALID_CONNECTION_MESSAGE) from error
        raise
