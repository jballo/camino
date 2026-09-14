from unittest.mock import ANY, MagicMock, patch

import pytest

from app.models.code import RepoIndexState
from app.services.repo_access import (
    RepoAccessDenied,
    authorize_index_read,
    resolve_repo_access,
)
from app.services.jobs import repository_ingest_dedupe_key


def _session(installation_id: int = 123) -> MagicMock:
    session = MagicMock()
    session.exec.return_value.first.return_value = MagicMock(
        installationId=installation_id
    )
    return session


def _response(status: int, payload: dict | None = None) -> MagicMock:
    response = MagicMock(status_code=status)
    response.json.return_value = payload or {}
    return response


@pytest.mark.parametrize(
    ("private", "visibility"),
    [(False, "public"), (True, "private")],
)
def test_resolve_repo_access_uses_repository_visibility(private, visibility):
    session = _session()
    with (
        patch(
            "app.services.repo_access.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.repo_access.requests.get",
            return_value=_response(200, {"private": private}),
        ) as get,
    ):
        access = resolve_repo_access(session, "user_1", "Org/Repo")

    assert access.installation_id == 123
    assert access.visibility == visibility
    assert get.call_args.args[0] == "https://api.github.com/repos/org/repo"


def test_resolve_repo_access_treats_404_as_denied():
    with (
        patch(
            "app.services.repo_access.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.repo_access.requests.get",
            return_value=_response(404),
        ),
        pytest.raises(RepoAccessDenied, match="Repository not found"),
    ):
        resolve_repo_access(_session(), "user_1", "org/private")


def test_public_index_read_does_not_call_github():
    state = RepoIndexState(
        repo_name="org/repo",
        ref="main",
        visibility="public",
        active_generation="generation-1",
    )
    with patch("app.services.repo_access.resolve_repo_access") as probe:
        authorize_index_read(MagicMock(), "user_1", state)
    probe.assert_not_called()


def test_private_index_read_rechecks_access():
    state = RepoIndexState(
        repo_name="org/repo",
        ref="main",
        visibility="private",
        active_generation="generation-1",
    )
    with patch("app.services.repo_access.resolve_repo_access") as probe:
        authorize_index_read(MagicMock(), "user_1", state)
    probe.assert_called_once_with(ANY, "user_1", "org/repo")


def test_repository_ingestion_dedupe_is_global_and_ref_aware():
    assert repository_ingest_dedupe_key(
        repo_name="Org/Repo", ref="feature/CaseSensitive"
    ) == repository_ingest_dedupe_key(
        repo_name="org/repo", ref="feature/CaseSensitive"
    )
    assert repository_ingest_dedupe_key(
        repo_name="org/repo", ref="feature/CaseSensitive"
    ) != repository_ingest_dedupe_key(
        repo_name="org/repo", ref="feature/casesensitive"
    )
