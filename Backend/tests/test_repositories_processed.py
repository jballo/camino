from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.repositories import list_processed_repositories


async def test_processed_repository_counts_use_live_chunks():
    session = MagicMock()
    session.exec.return_value.one.return_value = MagicMock(installationId=123)
    session.execute.return_value.all.return_value = [
        SimpleNamespace(
            repo_name="org/one",
            ref="main",
            visibility="private",
            indexed_sha="sha-one",
            chunk_count=7,
            requested_by_user=False,
        ),
        SimpleNamespace(
            repo_name="org/public",
            ref="develop",
            visibility="public",
            indexed_sha="sha-public",
            chunk_count=3,
            requested_by_user=True,
        ),
    ]
    installation = MagicMock()
    installation.get_repos.return_value = [MagicMock(full_name="Org/One")]
    integration = MagicMock()
    integration.get_app_installation.return_value = installation

    with (
        patch("app.api.repositories.Auth.AppAuth"),
        patch(
            "app.api.repositories.GithubIntegration",
            return_value=integration,
        ),
    ):
        result = await list_processed_repositories(session, "user_123")

    assert result == [
        {
            "repo_name": "org/one",
            "ref": "main",
            "visibility": "private",
            "indexed_sha": "sha-one",
            "chunk_count": 7,
        },
        {
            "repo_name": "org/public",
            "ref": "develop",
            "visibility": "public",
            "indexed_sha": "sha-public",
            "chunk_count": 3,
        },
    ]
    sql = " ".join(str(session.execute.call_args.args[0]).split())
    assert "FROM repo_index_state AS s" in sql
    assert "c.ref = s.ref" in sql
    assert session.execute.call_args.args[1] == {"user_id": "user_123"}
