from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings


def sqlalchemy_url(url: str) -> str:
    """Neon hands out postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver named."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache
def get_engine() -> Engine:
    # NullPool: each serverless invocation opens and closes its own connection;
    # Neon's pooler (the "-pooler" host) does the real pooling.
    return create_engine(sqlalchemy_url(get_settings().database_url), poolclass=NullPool)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session
