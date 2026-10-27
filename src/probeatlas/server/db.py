"""SQLAlchemy engine and session factories for the control plane."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Return the shared connection-pooled engine for ``PROBEATLAS_DATABASE_URL``."""
    return create_engine(
        get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10,
        # Keep bound parameters (password hashes, token hashes) out of exception messages and logs.
        hide_parameters=True,
    )


@lru_cache
def _sessionmaker() -> sessionmaker[Session]:
    """Return the cached session factory bound to :func:`get_engine`."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def new_session() -> Session:
    """Open a new ORM session. The caller must close it (use it as a context manager)."""
    return _sessionmaker()()


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and closes it after the request."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()
