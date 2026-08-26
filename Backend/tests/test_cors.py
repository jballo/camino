import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.main import app

client = TestClient(app)

REQUIRED_SETTINGS = {
    "database_url": "postgresql://agent:localdev@localhost:5432/onboarding_agent",
    "clerk_wh_key": "whsec_test",
    "clerk_secret_key": "sk_test",
    "gh_app_id": 1,
    "gh_app_client_id": "client",
    "gh_app_secret": "secret",
    "gh_app_private_key": "key",
    "encryption_key": "key",
    "gh_webhook_secret": "secret",
    "openai_api_key": "sk-test",
}
ALLOWED_ORIGIN = settings.cors_origins[0]
DISALLOWED_ORIGIN = "https://evil.example"
PREFLIGHT_HEADERS = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
}


def test_cors_origins_accepts_comma_separated_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://agent:localdev@localhost:5432/onboarding_agent",
                "CLERK_WH_KEY=whsec_test",
                "CLERK_SECRET_KEY=sk_test",
                "GH_APP_ID=1",
                "GH_APP_CLIENT_ID=client",
                "GH_APP_SECRET=secret",
                "GH_APP_PRIVATE_KEY=key",
                "ENCRYPTION_KEY=key",
                "GH_WEBHOOK_SECRET=secret",
                "OPENAI_API_KEY=sk-test",
                "CORS_ORIGINS=http://localhost:3000, https://app.example.com",
            ]
        )
    )

    parsed = Settings(_env_file=env_file)

    assert parsed.cors_origins == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


@pytest.mark.parametrize(
    "cors_origins",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(", ,", id="commas-only"),
        pytest.param([], id="empty-list"),
        pytest.param(["", "   "], id="list-without-origins"),
    ],
)
def test_cors_origins_rejects_values_without_origins(
    cors_origins: str | list[str],
):
    with pytest.raises(
        ValidationError, match="CORS_ORIGINS must contain at least one origin"
    ):
        Settings(
            _env_file=None,
            cors_origins=cors_origins,
            **REQUIRED_SETTINGS,
        )


def test_preflight_allows_configured_origin():
    resp = client.options(
        "/api/v1/agent/ask",
        headers={"Origin": ALLOWED_ORIGIN, **PREFLIGHT_HEADERS},
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") != "true"
    allow_headers = resp.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers


def test_preflight_rejects_unknown_origin():
    resp = client.options(
        "/api/v1/agent/ask",
        headers={"Origin": DISALLOWED_ORIGIN, **PREFLIGHT_HEADERS},
    )

    assert "access-control-allow-origin" not in resp.headers


def test_response_exposes_retry_after_for_allowed_origin():
    resp = client.get("/openapi.json", headers={"Origin": ALLOWED_ORIGIN})

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert resp.headers.get("access-control-allow-credentials") != "true"
    expose = resp.headers.get("access-control-expose-headers", "").lower()
    assert "retry-after" in expose


def test_response_omits_cors_allow_origin_for_unknown_origin():
    resp = client.get("/openapi.json", headers={"Origin": DISALLOWED_ORIGIN})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
