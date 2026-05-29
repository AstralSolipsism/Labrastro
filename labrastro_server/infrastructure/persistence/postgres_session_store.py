"""Postgres-backed conversation session and document store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
import gzip
import json
import time
import uuid

from reuleauxcoder.domain.context.manager import ensure_message_token_counts
from reuleauxcoder.domain.session.models import (
    Session,
    SessionMetadata,
    SessionRuntimeState,
)
from reuleauxcoder.domain.session.document import (
    apply_session_event,
    empty_session_document,
    session_metadata_from_document,
    update_session_document_metadata,
)
from reuleauxcoder.infrastructure.persistence.session_store import (
    DEFAULT_SESSION_FINGERPRINT,
    SessionStore,
)


try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


def _require_sqlalchemy() -> None:
    if text is None:
        raise RuntimeError("Postgres session store requires sqlalchemy and psycopg.")


def _jsonb_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\uFFFD")
    if isinstance(value, dict):
        return {
            _jsonb_safe(key) if isinstance(key, str) else key: _jsonb_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonb_safe(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_jsonb_safe(value if value is not None else {}), ensure_ascii=False)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonb_safe(value if value is not None else {}),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _saved_at_to_dt(value: str | None) -> datetime:
    if not value or value == "?":
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class PostgresSessionStore:
    """SessionStore-compatible adapter using Postgres as authority."""

    def __init__(
        self,
        engine: Any,
        *,
        payload_compress_threshold_bytes: int = 262144,
    ) -> None:
        _require_sqlalchemy()
        self.engine = engine
        self.payload_compress_threshold_bytes = max(
            1, int(payload_compress_threshold_bytes or 1)
        )

    @staticmethod
    def generate_session_id() -> str:
        return f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def save(
        self,
        messages: list[dict],
        model: str,
        session_id: Optional[str] = None,
        is_exit: bool = False,
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> str:
        if not session_id:
            session_id = self.generate_session_id()
        saved_messages = [dict(message) for message in messages]
        ensure_message_token_counts(saved_messages)
        if not SessionStore.has_history_content(saved_messages):
            self.delete(session_id)
            return session_id
        if is_exit:
            exit_time = time.strftime("%Y-%m-%d %H:%M:%S")
            exit_message = {
                "role": "system",
                "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
            }
            ensure_message_token_counts([exit_message])
            saved_messages.append(exit_message)
        effective_runtime = runtime_state or SessionRuntimeState(
            model=model, active_mode=active_mode
        )
        if effective_runtime.model is None:
            effective_runtime.model = model
        if effective_runtime.active_mode is None:
            effective_runtime.active_mode = active_mode
        session = Session(
            id=session_id,
            model=effective_runtime.model or model,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
            messages=saved_messages,
            active_mode=effective_runtime.active_mode or active_mode,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            runtime_state=effective_runtime,
        )
        with self.engine.begin() as conn:
            self._lock_session(conn, session.id)
            existing_record = self._load_record(conn, session.id, promote_legacy=True)
            document = update_session_document_metadata(
                self._record_document(existing_record),
                session_id=session.id,
                model=session.model,
                saved_at=session.saved_at,
                preview=session.get_preview(),
                fingerprint=session.fingerprint,
                runtime_state=session.runtime_state.to_dict(),
            )
            record = session.to_record(
                transcript=document,
                events=self._record_events(existing_record),
            )
            self._upsert_session_record(
                conn,
                session,
                record,
                has_history_content=True,
            )
        return session_id

    def save_runtime_state(
        self,
        session_id: str,
        model: str,
        runtime_state: SessionRuntimeState,
        *,
        messages: list[dict] | None = None,
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> str:
        saved_messages = [dict(message) for message in messages or []]
        ensure_message_token_counts(saved_messages)
        effective_runtime = runtime_state
        if effective_runtime.model is None:
            effective_runtime.model = model
        if effective_runtime.active_mode is None:
            effective_runtime.active_mode = active_mode
        session = Session(
            id=session_id,
            model=effective_runtime.model or model,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
            messages=saved_messages,
            active_mode=effective_runtime.active_mode or active_mode,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            runtime_state=effective_runtime,
        )
        has_history = SessionStore.has_history_content(saved_messages)
        with self.engine.begin() as conn:
            self._lock_session(conn, session.id)
            existing_record = self._load_record(conn, session.id, promote_legacy=True)
            document = update_session_document_metadata(
                self._record_document(existing_record),
                session_id=session.id,
                model=session.model,
                saved_at=session.saved_at,
                preview=session.get_preview(),
                fingerprint=session.fingerprint,
                runtime_state=session.runtime_state.to_dict(),
            )
            record = session.to_record(
                transcript=document,
                events=self._record_events(existing_record),
            )
            self._upsert_session_record(
                conn,
                session,
                record,
                has_history_content=has_history,
            )
        return session_id

    def append_system_message(
        self,
        session_id: str,
        model: str,
        content: str,
        *,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> None:
        loaded = self.load(session_id)
        if loaded is None:
            self.save(
                messages=[{"role": "system", "content": content}],
                model=model,
                session_id=session_id,
                active_mode=active_mode,
                runtime_state=runtime_state,
                fingerprint=fingerprint,
            )
            return
        messages = list(loaded.messages)
        messages.append({"role": "system", "content": content})
        self.save(
            messages=messages,
            model=loaded.model or model,
            session_id=session_id,
            total_prompt_tokens=loaded.total_prompt_tokens,
            total_completion_tokens=loaded.total_completion_tokens,
            active_mode=loaded.active_mode or active_mode,
            runtime_state=runtime_state or loaded.runtime_state,
            fingerprint=loaded.fingerprint or fingerprint,
        )

    def load(self, session_id: str) -> Session | None:
        with self.engine.begin() as conn:
            self._lock_session(conn, session_id)
            record = self._load_record(conn, session_id, promote_legacy=True)
        if record is None:
            return None
        return self._session_from_record(record)

    def delete(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_sessions
                    SET deleted_at=now(), updated_at=now()
                    WHERE id=:id AND deleted_at IS NULL
                    """
                ),
                {"id": session_id},
            )
            self._delete_legacy_document(conn, session_id)
            self._delete_legacy_trace_events(conn, session_id)
            return int(result.rowcount or 0) > 0

    def list(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> list[SessionMetadata]:
        params: dict[str, Any] = {"limit": max(1, min(100, int(limit or 20)))}
        clauses = ["sessions.deleted_at IS NULL"]
        if fingerprint is not None:
            clauses.append("sessions.fingerprint=:fingerprint")
            params["fingerprint"] = fingerprint
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT sessions.id, sessions.model, sessions.saved_at,
                           sessions.preview, sessions.fingerprint, sessions.record
                    FROM labrastro_sessions sessions
                    WHERE {' AND '.join(clauses)}
                      AND (
                        sessions.has_history_content IS TRUE
                        OR jsonb_array_length(COALESCE(sessions.record->'transcript'->'turns', '[]'::jsonb)) > 0
                      )
                    ORDER BY sessions.saved_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
        sessions: list[SessionMetadata] = []
        for row in rows:
            record = self._row_record(row)
            metadata_record = (
                record.get("metadata") if isinstance(record, dict) else None
            )
            metadata_record = metadata_record if isinstance(metadata_record, dict) else {}
            fallback = {
                "id": str(metadata_record.get("id") or row["id"]),
                "model": str(metadata_record.get("model") or row["model"]),
                "saved_at": str(
                    metadata_record.get("saved_at")
                    or (
                        row["saved_at"].isoformat()
                        if hasattr(row["saved_at"], "isoformat")
                        else str(row["saved_at"])
                    )
                ),
                "preview": str(metadata_record.get("preview") or row["preview"] or ""),
                "fingerprint": str(
                    metadata_record.get("fingerprint")
                    or row["fingerprint"]
                    or DEFAULT_SESSION_FINGERPRINT
                ),
            }
            document = self._record_document(record) or {}
            metadata = (
                session_metadata_from_document(document, fallback)
                if document.get("turns")
                else fallback
            )
            sessions.append(
                SessionMetadata(
                    id=str(metadata["id"]),
                    model=str(metadata["model"]),
                    saved_at=str(metadata["saved_at"]),
                    preview=str(metadata["preview"]),
                    fingerprint=str(metadata["fingerprint"] or DEFAULT_SESSION_FINGERPRINT),
                )
            )
        return sessions

    def get_latest(
        self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT
    ) -> SessionMetadata | None:
        sessions = self.list(limit=1, fingerprint=fingerprint)
        return sessions[0] if sessions else None

    def load_document(self, session_id: str) -> dict | None:
        record = self.load_record(session_id)
        return self._record_document(record)

    def save_document(self, session_id: str, document: dict) -> None:
        if not isinstance(document, dict):
            document = empty_session_document(session_id)
        with self.engine.begin() as conn:
            self._lock_session(conn, session_id)
            record = self._load_record(conn, session_id, promote_legacy=True)
            if record is None:
                session = Session(
                    id=session_id,
                    model=str(document.get("model") or ""),
                    saved_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    messages=[],
                )
                record = session.to_record(transcript=document, events=[])
                self._upsert_session_record(
                    conn,
                    session,
                    record,
                    has_history_content=False,
                )
                return
            session = self._session_from_record(record)
            record = session.to_record(
                transcript=document,
                events=self._record_events(record),
            )
            self._update_record(conn, session_id, record)

    def delete_document(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            self._lock_session(conn, session_id)
            record = self._load_record(conn, session_id, promote_legacy=True)
            had_document = isinstance(self._record_document(record), dict)
            if isinstance(record, dict):
                record["transcript"] = None
                self._update_record(conn, session_id, record)
            legacy_deleted = self._delete_legacy_document(conn, session_id)
            return had_document or legacy_deleted

    def append_trace_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        session_run_id: str | None = None,
        session_run_seq: int | None = None,
        source: str = "remote_session_run",
        replayable: bool = True,
    ) -> int:
        with self.engine.begin() as conn:
            self._lock_session(conn, session_id)
            record = self._load_record(conn, session_id, promote_legacy=True)
            if record is None:
                session = Session(
                    id=session_id,
                    model="",
                    saved_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    messages=[],
                )
                record = session.to_record(
                    transcript=empty_session_document(session_id),
                    events=[],
                )
                has_history = False
            else:
                session = self._session_from_record(record)
                has_history = SessionStore.has_history_content(session.messages)
            seq = self._latest_record_event_seq(record) + 1
            event_payload = _jsonb_safe(payload if isinstance(payload, dict) else {})
            event = {
                "session_id": session_id,
                "session_event_seq": int(seq),
                "seq": int(session_run_seq) if session_run_seq is not None else int(seq),
                "session_run_id": session_run_id,
                "session_run_seq": int(session_run_seq) if session_run_seq is not None else None,
                "type": str(event_type),
                "payload": event_payload if isinstance(event_payload, dict) else {},
                "source": source,
                "replayable": bool(replayable),
            }
            events = self._record_events(record)
            events.append(event)
            document = self._record_document(record) or empty_session_document(
                session_id,
                metadata={
                    "model": session.model,
                    "saved_at": session.saved_at,
                    "preview": session.get_preview(),
                    "fingerprint": session.fingerprint,
                },
                runtime_state=session.runtime_state.to_dict(),
            )
            document = apply_session_event(
                document,
                session_id=session_id,
                event_type=str(event_type),
                payload=event["payload"],
                session_event_seq=int(seq),
                session_run_id=session_run_id,
                session_run_seq=session_run_seq,
            )
            record = session.to_record(transcript=document, events=events)
            self._upsert_session_record(
                conn,
                session,
                record,
                has_history_content=has_history,
            )
            return int(seq)

    def list_trace_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        replayable_only: bool = True,
    ) -> list[dict]:
        record = self.load_record(session_id)
        return self._filter_trace_events(
            self._record_events(record),
            after_seq=after_seq,
            limit=limit,
            replayable_only=replayable_only,
        )

    def latest_trace_event_seq(self, session_id: str) -> int:
        record = self.load_record(session_id)
        return self._latest_record_event_seq(record)

    def delete_trace_events(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            self._lock_session(conn, session_id)
            record = self._load_record(conn, session_id, promote_legacy=True)
            had_events = bool(self._record_events(record))
            if isinstance(record, dict):
                record["events"] = []
                self._update_record(conn, session_id, record)
            legacy_deleted = self._delete_legacy_trace_events(conn, session_id)
            return had_events or legacy_deleted

    def load_record(self, session_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            self._lock_session(conn, session_id)
            return self._load_record(conn, session_id, promote_legacy=True)

    @staticmethod
    def _lock_session(conn: Any, session_id: str) -> None:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:session_id))"),
            {"session_id": session_id},
        )

    def _load_record(
        self,
        conn: Any,
        session_id: str,
        *,
        promote_legacy: bool,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            text(
                """
                SELECT *
                FROM labrastro_sessions
                WHERE id=:id AND deleted_at IS NULL
                """
            ),
            {"id": session_id},
        ).mappings().first()
        if row is None:
            return None

        record = self._row_record(row)
        legacy_document = self._load_legacy_document(conn, session_id)
        legacy_events = self._load_legacy_trace_events(conn, session_id)
        should_promote = False
        if record is None:
            record = self._legacy_row_to_record(row, legacy_document, legacy_events)
            should_promote = True
        else:
            record = dict(record)
            if legacy_document is not None and not isinstance(
                record.get("transcript"), dict
            ):
                record["transcript"] = legacy_document
                should_promote = True
            if legacy_events and self._latest_event_seq(
                legacy_events
            ) > self._latest_event_seq(self._record_events(record)):
                record["events"] = legacy_events
                should_promote = True
        if should_promote and promote_legacy:
            self._update_record(conn, session_id, record)
            self._delete_legacy_document(conn, session_id)
            self._delete_legacy_trace_events(conn, session_id)
        return record

    def _upsert_session_record(
        self,
        conn: Any,
        session: Session,
        record: dict[str, Any],
        *,
        has_history_content: bool,
    ) -> None:
        conn.execute(
            text(
                """
                INSERT INTO labrastro_sessions (
                    id, fingerprint, model, saved_at, preview, messages,
                    runtime_state, active_mode, total_prompt_tokens,
                    total_completion_tokens, has_history_content, record, deleted_at
                ) VALUES (
                    :id, :fingerprint, :model, :saved_at, :preview,
                    CAST(:messages AS JSONB), CAST(:runtime_state AS JSONB),
                    :active_mode, :total_prompt_tokens,
                    :total_completion_tokens, :has_history_content,
                    CAST(:record AS JSONB), NULL
                )
                ON CONFLICT (id) DO UPDATE SET
                    fingerprint=EXCLUDED.fingerprint,
                    model=EXCLUDED.model,
                    saved_at=EXCLUDED.saved_at,
                    preview=EXCLUDED.preview,
                    messages=EXCLUDED.messages,
                    runtime_state=EXCLUDED.runtime_state,
                    active_mode=EXCLUDED.active_mode,
                    total_prompt_tokens=EXCLUDED.total_prompt_tokens,
                    total_completion_tokens=EXCLUDED.total_completion_tokens,
                    has_history_content=EXCLUDED.has_history_content,
                    record=EXCLUDED.record,
                    deleted_at=NULL,
                    updated_at=now()
                """
            ),
            {
                "id": session.id,
                "fingerprint": session.fingerprint,
                "model": session.model,
                "saved_at": _saved_at_to_dt(session.saved_at),
                "preview": session.get_preview(),
                "messages": _json(session.messages),
                "runtime_state": _json(session.runtime_state.to_dict()),
                "active_mode": session.active_mode,
                "total_prompt_tokens": session.total_prompt_tokens,
                "total_completion_tokens": session.total_completion_tokens,
                "has_history_content": bool(has_history_content),
                "record": _json(record),
            },
        )
        self._delete_legacy_document(conn, session.id)
        self._delete_legacy_trace_events(conn, session.id)

    @staticmethod
    def _update_record(conn: Any, session_id: str, record: dict[str, Any]) -> None:
        conn.execute(
            text(
                """
                UPDATE labrastro_sessions
                SET record=CAST(:record AS JSONB), updated_at=now()
                WHERE id=:session_id AND deleted_at IS NULL
                """
            ),
            {"session_id": session_id, "record": _json(record)},
        )

    @staticmethod
    def _row_record(row: Any) -> dict[str, Any] | None:
        raw = row.get("record") if hasattr(row, "get") else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if isinstance(raw, dict) and raw.get("schema_version") == 2:
            return raw
        return None

    @staticmethod
    def _record_document(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            return None
        document = record.get("transcript")
        return document if isinstance(document, dict) else None

    @staticmethod
    def _record_events(record: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(record, dict):
            return []
        events = record.get("events")
        if not isinstance(events, list):
            return []
        return [dict(event) for event in events if isinstance(event, dict)]

    @staticmethod
    def _filter_trace_events(
        events: list[dict[str, Any]],
        *,
        after_seq: int = 0,
        limit: int | None = None,
        replayable_only: bool = True,
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        max_count = max(1, int(limit)) if limit is not None else None
        for event in events:
            seq = int(event.get("session_event_seq") or event.get("seq") or 0)
            if seq <= int(after_seq or 0):
                continue
            if replayable_only and event.get("replayable") is False:
                continue
            filtered.append(dict(event))
            if max_count is not None and len(filtered) >= max_count:
                break
        return filtered

    @classmethod
    def _latest_record_event_seq(cls, record: dict[str, Any] | None) -> int:
        return cls._latest_event_seq(cls._record_events(record))

    @staticmethod
    def _latest_event_seq(events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        return max(int(event.get("session_event_seq") or event.get("seq") or 0) for event in events)

    def _legacy_row_to_record(
        self,
        row: Any,
        document: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session = self._session_from_legacy_row(row)
        return session.to_record(transcript=document, events=events)

    def _load_legacy_document(self, conn: Any, session_id: str) -> dict | None:
        row = conn.execute(
            text(
                """
                SELECT document
                FROM labrastro_session_documents
                WHERE session_id=:session_id
                """
            ),
            {"session_id": session_id},
        ).mappings().first()
        if row is None:
            return None
        document = row.get("document")
        return dict(document) if isinstance(document, dict) else None

    def _load_legacy_trace_events(self, conn: Any, session_id: str) -> list[dict]:
        rows = conn.execute(
            text(
                """
                SELECT session_id, seq, type, payload, session_run_id, session_run_seq,
                       source, replayable, payload_blob, payload_encoding,
                       payload_bytes, created_at
                FROM labrastro_session_trace_events
                WHERE session_id=:session_id
                ORDER BY seq ASC
                """
            ),
            {"session_id": session_id},
        ).mappings().all()
        events: list[dict] = []
        for row in rows:
            payload, _error = self._decode_event_payload(row)
            seq = int(row["seq"] or 0)
            session_run_seq = row.get("session_run_seq")
            events.append(
                {
                    "session_id": str(row["session_id"]),
                    "session_event_seq": seq,
                    "seq": int(session_run_seq or seq),
                    "session_run_id": row.get("session_run_id"),
                    "session_run_seq": int(session_run_seq) if session_run_seq is not None else None,
                    "type": str(row["type"]),
                    "payload": payload if isinstance(payload, dict) else {},
                    "source": str(row.get("source") or "remote_session_run"),
                    "replayable": bool(row.get("replayable")),
                    "created_at": row["created_at"].isoformat()
                    if hasattr(row["created_at"], "isoformat")
                    else str(row["created_at"]),
                }
            )
        return events

    @staticmethod
    def _delete_legacy_document(conn: Any, session_id: str) -> bool:
        result = conn.execute(
            text("DELETE FROM labrastro_session_documents WHERE session_id=:session_id"),
            {"session_id": session_id},
        )
        return int(result.rowcount or 0) > 0

    @staticmethod
    def _delete_legacy_trace_events(conn: Any, session_id: str) -> bool:
        result = conn.execute(
            text("DELETE FROM labrastro_session_trace_events WHERE session_id=:session_id"),
            {"session_id": session_id},
        )
        return int(result.rowcount or 0) > 0

    def _encode_event_payload(
        self, payload: dict
    ) -> tuple[str | None, bytes | None, str, int]:
        raw = _json_bytes(payload)
        if len(raw) >= self.payload_compress_threshold_bytes:
            return None, gzip.compress(raw), "json+gzip", len(raw)
        return raw.decode("utf-8"), None, "jsonb", len(raw)

    @staticmethod
    def _decode_event_payload(row: Any) -> tuple[dict | None, str | None]:
        encoding = str(row.get("payload_encoding") or "jsonb")
        if encoding == "json+gzip":
            blob = row.get("payload_blob")
            if blob is None:
                return None, "payload_blob_missing"
            try:
                raw = gzip.decompress(bytes(blob))
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                return None, "payload_blob_invalid"
        else:
            payload = row.get("payload")
        if isinstance(payload, dict):
            return dict(payload), None
        return None, "payload_not_object"

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        return SessionStore.get_exit_time(messages)

    def _session_from_record(self, record: dict[str, Any]) -> Session:
        session = Session.from_dict(record)
        ensure_message_token_counts(session.messages)
        return session

    def _session_from_legacy_row(self, row: Any) -> Session:
        messages = list(row["messages"] or [])
        ensure_message_token_counts(messages)
        runtime_state = SessionRuntimeState.from_dict(row["runtime_state"])
        saved_at = (
            row["saved_at"].isoformat()
            if hasattr(row["saved_at"], "isoformat")
            else str(row["saved_at"])
        )
        return Session(
            id=str(row["id"]),
            model=str(row["model"]),
            saved_at=saved_at,
            fingerprint=str(row["fingerprint"] or DEFAULT_SESSION_FINGERPRINT),
            messages=messages,
            active_mode=row["active_mode"],
            total_prompt_tokens=int(row["total_prompt_tokens"] or 0),
            total_completion_tokens=int(row["total_completion_tokens"] or 0),
            runtime_state=runtime_state,
        )
