from clerk_backend_api.security import authenticate_request_async
from clerk_backend_api.security.types import AuthenticateRequestOptions
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request

from app.config import settings


def get_fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def encrypt_token(token: str) -> str:
    return get_fernet().encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    return get_fernet().decrypt(token.encode()).decode()


async def get_authenticated_user_id(request: Request) -> str:
    """Validate the incoming Clerk session JWT and return the authenticated user id.

    The session token is read from the ``Authorization: Bearer <jwt>`` header (or the
    ``__session`` cookie). When ``clerk_jwt_key`` is configured the token is verified
    locally against that public key; otherwise the SDK fetches Clerk's JWKS using the
    secret key.
    """
    try:
        request_state = await authenticate_request_async(
            request,
            AuthenticateRequestOptions(
                secret_key=settings.clerk_secret_key,
                jwt_key=settings.clerk_jwt_key,
            ),
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not request_state.is_signed_in or request_state.payload is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_id = request_state.payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return user_id
