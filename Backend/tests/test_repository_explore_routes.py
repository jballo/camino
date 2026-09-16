import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.api.repositories import (
    RepoFollowBody,
    follow_repository,
    lookup_repository,
    repository_overview,
    unfollow_repository,
)
from app.models.repo_follow import UserRepoFollow
from app.services.repo_access import RepoAccess


USER_ID = "user_123"
INDEXED_AT = dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc)


def _index_row(repo_name: str = "org/repo"):
    return SimpleNamespace(
        repo_name=repo_name,
        ref="main",
        indexed_sha="abcdef123456",
        indexed_at=INDEXED_AT,
        chunk_count=42,
    )


@pytest.mark.asyncio
async def test_lookup_returns_index_facts_and_personal_follow():
    session = MagicMock()
    session.exec.return_value.first.return_value = UserRepoFollow(
        userId=USER_ID,
        repo_name="org/repo",
    )
    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(installation_id=12, visibility="public"),
        ),
        patch("app.api.repositories._index_rows", return_value=[_index_row()]),
    ):
        result = await lookup_repository("Org/Repo", session, USER_ID)

    assert result.repoName == "org/repo"
    assert result.indexed is True
    assert result.followed is True
    assert result.refs[0].chunkCount == 42
    assert result.refs[0].indexedSha == "abcdef123456"


@pytest.mark.asyncio
async def test_overview_keeps_installed_and_requested_separate():
    session = MagicMock()
    connection_result = MagicMock()
    connection_result.one.return_value = MagicMock(installationId=12)
    follows_result = MagicMock()
    follows_result.all.return_value = [
        UserRepoFollow(userId=USER_ID, repo_name="public/requested")
    ]
    session.exec.side_effect = [connection_result, follows_result]

    with (
        patch(
            "app.api.repositories._installed_repository_names",
            return_value={"private/installed"},
        ),
        patch(
            "app.api.repositories._index_rows",
            return_value=[
                _index_row("private/installed"),
                _index_row("public/requested"),
                _index_row("other/not-visible"),
            ],
        ),
    ):
        result = await repository_overview(session, USER_ID)

    assert [item.repoName for item in result.installed] == ["private/installed"]
    assert [item.repoName for item in result.requested] == ["public/requested"]
    assert result.installed[0].refs[0].chunkCount == 42
    assert result.requested[0].refs[0].ref == "main"


@pytest.mark.asyncio
async def test_follow_attaches_an_indexed_repository_without_enqueuing():
    session = MagicMock()
    indexed_result = MagicMock()
    indexed_result.first.return_value = MagicMock()
    session.exec.return_value = indexed_result

    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(installation_id=12, visibility="public"),
        ),
        patch("app.api.repositories._installed_repository_names", return_value=set()),
        patch("app.api.repositories.enqueue_job") as enqueue,
    ):
        result = await follow_repository(
            RepoFollowBody(repoName="Org/Repo"),
            session,
            USER_ID,
        )

    assert result.model_dump() == {
        "repoName": "org/repo",
        "followed": True,
        "indexed": True,
        "jobQueued": False,
    }
    sql = str(session.execute.call_args.args[0])
    assert "ON CONFLICT ON CONSTRAINT uq_user_repo_follow DO NOTHING" in sql
    session.commit.assert_called_once()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_follow_requests_an_unindexed_repository_once():
    session = MagicMock()
    indexed_result = MagicMock()
    indexed_result.first.return_value = None
    session.exec.return_value = indexed_result

    with (
        patch(
            "app.api.repositories.resolve_repo_access",
            return_value=RepoAccess(installation_id=12, visibility="public"),
        ),
        patch("app.api.repositories._installed_repository_names", return_value=set()),
        patch(
            "app.api.repositories.resolve_target_branch",
            return_value=SimpleNamespace(branch="main"),
        ),
        patch(
            "app.api.repositories.enqueue_job",
            return_value=(MagicMock(), True),
        ) as enqueue,
    ):
        result = await follow_repository(
            RepoFollowBody(repoName="org/repo"),
            session,
            USER_ID,
        )

    assert result.indexed is False
    assert result.jobQueued is True
    assert enqueue.call_args.kwargs["dedupe_key"] == (
        "repository_ingest:org/repo:main"
    )


@pytest.mark.asyncio
async def test_unfollow_only_removes_the_authenticated_users_row():
    session = MagicMock()
    follow = UserRepoFollow(userId=USER_ID, repo_name="org/repo")
    session.exec.return_value.first.return_value = follow

    await unfollow_repository("Org", "Repo", session, USER_ID)

    session.delete.assert_called_once_with(follow)
    session.commit.assert_called_once()
    statement = str(session.exec.call_args.args[0])
    assert "user_repo_follows.\"userId\"" in statement
    assert "user_repo_follows.repo_name" in statement
