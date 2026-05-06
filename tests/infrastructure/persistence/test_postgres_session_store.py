from __future__ import annotations

import os

import pytest

from labrastro_server.infrastructure.persistence.db import create_postgres_engine
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

