from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from requests.exceptions import Timeout

from app.services.embeddings import EmbeddingError
from app.services.repository_ingestion import (
    PermanentRepositoryIngestionError,
    TransientRepositoryIngestionError,
    ingest_repository,
)


def _github(installation):
    integration = MagicMock()
    integration.get_app_installation.return_value = installation
    return patch(
        "app.services.repository_ingestion.GithubIntegration",
        return_value=integration,
    )


async def test_ingestion_commits_atomic_replace_and_returns_counts():
    session = MagicMock()
    installation = MagicMock()
    repository = MagicMock(full_name="org/repo")
    repository.get_contents.return_value = []
    installation.get_repos.return_value = [repository]

    with (
        _github(installation),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
        )

    assert result == {"chunks_inserted": 0, "embeddings_created": 0}
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


async def test_network_failure_is_transient():
    session = MagicMock()
    integration = MagicMock()
    integration.get_app_installation.side_effect = Timeout("timed out")

    with patch(
        "app.services.repository_ingestion.GithubIntegration",
        return_value=integration,
    ):
        with pytest.raises(TransientRepositoryIngestionError):
            await ingest_repository(
                session,
                repo_name="org/repo",
                installation_id=123,
            )

    session.rollback.assert_called_once_with()


async def test_missing_repository_is_permanent():
    session = MagicMock()
    installation = MagicMock()
    installation.get_repos.return_value = []

    with _github(installation):
        with pytest.raises(
            PermanentRepositoryIngestionError,
            match="Repository not found",
        ):
            await ingest_repository(
                session,
                repo_name="org/missing",
                installation_id=123,
            )

    session.rollback.assert_called_once_with()


async def test_embedding_failure_is_transient():
    session = MagicMock()
    installation = MagicMock()
    repository = MagicMock(full_name="org/repo")
    repository.get_contents.return_value = []
    installation.get_repos.return_value = [repository]

    with (
        _github(installation),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            side_effect=EmbeddingError("OpenAI unavailable"),
        ),
    ):
        with pytest.raises(TransientRepositoryIngestionError):
            await ingest_repository(
                session,
                repo_name="org/repo",
                installation_id=123,
            )

    session.rollback.assert_called_once_with()
