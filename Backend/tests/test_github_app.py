import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from github import GithubException
import pytest
import requests

from app.services import github_app
from app.services.github_app import (
    GITHUB_APP_RETRY,
    GITHUB_APP_TIMEOUT_SECONDS,
    GithubConnectionInvalid,
    github_integration,
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


def test_token_minting_for_suspended_installation_returns_reconnect_error():
    integration = MagicMock()
    integration.get_access_token.side_effect = GithubException(
        403,
        {"message": "This installation has been suspended"},
        None,
    )

    with pytest.raises(GithubConnectionInvalid):
        installation_access_token(99, integration=integration)


def test_token_minting_other_403_is_not_a_reconnect_error():
    integration = MagicMock()
    error = GithubException(
        403,
        {"message": "You have exceeded a secondary rate limit"},
        None,
    )
    integration.get_access_token.side_effect = error

    with pytest.raises(GithubException) as raised:
        installation_access_token(99, integration=integration)

    assert raised.value is error


# --- #102: timeout and retry policy on the App integration client ---

_TOKEN_PATH = "/app/installations/99/access_tokens"
_STALL_SECONDS = 1


def test_retry_policy_retries_the_token_post_on_read_errors():
    assert "POST" in GITHUB_APP_RETRY.allowed_methods
    assert GITHUB_APP_RETRY.read >= 1
    assert GITHUB_APP_RETRY.connect >= 1


def test_retry_policy_never_retries_403_and_hands_back_final_status():
    assert 403 not in GITHUB_APP_RETRY.status_forcelist
    assert 503 in GITHUB_APP_RETRY.status_forcelist
    assert GITHUB_APP_RETRY.raise_on_status is False


def test_integration_client_uses_explicit_timeout_and_retry():
    with (
        patch("app.services.github_app.Auth.AppAuth"),
        patch("app.services.github_app.GithubIntegration") as integration,
    ):
        github_integration()

    kwargs = integration.call_args.kwargs
    assert kwargs["timeout"] == GITHUB_APP_TIMEOUT_SECONDS
    assert kwargs["retry"] is GITHUB_APP_RETRY


@pytest.fixture
def synthetic_app_key(monkeypatch) -> None:
    """Sign App JWTs with a throwaway key generated for this test."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(github_app.settings, "gh_app_private_key", pem)
    monkeypatch.setattr(github_app, "GITHUB_APP_TIMEOUT_SECONDS", _STALL_SECONDS)


class _TokenServer:
    """Local stand-in for GitHub's token endpoint with scripted responses.

    Each script entry is a status code, or "stall" to hold the response past
    the client's read timeout.
    """

    def __init__(self, script: list[int | str]) -> None:
        self.script = list(script)
        self.requests: list[str] = []
        self.release = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                server.requests.append(self.path)
                step = server.script.pop(0) if server.script else 201
                if step == "stall":
                    server.release.wait(_STALL_SECONDS * 5)
                    return
                body = (
                    {
                        "token": "SYNTHETIC_TEST_VALUE",
                        "expires_at": "2099-01-01T00:00:00Z",
                    }
                    if step == 201
                    else {"message": _MESSAGES.get(step, "Server Error")}
                )
                payload = json.dumps(body).encode()
                try:
                    self.send_response(step)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_TokenServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


_MESSAGES = {
    403: "This installation has been suspended",
    503: "Service Unavailable",
}


@pytest.fixture
def token_server() -> Iterator[type[_TokenServer]]:
    yield _TokenServer


def _mint(server: _TokenServer) -> str:
    integration = github_integration(base_url=server.base_url)
    return installation_access_token(99, integration=integration)


def test_read_timeout_on_token_post_is_retried(synthetic_app_key, token_server):
    with token_server(["stall", 201]) as server:
        token = _mint(server)

    assert token == "SYNTHETIC_TEST_VALUE"
    assert server.requests == [_TOKEN_PATH, _TOKEN_PATH]


def test_persistent_read_timeout_stops_after_one_retry(
    synthetic_app_key, token_server
):
    # read=1 caps one mint near a minute; preview and create mint several
    # times inline. RequestException is what the worker classes as transient.
    with token_server(["stall", "stall", "stall"]) as server:
        with pytest.raises(requests.RequestException):
            _mint(server)

    assert server.requests == [_TOKEN_PATH, _TOKEN_PATH]


def test_server_error_on_token_post_is_retried(synthetic_app_key, token_server):
    with token_server([503, 201]) as server:
        token = _mint(server)

    assert token == "SYNTHETIC_TEST_VALUE"
    assert server.requests == [_TOKEN_PATH, _TOKEN_PATH]


def test_suspended_installation_is_not_retried(synthetic_app_key, token_server):
    with token_server([403, 201]) as server:
        with pytest.raises(GithubConnectionInvalid):
            _mint(server)

    assert server.requests == [_TOKEN_PATH]


def test_persistent_server_errors_still_raise_github_exception(
    synthetic_app_key, token_server
):
    with token_server([503, 503, 503, 201]) as server:
        with pytest.raises(GithubException) as raised:
            _mint(server)

    assert raised.value.status == 503
    assert len(server.requests) == 3
