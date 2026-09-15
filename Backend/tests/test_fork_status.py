from unittest.mock import patch

from app.services.fork_status import resolve_fork_status


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def close(self):
        pass


def test_fork_resolves_upstream_and_behind_count():
    with (
        patch("app.services.fork_status.installation_access_token", return_value="token"),
        patch("app.services.fork_status.requests.get", side_effect=[
            Response({"parent": {"full_name": "Org/Upstream"}}),
            Response({"behind_by": 3}),
        ]) as get,
    ):
        result = resolve_fork_status("Me/Fork", 7, "develop")
    assert result.upstream_repo == "org/upstream"
    assert result.fork_repo == "me/fork"
    assert result.commits_behind == 3
    assert result.measurable is True
    assert "/compare/develop...me:develop" in get.call_args_list[1].args[0]
