"""Alembic environment: runs migrations against ``PROBEATLAS_DATABASE_URL`` using the ORM metadata."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from probeatlas.server.config import get_settings
from probeatlas.server.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    """Return the database URL from the Alembic config, falling back to the application settings."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit migration SQL without connecting (``alembic upgrade --sql``)."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations over a live connection, comparing column types for autogenerate."""
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
