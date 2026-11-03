"""Passwords (argon2id), JWT access tokens and refresh tokens."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AdminUser

_hasher = PasswordHasher()  # argon2id with the library's recommended parameters
# Verified against when the email is unknown, so a login takes the same time either way
_DUMMY_HASH = _hasher.hash("not-a-real-password")
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def _jwt_secret() -> str:
    secret = get_settings().jwt_secret
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET is missing or too short")
    return secret


def create_access_token(admin_user_id: uuid.UUID) -> tuple[str, int]:
    ttl = get_settings().access_token_minutes * 60
    now = datetime.now(UTC)
    claims = {"sub": str(admin_user_id), "type": "access", "iat": now, "exp": now + timedelta(seconds=ttl)}
    return jwt.encode(claims, _jwt_secret(), algorithm="HS256"), ttl


def new_refresh_token() -> tuple[str, str, datetime]:
    """(token for the client, sha256 for the database, expiry)."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(days=get_settings().refresh_token_days)
    return token, hash_token(token), expires_at


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AdminUser:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                                 headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        claims = jwt.decode(creds.credentials, _jwt_secret(), algorithms=["HS256"])
        if claims.get("type") != "access":
            raise unauthorized
        admin_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized
    admin = db.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        raise unauthorized
    return admin
