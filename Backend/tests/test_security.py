import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_session
from app.main import app
from app.security import get_authenticated_user_id


auth_app = FastAPI()


@auth_app.get("/protected")
async def protected_route(
    user_id: Annotated[str, Depends(get_authenticated_user_id)],
) -> dict[str, str]:
    return {"userId": user_id}


client = TestClient(auth_app)


@pytest.fixture
def clerk_signing_key(monkeypatch: pytest.MonkeyPatch):
    """Use a local Clerk JWT key so authentication never calls Clerk's JWKS API."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setattr(settings, "clerk_jwt_key", public_key.decode())
    return private_key


def _session_claims(**claim_overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims = {
        "sub": "user_frontend_123",
        "sid": "sess_test_123",
        "iss": "https://clerk.test",
        "iat": now,
        "nbf": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
    }
    claims.update(claim_overrides)
    return claims


def _session_jwt(signing_key: Any, **claim_overrides: Any) -> str:
    claims = _session_claims(**claim_overrides)
    return jwt.encode(claims, signing_key, algorithm="RS256")


def test_accepts_valid_clerk_session_jwt_from_frontend(clerk_signing_key):
    token = _session_jwt(clerk_signing_key)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"userId": "user_frontend_123"}


def test_accepts_valid_clerk_session_jwt_from_session_cookie(clerk_signing_key):
    token = _session_jwt(clerk_signing_key)

    response = client.get(
        "/protected",
        headers={"Cookie": f"__session={token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"userId": "user_frontend_123"}


@pytest.mark.parametrize(
    "authorization",
    [
        "Token not-a-jwt",
        "Bearer not-a-jwt",
    ],
)
def test_rejects_malformed_authorization_header(clerk_signing_key, authorization):
    response = client.get(
        "/protected",
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_clerk_session_jwt_with_invalid_signature(clerk_signing_key):
    different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _session_jwt(different_key)

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_unsigned_clerk_session_jwt(clerk_signing_key):
    token = jwt.encode(_session_claims(), key=None, algorithm="none")

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_hs256_algorithm_confusion_attack(clerk_signing_key):
    public_key_der = clerk_signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = jwt.encode(_session_claims(), public_key_der, algorithm="HS256")

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_clerk_session_jwt_with_tampered_payload(clerk_signing_key):
    token = _session_jwt(clerk_signing_key)
    encoded_header, encoded_payload, encoded_signature = token.split(".")
    payload = json.loads(jwt.utils.base64url_decode(encoded_payload))
    payload["sub"] = "attacker_123"
    tampered_payload = jwt.utils.base64url_encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    tampered_token = ".".join(
        (encoded_header, tampered_payload, encoded_signature)
    )

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_expired_clerk_session_jwt(clerk_signing_key):
    token = _session_jwt(
        clerk_signing_key,
        exp=datetime.now(UTC) - timedelta(minutes=1),
    )

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_clerk_session_jwt_before_not_before_time(clerk_signing_key):
    token = _session_jwt(
        clerk_signing_key,
        nbf=datetime.now(UTC) + timedelta(minutes=5),
    )

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_clerk_session_jwt_without_user_id(clerk_signing_key):
    token = _session_jwt(clerk_signing_key, sub="")

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_clerk_session_jwt_without_sub_claim(clerk_signing_key):
    claims = _session_claims()
    del claims["sub"]
    token = jwt.encode(claims, clerk_signing_key, algorithm="RS256")

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_rejects_request_without_clerk_session_jwt(clerk_signing_key):
    response = client.get("/protected")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.fixture
def real_app_client():
    def _fake_session():
        session = MagicMock()
        session.exec.return_value.all.return_value = []
        yield session

    app.dependency_overrides[get_session] = _fake_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_real_app_route_enforces_clerk_session_jwt(
    clerk_signing_key,
    real_app_client,
):
    unauthenticated_response = real_app_client.get("/api/v1/journeys")

    token = _session_jwt(clerk_signing_key)
    authenticated_response = real_app_client.get(
        "/api/v1/journeys",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert unauthenticated_response.status_code == 401
    assert unauthenticated_response.json() == {"detail": "Unauthorized"}
    assert authenticated_response.status_code == 200
    assert authenticated_response.json() == []


def test_real_app_route_forbids_verified_user_from_another_users_resource(
    clerk_signing_key,
    real_app_client,
):
    token = _session_jwt(clerk_signing_key)

    response = real_app_client.get(
        "/api/v1/github/connection/another_user",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
