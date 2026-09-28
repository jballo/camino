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
