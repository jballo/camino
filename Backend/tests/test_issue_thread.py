from unittest.mock import patch

from app.services.issue_thread import fetch_issue_thread


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


def test_fetch_issue_derives_warnings_and_maintainer_branch():
    issue = {
        "title": "Fix auth refresh",
        "body": "Refresh can race.",
        "state": "closed",
        "html_url": "https://github.com/org/repo/issues/44",
        "labels": [{"name": "needs-discussion"}],
        "assignees": [{"login": "octocat"}],
    }
    comments = [{
        "author_association": "MEMBER",
        "body": "Please target `develop` branch.",
        "user": {"login": "maintainer"},
    }]
    timeline = [{
        "event": "cross-referenced",
        "source": {"issue": {
            "state": "open", "number": 88,
            "html_url": "https://github.com/org/repo/pull/88",
            "pull_request": {},
        }},
    }]
    responses = [Response(issue), Response(comments), Response(timeline)]
    with (
        patch("app.services.issue_thread.installation_access_token", return_value="token"),
        patch("app.services.issue_thread.requests.get", side_effect=responses),
    ):
        result = fetch_issue_thread("Org/Repo", 44, 7)

    assert result.repo_name == "org/repo"
    assert result.branch_instruction.branch == "develop"
    assert {warning.kind for warning in result.warnings} == {
        "closed", "assigned", "needs_discussion", "open_pr"
    }
    assert all(response.closed for response in responses)
