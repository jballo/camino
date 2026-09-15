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


@pytest.mark.parametrize("visibility", ["public", "private"])
def test_index_read_always_rechecks_access(visibility):
    state = RepoIndexState(
        repo_name="org/repo",
        ref="main",
        visibility=visibility,
        active_generation="generation-1",
    )
    with patch("app.services.repo_access.resolve_repo_access") as probe:
        authorize_index_read(MagicMock(), "user_1", state)
    probe.assert_called_once_with(ANY, "user_1", "org/repo")


def test_privatized_repo_denies_stale_public_index_read():
    """A repo indexed while public but private today must deny the read."""
    state = RepoIndexState(
        repo_name="org/repo",
        ref="main",
        visibility="public",
        active_generation="generation-1",
    )
    with (
        patch(
            "app.services.repo_access.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.repo_access.requests.get",
            return_value=_response(404),
        ),
        pytest.raises(RepoAccessDenied),
    ):
        authorize_index_read(_session(), "user_1", state)


def test_invalid_visibility_denies_without_probe():
    state = RepoIndexState(
        repo_name="org/repo",
        ref="main",
        visibility="internal",
        active_generation="generation-1",
    )
    with (
        patch("app.services.repo_access.resolve_repo_access") as probe,
        pytest.raises(RepoAccessDenied, match="invalid visibility"),
    ):
        authorize_index_read(MagicMock(), "user_1", state)
    probe.assert_not_called()


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
