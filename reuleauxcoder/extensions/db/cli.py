"""Database maintenance commands for optional Postgres persistence."""

from __future__ import annotations

from pathlib import Path
import sys

from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.migration import (
    current_revision,
    run_migrations,
)
from labrastro_server.infrastructure.persistence.postgres_session_store import (
    PostgresSessionStore,
)
from reuleauxcoder.services.config.loader import ConfigLoader


def _load_database_url(args) -> str:
    config = ConfigLoader.from_path(Path(args.config) if args.config else None)
    database_url = config.persistence.database_url
    if not database_url:
        print("persistence.database_url is required", file=sys.stderr)
        raise SystemExit(1)
    return database_url


def run_db_cli(args) -> int:
    database_url = _load_database_url(args)
    if args.db_command == "migrate":
        run_migrations(database_url)
        print("database migrated")
        return 0
    if args.db_command == "status":
        print(current_revision(database_url) or "unversioned")
        return 0
    if args.db_command == "cleanup":
        engine = create_postgres_engine(database_url)
        days = max(0, int(args.retention_days or 0))
        if days <= 0:
            print("retention_days must be positive for cleanup", file=sys.stderr)
            return 1
        try:
            from sqlalchemy import text
        except ImportError as exc:
            print(f"sqlalchemy required: {exc}", file=sys.stderr)
            return 1
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM labrastro_session_snapshots
                    WHERE created_at < now() - (:days * interval '1 day')
                    """
                ),
                {"days": days},
            )
            conn.execute(
                text(
                    """
                    DELETE FROM labrastro_runtime_events
                    WHERE created_at < now() - (:days * interval '1 day')
                    AND task_id IN (
                        SELECT id FROM labrastro_runtime_tasks
                        WHERE status IN ('completed', 'failed', 'cancelled', 'blocked')
                    )
                    """
                ),
                {"days": days},
            )
        print(f"cleanup_complete retention_days={days}")
        return 0
    print("unknown db command", file=sys.stderr)
    return 1

