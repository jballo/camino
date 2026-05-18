from cryptography.fernet import Fernet
from fastapi import HTTPException, Request

from app.config import settings


def get_fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def encrypt_token(token: str) -> str:
    return get_fernet().encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    return get_fernet().decrypt(token.encode()).decode()


def verify_api_key(request: Request) -> None:
    authorization = request.headers.get("authorization")
    if authorization is None or authorization != f"Bearer {settings.backend_api_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")
