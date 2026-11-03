"""Fixed-window rate limits, counted in Postgres.

Serverless instances share no memory, so the counters live in the rate_limits table. Keys are
keyed hashes: no raw IP address or email is stored. If the database is unreachable the limit
is skipped (fail open), so a database outage never blocks a booking."""

import hashlib
import hmac
import logging
import random
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import RateLimit

log = logging.getLogger(__name__)

TOO_MANY = "Too many attempts. Please wait a few minutes and try again."


def client_ip(request: Request) -> str:
    # On Vercel, x-forwarded-for is set by the platform; its first entry is the visitor
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _now() -> datetime:
    """The clock the windows are cut from (tests pin it, so a run never straddles a window edge)."""
    return datetime.now(UTC)


def window_start(at: datetime, window_seconds: int) -> datetime:
    """Start of the fixed window containing `at`; windows are aligned to EPOCH."""
    elapsed = int((at - EPOCH).total_seconds())
    return EPOCH + timedelta(seconds=elapsed - elapsed % window_seconds)


def _key(scope: str, subject: str) -> str:
    secret = (get_settings().jwt_secret or "rate-limit").encode()
    return hmac.new(secret, f"{scope}:{subject.lower()}".encode(), hashlib.sha256).hexdigest()


def hit(scope: str, subject: str, limit: int, window_seconds: int) -> None:
    """Count one request for (scope, subject); raise 429 once `limit` is exceeded in the window."""
    at = _now()
    try:
        with get_sessionmaker()() as db:
            stmt = (
                insert(RateLimit)
                .values(key=_key(scope, subject), window_start=window_start(at, window_seconds), hits=1)
                .on_conflict_do_update(index_elements=[RateLimit.key, RateLimit.window_start],
                                       set_={"hits": RateLimit.hits + 1})
                .returning(RateLimit.hits)
            )
            hits = db.execute(stmt).scalar_one()
            if random.random() < 0.02:  # tidy up old windows now and then
                db.execute(delete(RateLimit).where(RateLimit.window_start < at - timedelta(days=1)))
            db.commit()
    except SQLAlchemyError:
        log.exception("rate limit check failed for %s; allowing the request", scope)
        return
    if hits > limit:
        raise HTTPException(429, TOO_MANY, headers={"Retry-After": str(window_seconds)})


def limit_by_ip(scope: str, limit: int, window_seconds: int):
    """FastAPI dependency: at most `limit` requests per visitor IP per window."""
    def dependency(request: Request) -> None:
        hit(scope, client_ip(request), limit, window_seconds)
    return dependency
