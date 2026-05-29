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


def test_postgres_session_store_save_load_document_delete() -> None:
    store = _store()
    session_id = store.save(
        messages=[{"role": "user", "content": "postgres-session"}],
        model="m1",
        fingerprint="pg-test",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "postgres-session"

    store.save_document(
        session_id,
        {
            "session": {"id": session_id, "title": "Document"},
            "stats": {"taskText": "Document"},
            "turns": [{"id": "t1"}],
            "last_event_seq": 7,
        },
    )
    document = store.load_document(session_id)
    assert document is not None
    assert document["turns"][0]["id"] == "t1"

    listed = store.list(limit=10, fingerprint="pg-test")
    assert any(item.id == session_id for item in listed)
    assert store.delete(session_id) is True
    assert store.load(session_id) is None
    assert store.load_document(session_id) is None


def test_postgres_session_store_writes_single_session_record() -> None:
    engine = _engine()
    store = PostgresSessionStore(engine)
    session_id = store.save(
        messages=[{"role": "user", "content": "postgres-record"}],
        model="m1",
        fingerprint="pg-test",
    )

    try:
        seq = store.append_trace_event(
            session_id,
            "session_run_start",
            {"prompt": "postgres-record"},
            session_run_id="run-record",
            session_run_seq=1,
        )

        assert seq == 1
        record = store.load_record(session_id)
        assert record is not None
        assert record["schema_version"] == 2
        assert record["metadata"]["id"] == session_id
        assert record["history"]["messages"][0]["content"] == "postgres-record"
        assert record["transcript"]["turns"][0]["userMessage"]["text"] == "postgres-record"
        assert record["events"][0]["type"] == "session_run_start"

        with engine.begin() as conn:
            document_count = conn.execute(
                text(
                    """
                    SELECT count(*)
                    FROM labrastro_session_documents
                    WHERE session_id=:session_id
                    """
                ),
                {"session_id": session_id},
            ).scalar_one()
            event_count = conn.execute(
                text(
                    """
                    SELECT count(*)
                    FROM labrastro_session_trace_events
                    WHERE session_id=:session_id
                    """
                ),
                {"session_id": session_id},
            ).scalar_one()
        assert int(document_count or 0) == 0
        assert int(event_count or 0) == 0
    finally:
        store.delete(session_id)


def test_postgres_session_store_trace_events_reduce_to_record_document() -> None:
    engine = _engine()
    store = PostgresSessionStore(engine, payload_compress_threshold_bytes=64)
    session_id = store.save(
        messages=[{"role": "user", "content": "trace-event"}],
        model="m1",
        fingerprint="pg-test",
    )

    try:
        first = store.append_trace_event(
            session_id,
            "session_run_start",
            {"prompt": "hello"},
            session_run_id="run-1",
            session_run_seq=1,
        )
        second = store.append_trace_event(
            session_id,
            "tool_call_end",
            {
                "tool_name": "write_file",
                "tool_call_id": "tool-1",
                "tool_result": "x" * 512,
            },
            session_run_id="run-1",
            session_run_seq=2,
        )

        assert (first, second) == (1, 2)
        assert store.latest_trace_event_seq(session_id) == 2
        events = store.list_trace_events(session_id, after_seq=1)
        assert events[0]["session_event_seq"] == 2
        assert events[0]["session_run_seq"] == 2
        assert events[0]["payload"]["tool_result"] == "x" * 512

        document = store.load_document(session_id)
        assert document is not None
        assert document["last_event_seq"] == 2
        assert document["turns"][0]["userMessage"]["text"] == "hello"
        tool_parts = document["turns"][0]["assistantMessages"][0]["parts"]
        assert tool_parts[0]["type"] == "tool"
        assert tool_parts[0]["output"] == "x" * 512

        with engine.begin() as conn:
            event_count = conn.execute(
                text(
                    """
                    SELECT count(*)
                    FROM labrastro_session_trace_events
                    WHERE session_id=:session_id
                    """
                ),
                {"session_id": session_id},
            ).scalar_one()
        assert int(event_count or 0) == 0

        record = store.load_record(session_id)
        assert record is not None
        assert record["events"][1]["payload"]["tool_result"] == "x" * 512
    finally:
        store.delete(session_id)


def test_postgres_session_store_sanitizes_nul_in_messages_and_trace_events() -> None:
    store = _store()
    session_id = store.save(
        messages=[{"role": "user", "content": "a\x00b"}],
        model="m1",
        fingerprint="pg-test",
    )

    try:
        loaded = store.load(session_id)
        assert loaded is not None
        assert loaded.messages[0]["content"] == "a\ufffdb"

        store.append_trace_event(
            session_id,
            "session_run_start",
            {"prompt": "hi\x00there"},
            session_run_id="run-nul",
            session_run_seq=1,
        )
        store.append_trace_event(
            session_id,
            "tool_call_end",
            {
                "tool_name": "grep",
                "tool_call_id": "tool-nul",
                "tool_result": "x\x00y",
            },
            session_run_id="run-nul",
            session_run_seq=2,
        )

        events = store.list_trace_events(session_id)
        assert events[0]["payload"]["prompt"] == "hi\ufffdthere"
        assert events[1]["payload"]["tool_result"] == "x\ufffdy"

        document = store.load_document(session_id)
        assert document is not None
        assert document["turns"][0]["userMessage"]["text"] == "hi\ufffdthere"
        tool_parts = document["turns"][0]["assistantMessages"][0]["parts"]
        assert tool_parts[0]["output"] == "x\ufffdy"
    finally:
        store.delete(session_id)


def test_postgres_session_store_sanitizes_nul_in_record_payloads() -> None:
    engine = _engine()
    store = PostgresSessionStore(engine, payload_compress_threshold_bytes=64)
    session_id = store.save(
        messages=[{"role": "user", "content": "compressed-nul"}],
        model="m1",
        fingerprint="pg-test",
    )

    try:
        store.append_trace_event(
            session_id,
            "tool_call_end",
            {
                "tool_name": "grep",
                "tool_call_id": "tool-compressed-nul",
                "tool_result": "x\x00" + ("y" * 512),
            },
            session_run_id="run-compressed-nul",
            session_run_seq=1,
        )

        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT record::text AS record_text
                    FROM labrastro_sessions
                    WHERE id=:session_id
                    """
                ),
                {"session_id": session_id},
            ).mappings().one()

        record = store.load_record(session_id)
        assert record is not None
        assert record["events"][0]["payload"]["tool_result"].startswith("x\ufffd")
        assert "\\u0000" not in row["record_text"]
        assert "\x00" not in row["record_text"]
    finally:
        store.delete(session_id)


def test_persistence_maintenance_does_not_touch_session_record_document() -> None:
    engine = _engine()
    store = PostgresSessionStore(engine)
    session_id = store.save(
        messages=[{"role": "user", "content": "maintenance"}],
        model="m1",
        fingerprint="pg-test",
    )

    try:
        store.save_document(
            session_id,
            {"turns": [{"id": "t1"}], "last_event_seq": 1},
        )
        result = PersistenceMaintenanceService(
            engine,
            retention_days=0,
            interval_sec=3600,
        ).run_once()

        assert result.agent_run_events_deleted == 0
        assert store.load_document(session_id) is not None
    finally:
        store.delete(session_id)
