"""Postgres engine helpers for optional durable persistence."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


def normalize_database_url(database_url: str) -> str:
    """Prefer psycopg for plain Postgres URLs."""

    url = str(database_url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@lru_cache(maxsize=8)
def create_postgres_engine(database_url: str) -> Any:
    """Create a SQLAlchemy engine lazily so non-DB users need no imports."""

    try:
        from sqlalchemy import create_engine
    except ImportError as exc:  # pragma: no cover - exercised without extras.
        raise RuntimeError(
            "Postgres persistence requires sqlalchemy and psycopg. "
            "Install package dependencies or disable persistence."
        ) from exc

    return create_engine(
        normalize_database_url(database_url),
        pool_pre_ping=True,
        future=True,
    )

