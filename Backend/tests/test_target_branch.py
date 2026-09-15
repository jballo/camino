from unittest.mock import patch

import pytest

from app.services.target_branch import resolve_target_branch


class _Response:
    def __init__(
        self,
        payload: object | None = None,
        status_code: int = 200,
        *,
        text: str = "",
    ):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


def _merged_prs(*branches: str) -> list[dict]:
    return [
        {
            "merged_at": f"2026-09-{index + 1:02d}T12:00:00Z",
            "base": {"ref": branch},
        }
        for index, branch in enumerate(branches)
    ]


def _github(
    *,
    docs: dict[str, str] | None = None,
    valid_branches: set[str] | None = None,
    pulls: list[dict] | None = None,
    repo_status: int = 200,
):
    docs = docs or {}
    valid_branches = valid_branches or set()
    pulls = pulls or []

    def get(url, **_kwargs):
        if url.endswith("/repos/org/repo"):
            return _Response({"default_branch": "main"}, repo_status)
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            if path in docs:
                return _Response(text=docs[path])
            return _Response(status_code=404)
        if "/branches/" in url:
            branch = url.split("/branches/", 1)[1]
            return _Response(
                {"name": branch},
                200 if branch in valid_branches else 404,
            )
        if url.endswith("/pulls"):
            return _Response(pulls)
        raise AssertionError(f"Unexpected GitHub URL: {url}")

    return get


def _resolve(**github_options):
    with (
        patch(
            "app.services.target_branch.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.target_branch.requests.get",
            side_effect=_github(**github_options),
        ) as get,
    ):
        return resolve_target_branch("Org/Repo", 7), get


def test_contributing_document_resolves_verified_branch():
    line = "Please target the `develop` branch for all changes."
    resolution, get = _resolve(
        docs={"CONTRIBUTING.md": f"# Contributing\n\n{line}\n"},
        valid_branches={"develop"},
    )

    assert resolution.branch == "develop"
    assert resolution.source == "contributing_doc"
    assert resolution.evidence == line
    assert resolution.evidence_path == "CONTRIBUTING.md"
    assert resolution.default_branch == "main"
    assert resolution.checked_at.tzinfo is not None
    assert any("/branches/develop" in call.args[0] for call in get.call_args_list)


def test_unverified_document_branch_falls_through_to_default():
    resolution, _ = _resolve(
        docs={
            "CONTRIBUTING.md": "Please target the `imaginary` branch."
        },
    )

    assert resolution.branch == "main"
    assert resolution.source == "default_branch"
    assert resolution.evidence == "repository default branch"
    assert resolution.evidence_path is None


def test_generic_branch_prose_does_not_create_a_candidate():
    resolution, get = _resolve(
        docs={
            "CONTRIBUTING.md": "Create a new branch for your work."
        },
    )

    assert resolution.source == "default_branch"
    assert not any("/branches/" in call.args[0] for call in get.call_args_list)


def test_recent_merged_pr_majority_resolves_target():
    resolution, _ = _resolve(
        pulls=_merged_prs(
            "develop",
            "develop",
            "main",
            "develop",
            "release",
            "develop",
            "develop",
            "main",
            "develop",
            "develop",
        )
    )

    assert resolution.branch == "develop"
    assert resolution.source == "merged_prs"
    assert resolution.evidence == "7 of 10 recently merged PRs targeted `develop`"
    assert resolution.evidence_path is None


@pytest.mark.parametrize(
    "pulls",
    [
        _merged_prs("develop", "develop"),
        _merged_prs("develop", "develop", "main", "main"),
    ],
)
def test_weak_merged_pr_signal_falls_through_to_default(pulls):
    resolution, _ = _resolve(pulls=pulls)

    assert resolution.branch == "main"
    assert resolution.source == "default_branch"


def test_repository_lookup_failure_is_unresolved():
    resolution, get = _resolve(repo_status=503)

    assert resolution.branch is None
    assert resolution.source == "unresolved"
    assert resolution.evidence is None
    assert resolution.evidence_path is None
    assert resolution.default_branch is None
    assert get.call_count == 1
