"""Alembic migration helpers for Labrastro persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from labrastro_server.infrastructure.persistence.db import normalize_database_url


def migrations_dir() -> Path:
    return Path(__file__).with_name("migrations")


def _alembic_config(database_url: str) -> Any:
    try:
        from alembic.config import Config
    except ImportError as exc:  # pragma: no cover - exercised without extras.
        raise RuntimeError(
            "Database migrations require alembic. Install package dependencies "
            "or disable persistence.auto_migrate."
        ) from exc

    config = Config()
    config.set_main_option("script_location", str(migrations_dir()))
    config.set_main_option("sqlalchemy.url", normalize_database_url(database_url))
    return config


def run_migrations(database_url: str) -> None:
    try:
        from alembic import command
    except ImportError as exc:  # pragma: no cover - exercised without extras.
        raise RuntimeError("Database migrations require alembic.") from exc

    _ensure_alembic_version_capacity(database_url)
    command.upgrade(_alembic_config(database_url), "head")


def _ensure_alembic_version_capacity(database_url: str) -> None:
    try:
        from sqlalchemy import create_engine, text
    except ImportError as exc:  # pragma: no cover - exercised without extras.
        raise RuntimeError("Database migrations require sqlalchemy.") from exc

    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    with engine.begin() as conn:
        if getattr(conn.dialect, "name", "") != "postgresql":
            return
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS alembic_version (
                    version_num VARCHAR(255) NOT NULL PRIMARY KEY
                )
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE alembic_version
                ALTER COLUMN version_num TYPE VARCHAR(255)
                """
            )
        )


def current_revision(database_url: str) -> str | None:
    try:
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine
    except ImportError as exc:  # pragma: no cover - exercised without extras.
        raise RuntimeError("Database migration status requires alembic/sqlalchemy.") from exc

    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()

