from __future__ import annotations

import gzip
import json
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


def test_postgres_session_store_trace_events_reduce_to_document_and_compress_payload() -> None:
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
            "chat_start",
            {"prompt": "hello"},
            chat_id="chat-1",
            chat_seq=1,
        )
        second = store.append_trace_event(
            session_id,
            "tool_call_end",
            {
                "tool_name": "write_file",
                "tool_call_id": "tool-1",
                "tool_result": "x" * 512,
            },
            chat_id="chat-1",
            chat_seq=2,
        )

        assert (first, second) == (1, 2)
        assert store.latest_trace_event_seq(session_id) == 2
        events = store.list_trace_events(session_id, after_seq=1)
        assert events[0]["session_event_seq"] == 2
        assert events[0]["chat_seq"] == 2
        assert events[0]["payload"]["tool_result"] == "x" * 512

        document = store.load_document(session_id)
        assert document is not None
        assert document["last_event_seq"] == 2
        assert document["turns"][0]["userMessage"]["text"] == "hello"
        tool_parts = document["turns"][0]["assistantMessages"][0]["parts"]
        assert tool_parts[0]["type"] == "tool"
        assert tool_parts[0]["toolOutput"] == "x" * 512

        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT payload, payload_blob, payload_encoding
                    FROM labrastro_session_trace_events
                    WHERE session_id=:session_id AND seq=2
                    """
                ),
                {"session_id": session_id},
            ).mappings().one()
        assert row["payload"] is None
        assert row["payload_blob"] is not None
        assert row["payload_encoding"] == "json+gzip"
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
            "chat_start",
            {"prompt": "hi\x00there"},
            chat_id="chat-nul",
            chat_seq=1,
        )
        store.append_trace_event(
            session_id,
            "tool_call_end",
            {
                "tool_name": "grep",
                "tool_call_id": "tool-nul",
                "tool_result": "x\x00y",
            },
            chat_id="chat-nul",
            chat_seq=2,
        )

        events = store.list_trace_events(session_id)
        assert events[0]["payload"]["prompt"] == "hi\ufffdthere"
        assert events[1]["payload"]["tool_result"] == "x\ufffdy"

        document = store.load_document(session_id)
        assert document is not None
        assert document["turns"][0]["userMessage"]["text"] == "hi\ufffdthere"
        tool_parts = document["turns"][0]["assistantMessages"][0]["parts"]
        assert tool_parts[0]["toolOutput"] == "x\ufffdy"
    finally:
        store.delete(session_id)


def test_postgres_session_store_sanitizes_nul_before_payload_compression() -> None:
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
            chat_id="chat-compressed-nul",
            chat_seq=1,
        )

        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT payload, payload_blob, payload_encoding
                    FROM labrastro_session_trace_events
                    WHERE session_id=:session_id AND seq=1
                    """
                ),
                {"session_id": session_id},
            ).mappings().one()

        assert row["payload"] is None
        assert row["payload_blob"] is not None
        assert row["payload_encoding"] == "json+gzip"
        raw = gzip.decompress(row["payload_blob"])
        assert b"\\u0000" not in raw
        assert b"\x00" not in raw
        assert json.loads(raw.decode("utf-8"))["tool_result"].startswith("x\ufffd")
    finally:
        store.delete(session_id)


def test_persistence_maintenance_does_not_touch_session_documents() -> None:
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
