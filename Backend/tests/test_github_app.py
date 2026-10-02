from unittest.mock import MagicMock

from github import GithubException
import pytest

from app.services.github_app import (
    GithubConnectionInvalid,
    installation_access_token,
)


def test_token_minting_404_returns_reconnect_error():
    integration = MagicMock()
    integration.get_access_token.side_effect = GithubException(
        404,
        {"message": "Not Found"},
        None,
    )

    with pytest.raises(
        GithubConnectionInvalid,
        match="GitHub connection is no longer valid — reconnect",
    ):
        installation_access_token(99, integration=integration)


def test_token_minting_for_suspended_installation_returns_reconnect_error():
    integration = MagicMock()
    integration.get_access_token.side_effect = GithubException(
        403,
        {"message": "This installation has been suspended"},
        None,
    )

    with pytest.raises(GithubConnectionInvalid):
        installation_access_token(99, integration=integration)


def test_token_minting_other_403_is_not_a_reconnect_error():
    integration = MagicMock()
    error = GithubException(
        403,
        {"message": "You have exceeded a secondary rate limit"},
        None,
    )
    integration.get_access_token.side_effect = error

    with pytest.raises(GithubException) as raised:
        installation_access_token(99, integration=integration)

    assert raised.value is error
