from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)

ALLOWED_ORIGIN = settings.cors_origins[0]
DISALLOWED_ORIGIN = "https://evil.example"
PREFLIGHT_HEADERS = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
}


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
