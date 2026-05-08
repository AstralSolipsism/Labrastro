from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.maintenance import (
    PersistenceMaintenanceService,
)
from labrastro_server.infrastructure.persistence.migration import run_migrations
from labrastro_server.infrastructure.persistence.postgres_session_store import (
    PostgresSessionStore,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("LABRASTRO_TEST_DATABASE_URL"),
    reason="LABRASTRO_TEST_DATABASE_URL is not configured",
)


def _store() -> PostgresSessionStore:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    return PostgresSessionStore(create_postgres_engine(database_url))


def _engine():
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    return create_postgres_engine(database_url)


def test_postgres_session_store_save_load_snapshot_delete() -> None:
    store = _store()
    session_id = store.save(
        messages=[{"role": "user", "content": "postgres-session"}],
        model="m1",
        fingerprint="pg-test",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "postgres-session"

    store.save_snapshot(
        session_id,
        {"turns": [{"id": "t1"}], "traceNodes": [{"id": "n1"}], "traceEdges": []},
    )
    snapshot, error = store.load_snapshot(session_id)
    assert error is None
    assert snapshot is not None
    assert snapshot["turns"][0]["id"] == "t1"

    listed = store.list(limit=10, fingerprint="pg-test")
    assert any(item.id == session_id for item in listed)
    assert store.delete(session_id) is True
    assert store.load(session_id) is None


def test_postgres_session_store_compresses_large_snapshot() -> None:
    engine = _engine()
    store = PostgresSessionStore(engine, snapshot_compress_threshold_bytes=64)
    session_id = store.save(
        messages=[{"role": "user", "content": "compressed-snapshot"}],
        model="m1",
        fingerprint="pg-test",
    )
    snapshot = {"turns": [{"id": "t1", "content": "x" * 512}]}

    try:
        store.save_snapshot(session_id, snapshot)

        loaded, error = store.load_snapshot(session_id)
        assert error is None
        assert loaded == snapshot

        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT snapshot, snapshot_blob, snapshot_encoding, snapshot_bytes
                    FROM labrastro_session_snapshots
                    WHERE session_id=:session_id
                    ORDER BY version DESC
                    LIMIT 1
                    """
                ),
                {"session_id": session_id},
            ).mappings().one()
        assert row["snapshot"] is None
        assert row["snapshot_blob"] is not None
        assert row["snapshot_encoding"] == "json+gzip"
        assert row["snapshot_bytes"] > 64
    finally:
        store.delete(session_id)


def test_persistence_maintenance_trims_snapshot_versions_and_retention() -> None:
    engine = _engine()
    store = PostgresSessionStore(engine)
    session_id = store.save(
        messages=[{"role": "user", "content": "maintenance"}],
        model="m1",
        fingerprint="pg-test",
    )

    try:
        for idx in range(5):
            store.save_snapshot(session_id, {"turns": [{"id": f"t{idx}"}]})

        maintenance = PersistenceMaintenanceService(
            engine,
            retention_days=0,
            snapshot_max_versions_per_session=2,
            interval_sec=3600,
        )
        result = maintenance.run_once()
        assert result.snapshot_versions_deleted >= 3

        with engine.begin() as conn:
            versions = conn.execute(
                text(
                    """
                    SELECT version
                    FROM labrastro_session_snapshots
                    WHERE session_id=:session_id
                    ORDER BY version
                    """
                ),
                {"session_id": session_id},
            ).scalars().all()
            conn.execute(
                text(
                    """
                    UPDATE labrastro_session_snapshots
                    SET created_at = now() - interval '10 day'
                    WHERE session_id=:session_id
                    """
                ),
                {"session_id": session_id},
            )
        assert versions == [4, 5]

        retention = PersistenceMaintenanceService(
            engine,
            retention_days=1,
            snapshot_max_versions_per_session=20,
            interval_sec=3600,
        )
        result = retention.run_once()
        assert result.snapshot_retention_deleted == 1

        with engine.begin() as conn:
            remaining = conn.execute(
                text(
                    """
                    SELECT version
                    FROM labrastro_session_snapshots
                    WHERE session_id=:session_id
                    ORDER BY version
                    """
                ),
                {"session_id": session_id},
            ).scalars().all()
        assert remaining == [5]
    finally:
        store.delete(session_id)

