"""Shared GitHub App authentication helpers."""

from github import Auth, GithubException, GithubIntegration
from urllib3.util.retry import Retry

from app.config import settings


INVALID_CONNECTION_MESSAGE = "GitHub connection is no longer valid — reconnect"

# Matches the read half of the (10, 30) timeout used by direct GitHub calls.
# PyGithub takes one value for both connect and read.
GITHUB_APP_TIMEOUT_SECONDS = 30

# PyGithub's default is no retries at all, so one slow token request failed a
# whole brief attempt (#102). Plain urllib3 Retry rather than GithubRetry:
# GithubRetry sleeps up to a minute on secondary rate limits, which would stall
# the preview and create request handlers that mint tokens inline.
#   - POST must be allowed explicitly: the token mint is a POST, and urllib3
#     skips read-error retries for methods outside allowed_methods.
#   - read=1 keeps the worst case per mint near one minute; preview and create
#     mint several times inside one HTTP request.
#   - raise_on_status=False returns the last 5xx to PyGithub, so callers still
#     get a GithubException instead of a requests RetryError.
#   - 403 is deliberately absent: a suspended installation answers 403 and must
#     surface at once as GithubConnectionInvalid.
GITHUB_APP_RETRY = Retry(
    total=2,
    connect=2,
    read=1,
    status=2,
    backoff_factor=1,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "POST"}),
    raise_on_status=False,
)


class GithubConnectionInvalid(RuntimeError):
    """The stored installation no longer exists or cannot mint tokens."""


def github_integration(
    *,
    base_url: str | None = None,
) -> GithubIntegration:
    """Build an authenticated GitHub App integration client.

    ``base_url`` exists for tests that point the client at a local server.
    """
    app_auth = Auth.AppAuth(
        app_id=settings.gh_app_id,
        private_key=settings.gh_app_private_key,
    )
    options = {} if base_url is None else {"base_url": base_url}
    return GithubIntegration(
        auth=app_auth,
        timeout=GITHUB_APP_TIMEOUT_SECONDS,
        retry=GITHUB_APP_RETRY,
        **options,
    )


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
