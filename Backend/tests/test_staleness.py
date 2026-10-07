from unittest.mock import patch

import pytest

from app.services.staleness import (
    ChangedFile,
    compare_to_head,
    relevant_overlap,
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("changed_files", "cited_paths", "query_terms", "expected", "term"),
    [
        (
            [ChangedFile("src/auth/session.py", "added")],
            ["src/auth/login.py"],
            [],
            True,
            None,
        ),
        (
            [ChangedFile("backend/policies/rateLimit.py", "added")],
            ["src/auth/login.py"],
            ["How are request limits enforced?"],
            True,
            "limit",
        ),
        (
            [ChangedFile("src/utils/index.py", "modified")],
            ["backend/auth.py"],
            ["test utility index"],
            False,
            None,
        ),
        (
            [ChangedFile("backend/policies/limits.py", "modified")],
            ["src/auth.py"],
            ["request limit enforcement"],
            True,
            "limit",
        ),
        (
            [ChangedFile("tests/helpers.py", "added")],
            ["backend/auth.py"],
            ["tests helper"],
            False,
            None,
        ),
    ],
)
def test_relevant_overlap_signals(
    changed_files,
    cited_paths,
    query_terms,
    expected,
    term,
):
    verdict = relevant_overlap(changed_files, cited_paths, query_terms)

    assert verdict.overlaps is expected
    if term is not None:
        assert term in verdict.matched_terms


def test_compare_to_head_returns_changed_files_and_head():
    response = _Response(
        {
            "ahead_by": 2,
            "total_commits": 2,
            "base_commit": {"sha": "indexed"},
            "commits": [{"sha": "middle"}, {"sha": "live-head"}],
            "files": [
                {"filename": "src/auth.py", "status": "modified"},
                {"filename": "src/new.py", "status": "added"},
            ],
        }
    )

    with (
        patch(
            "app.services.staleness.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.staleness.requests.get",
            return_value=response,
        ) as get,
    ):
        comparison = compare_to_head("Org/Repo", 7, "indexed", "main")

    assert comparison.measurable
    assert comparison.head_sha == "live-head"
    assert comparison.commits_behind == 2
    assert comparison.changed_files == (
        ChangedFile("src/auth.py", "modified"),
        ChangedFile("src/new.py", "added"),
    )
    assert get.call_args.args[0].endswith(
        "/org/repo/compare/indexed...main"
    )
    assert response.closed


def test_compare_to_head_treats_404_as_unmeasurable():
    response = _Response({}, status_code=404)
    with (
        patch(
            "app.services.staleness.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.staleness.requests.get",
            return_value=response,
        ),
    ):
        comparison = compare_to_head("org/repo", 7, "gone", "main")

    assert not comparison.measurable
    assert comparison.changed_files == ()
    assert response.closed


@pytest.mark.parametrize(
    "payload",
    [
        {
            "ahead_by": 251,
            "total_commits": 251,
            "commits": [{"sha": str(i)} for i in range(250)],
            "files": [],
        },
        {
            "ahead_by": 1,
            "total_commits": 1,
            "commits": [{"sha": "head"}],
            "files": [
                {"filename": f"src/file-{i}.py", "status": "modified"}
                for i in range(300)
            ],
        },
    ],
)
def test_compare_to_head_treats_truncation_as_unmeasurable(payload):
    with (
        patch(
            "app.services.staleness.installation_access_token",
            return_value="token",
        ),
        patch(
            "app.services.staleness.requests.get",
            return_value=_Response(payload),
        ),
    ):
        comparison = compare_to_head("org/repo", 7, "indexed", "main")

    assert not comparison.measurable


def test_compare_to_head_missing_sha_never_calls_github():
    with patch("app.services.staleness.requests.get") as get:
        comparison = compare_to_head("org/repo", 7, None, "main")

    assert not comparison.measurable
    get.assert_not_called()
