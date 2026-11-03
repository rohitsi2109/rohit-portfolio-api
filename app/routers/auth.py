"""Admin app login. There is no sign-up: admins are created with `python -m app.cli create-admin`."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AdminUser, RefreshToken
from app.schemas import LoginRequest, RefreshRequest, TokenPair
from app.services import ratelimit as rate_limit
from app.services.leads import now
from app.services.ratelimit import limit_by_ip
from app.services.security import create_access_token, hash_token, new_refresh_token, verify_password

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def issue_tokens(db: Session, admin: AdminUser, device_label: str | None) -> TokenPair:
    access, ttl = create_access_token(admin.id)
    refresh, refresh_hash, expires_at = new_refresh_token()
    db.add(RefreshToken(admin_user_id=admin.id, token_hash=refresh_hash, device_label=device_label,
                        expires_at=expires_at))
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl)


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
        # A rotated-out token was used again: it may have been stolen, so end every session
        db.execute(update(RefreshToken)
                   .where(RefreshToken.admin_user_id == token.admin_user_id, RefreshToken.revoked_at.is_(None))
                   .values(revoked_at=now()))
        db.commit()
        raise invalid
    if token.expires_at <= now() or not token.admin_user.is_active:
        raise invalid
    token.revoked_at = now()
    tokens = issue_tokens(db, token.admin_user, token.device_label)
    db.commit()
    return tokens


@router.post("/logout", status_code=204)
def logout(body: RefreshRequest, db: Session = Depends(get_db)):
    db.execute(update(RefreshToken)
               .where(RefreshToken.token_hash == hash_token(body.refresh_token), RefreshToken.revoked_at.is_(None))
               .values(revoked_at=now()))
    db.commit()
    return Response(status_code=204)
