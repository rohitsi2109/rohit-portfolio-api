from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import sqlalchemy_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    # Migrations run against Neon's direct (non-pooled) endpoint: the pooler can't hold
    # the session-level state some DDL needs
    s = get_settings()
    url = s.database_url_direct or s.database_url
    if not url:
        raise RuntimeError("Set DATABASE_URL_DIRECT (or DATABASE_URL) to run migrations")
    return sqlalchemy_url(url)


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
