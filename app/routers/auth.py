"""Admin app login. There is no sign-up: admins are created with `python -m app.cli create-admin`."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AdminUser, RefreshToken
from app.schemas import LoginRequest, RefreshRequest, SessionOut, TokenPair
from app.services import ratelimit as rate_limit
from app.services.leads import now
from app.services.ratelimit import limit_by_ip
from app.services.security import (
    access_claims,
    create_access_token,
    current_admin,
    hash_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def issue_tokens(db: Session, admin: AdminUser, device_label: str | None,
                 session_started_at: datetime | None = None) -> TokenPair:
    refresh, refresh_hash, expires_at = new_refresh_token()
    row = RefreshToken(admin_user_id=admin.id, token_hash=refresh_hash, device_label=device_label,
                       session_started_at=session_started_at or now(), expires_at=expires_at)
    db.add(row)
    db.flush()
    access, ttl = create_access_token(admin.id, row.id)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl)


def revoke(token: RefreshToken, reason: str) -> None:
    token.revoked_at = now()
    token.revoked_reason = reason


@router.post("/login", response_model=TokenPair,
             dependencies=[Depends(limit_by_ip("login", limit=10, window_seconds=900))])
def login(body: LoginRequest, db: Session = Depends(get_db)):
    # Per account too, so guessing spread across many IPs is still capped
    rate_limit.hit("login-email", body.email, limit=20, window_seconds=3600)
    admin = db.scalar(select(AdminUser).where(AdminUser.email == body.email))
    # verify_password runs (against a dummy hash) even for unknown emails, so timing doesn't leak them
    if not verify_password(admin.password_hash if admin else None, body.password) or not admin.is_active:
        raise HTTPException(401, "Incorrect email or password")
    admin.last_login_at = now()
    tokens = issue_tokens(db, admin, body.device_label)
    db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair,
             dependencies=[Depends(limit_by_ip("refresh", limit=30, window_seconds=900))])
def refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))
    invalid = HTTPException(401, "Session expired, please log in again")
    if token is None:
        raise invalid
    if token.revoked_at is not None:
        if token.revoked_reason == "rotated":
            # A rotated-out token was used again: it may have been stolen, so end every session.
            # (A token ended by logout or "log out device" is just refused.)
            db.execute(update(RefreshToken)
                       .where(RefreshToken.admin_user_id == token.admin_user_id, RefreshToken.revoked_at.is_(None))
                       .values(revoked_at=now(), revoked_reason="reuse_detected"))
            db.commit()
        raise invalid
    if token.expires_at <= now() or not token.admin_user.is_active:
        raise invalid
    revoke(token, "rotated")
    tokens = issue_tokens(db, token.admin_user, token.device_label, token.session_started_at)
    db.commit()
    return tokens


@router.post("/logout", status_code=204)
def logout(body: RefreshRequest, db: Session = Depends(get_db)):
    db.execute(update(RefreshToken)
               .where(RefreshToken.token_hash == hash_token(body.refresh_token), RefreshToken.revoked_at.is_(None))
               .values(revoked_at=now(), revoked_reason="logout"))
    db.commit()
    return Response(status_code=204)


def current_session_id(claims: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(claims["sid"])
    except (KeyError, ValueError):
        return None


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(db: Session = Depends(get_db), admin: AdminUser = Depends(current_admin),
                  claims: dict[str, Any] = Depends(access_claims)):
    """Signed-in devices: one live refresh token per device (rotation revokes the previous one)."""
    current = current_session_id(claims)
    tokens = db.scalars(select(RefreshToken)
                        .where(RefreshToken.admin_user_id == admin.id, RefreshToken.revoked_at.is_(None),
                               RefreshToken.expires_at > now())
                        .order_by(RefreshToken.created_at.desc())).all()
    return [SessionOut(id=t.id, device_label=t.device_label, signed_in_at=t.session_started_at,
                       last_active_at=t.created_at, expires_at=t.expires_at, is_current=t.id == current)
            for t in tokens]


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(session_id: uuid.UUID, db: Session = Depends(get_db), admin: AdminUser = Depends(current_admin)):
    """Log a device out (e.g. a lost phone). It can't refresh again; its current access token
    stops working when it expires (at most ACCESS_TOKEN_MINUTES)."""
    token = db.scalar(select(RefreshToken).where(RefreshToken.id == session_id,
                                                 RefreshToken.admin_user_id == admin.id))
    if token is None or token.revoked_at is not None:
        raise HTTPException(404, "Session not found")
    revoke(token, "revoked")
    db.commit()
    return Response(status_code=204)
