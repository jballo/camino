"""Shared GitHub App authentication helpers."""

from github import Auth, GithubIntegration

from app.config import settings


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
    return client.get_access_token(installation_id).token
