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


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value if value is not None else {},
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
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_sessions (
                        id, fingerprint, model, saved_at, preview, messages,
                        runtime_state, active_mode, total_prompt_tokens,
                        total_completion_tokens, has_history_content, deleted_at
                    ) VALUES (
                        :id, :fingerprint, :model, :saved_at, :preview,
                        CAST(:messages AS JSONB), CAST(:runtime_state AS JSONB),
                        :active_mode, :total_prompt_tokens,
                        :total_completion_tokens, TRUE, NULL
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
                        has_history_content=TRUE,
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
                },
            )
            self._upsert_document_metadata(conn, session)
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
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_sessions (
                        id, fingerprint, model, saved_at, preview, messages,
                        runtime_state, active_mode, total_prompt_tokens,
                        total_completion_tokens, has_history_content, deleted_at
                    ) VALUES (
                        :id, :fingerprint, :model, :saved_at, :preview,
                        CAST(:messages AS JSONB), CAST(:runtime_state AS JSONB),
                        :active_mode, :total_prompt_tokens,
                        :total_completion_tokens, :has_history_content, NULL
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
                    "has_history_content": has_history,
                },
            )
            self._upsert_document_metadata(conn, session)
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
            row = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_sessions
                    WHERE id=:id AND deleted_at IS NULL
                    """
                ),
                {"id": session_id},
            ).mappings().first()
        if row is None:
            return None
        return self._session_from_row(row)

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
            self._delete_document(conn, session_id)
            self.delete_trace_events(session_id)
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
                           sessions.preview, sessions.fingerprint,
                           documents.document
                    FROM labrastro_sessions sessions
                    JOIN labrastro_session_documents documents
                      ON documents.session_id = sessions.id
                    WHERE {' AND '.join(clauses)}
                      AND jsonb_array_length(COALESCE(documents.document->'turns', '[]'::jsonb)) > 0
                    ORDER BY documents.updated_at DESC, sessions.saved_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
        sessions: list[SessionMetadata] = []
        for row in rows:
            fallback = {
                "id": str(row["id"]),
                "model": str(row["model"]),
                "saved_at": row["saved_at"].isoformat()
                if hasattr(row["saved_at"], "isoformat")
                else str(row["saved_at"]),
                "preview": str(row["preview"] or ""),
                "fingerprint": str(row["fingerprint"] or DEFAULT_SESSION_FINGERPRINT),
            }
            document = row.get("document") if isinstance(row.get("document"), dict) else {}
            metadata = session_metadata_from_document(document, fallback)
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
        with self.engine.begin() as conn:
            return self._load_document(conn, session_id)

    def save_document(self, session_id: str, document: dict) -> None:
        if not isinstance(document, dict):
            document = empty_session_document(session_id)
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:session_id))"),
                {"session_id": session_id},
            )
            self._upsert_document(
                conn,
                session_id,
                document,
                int(document.get("revision") or 0),
                int(document.get("last_event_seq") or 0),
            )

    def delete_document(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            return self._delete_document(conn, session_id)

    def append_trace_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        chat_id: str | None = None,
        chat_seq: int | None = None,
        source: str = "remote_chat",
        replayable: bool = True,
    ) -> int:
        payload_json, payload_blob, payload_encoding, payload_bytes = (
            self._encode_event_payload(payload if isinstance(payload, dict) else {})
        )
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:session_id))"),
                {"session_id": session_id},
            )
            seq = conn.execute(
                text(
                    """
                    SELECT COALESCE(max(seq), 0) + 1
                    FROM labrastro_session_trace_events
                    WHERE session_id=:session_id
                    """
                ),
                {"session_id": session_id},
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_session_trace_events (
                        session_id, seq, type, payload, chat_id, chat_seq,
                        source, replayable, payload_blob, payload_encoding,
                        payload_bytes
                    ) VALUES (
                        :session_id, :seq, :type, CAST(:payload AS JSONB),
                        :chat_id, :chat_seq, :source, :replayable,
                        :payload_blob, :payload_encoding, :payload_bytes
                    )
                    ON CONFLICT (session_id, seq) DO NOTHING
                    """
                ),
                {
                    "session_id": session_id,
                    "seq": int(seq),
                    "type": str(event_type),
                    "payload": payload_json,
                    "chat_id": chat_id,
                    "chat_seq": int(chat_seq) if chat_seq is not None else None,
                    "source": source,
                    "replayable": bool(replayable),
                    "payload_blob": payload_blob,
                    "payload_encoding": payload_encoding,
                    "payload_bytes": payload_bytes,
                },
            )
            document = self._load_document(conn, session_id)
            if document is None:
                row = conn.execute(
                    text(
                        """
                        SELECT model, saved_at, preview, fingerprint, runtime_state
                        FROM labrastro_sessions
                        WHERE id=:session_id
                        """
                    ),
                    {"session_id": session_id},
                ).mappings().first()
                if row is not None:
                    saved_at = (
                        row["saved_at"].isoformat()
                        if hasattr(row["saved_at"], "isoformat")
                        else str(row["saved_at"])
                    )
                    document = empty_session_document(
                        session_id,
                        metadata={
                            "model": str(row["model"] or ""),
                            "saved_at": saved_at,
                            "preview": str(row["preview"] or ""),
                            "fingerprint": str(row["fingerprint"] or DEFAULT_SESSION_FINGERPRINT),
                        },
                        runtime_state=row.get("runtime_state")
                        if isinstance(row.get("runtime_state"), dict)
                        else {},
                    )
                else:
                    document = empty_session_document(session_id)
            document = apply_session_event(
                document,
                session_id=session_id,
                event_type=str(event_type),
                payload=payload if isinstance(payload, dict) else {},
                session_event_seq=int(seq),
                chat_id=chat_id,
                chat_seq=chat_seq,
            )
            self._upsert_document(
                conn,
                session_id,
                document,
                int(document.get("revision") or 0),
                int(document.get("last_event_seq") or 0),
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
        clauses = ["session_id=:session_id", "seq > :after_seq"]
        params: dict[str, Any] = {
            "session_id": session_id,
            "after_seq": max(0, int(after_seq or 0)),
        }
        if replayable_only:
            clauses.append("replayable IS TRUE")
        limit_sql = ""
        if limit is not None:
            params["limit"] = max(1, int(limit))
            limit_sql = " LIMIT :limit"
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT session_id, seq, type, payload, chat_id, chat_seq,
                           source, replayable, payload_blob, payload_encoding,
                           payload_bytes, created_at
                    FROM labrastro_session_trace_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY seq ASC
                    {limit_sql}
                    """
                ),
                params,
            ).mappings().all()
        events: list[dict] = []
        for row in rows:
            payload, _error = self._decode_event_payload(row)
            seq = int(row["seq"] or 0)
            chat_seq = row.get("chat_seq")
            events.append(
                {
                    "session_id": str(row["session_id"]),
                    "session_event_seq": seq,
                    "seq": int(chat_seq or seq),
                    "chat_id": row.get("chat_id"),
                    "chat_seq": int(chat_seq) if chat_seq is not None else None,
                    "type": str(row["type"]),
                    "payload": payload if isinstance(payload, dict) else {},
                    "source": str(row.get("source") or "remote_chat"),
                    "replayable": bool(row.get("replayable")),
                    "created_at": row["created_at"].isoformat()
                    if hasattr(row["created_at"], "isoformat")
                    else str(row["created_at"]),
                }
            )
        return events

    def latest_trace_event_seq(self, session_id: str) -> int:
        with self.engine.begin() as conn:
            return self._latest_trace_event_seq(conn, session_id)

    def delete_trace_events(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "DELETE FROM labrastro_session_trace_events WHERE session_id=:session_id"
                ),
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
    def _latest_trace_event_seq(conn: Any, session_id: str) -> int:
        value = conn.execute(
            text(
                """
                SELECT COALESCE(max(seq), 0)
                FROM labrastro_session_trace_events
                WHERE session_id=:session_id
                """
            ),
            {"session_id": session_id},
        ).scalar_one()
        return int(value or 0)

    def _load_document(self, conn: Any, session_id: str) -> dict | None:
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

    def _upsert_document_metadata(self, conn: Any, session: Session) -> None:
        existing = self._load_document(conn, session.id)
        document = update_session_document_metadata(
            existing,
            session_id=session.id,
            model=session.model,
            saved_at=session.saved_at,
            preview=session.get_preview(),
            fingerprint=session.fingerprint,
            runtime_state=session.runtime_state.to_dict(),
        )
        self._upsert_document(
            conn,
            session.id,
            document,
            int(document.get("revision") or 0),
            int(document.get("last_event_seq") or 0),
        )

    def _upsert_document(
        self,
        conn: Any,
        session_id: str,
        document: dict,
        revision: int,
        last_event_seq: int,
    ) -> None:
        conn.execute(
            text(
                """
                INSERT INTO labrastro_session_documents (
                    session_id, document, revision, last_event_seq, updated_at
                ) VALUES (
                    :session_id, CAST(:document AS JSONB), :revision,
                    :last_event_seq, now()
                )
                ON CONFLICT (session_id) DO UPDATE SET
                    document=EXCLUDED.document,
                    revision=EXCLUDED.revision,
                    last_event_seq=EXCLUDED.last_event_seq,
                    updated_at=now()
                """
            ),
            {
                "session_id": session_id,
                "document": _json(document),
                "revision": int(revision or 0),
                "last_event_seq": int(last_event_seq or 0),
            },
        )

    @staticmethod
    def _delete_document(conn: Any, session_id: str) -> bool:
        result = conn.execute(
            text("DELETE FROM labrastro_session_documents WHERE session_id=:session_id"),
            {"session_id": session_id},
        )
        return int(result.rowcount or 0) > 0

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        return SessionStore.get_exit_time(messages)

    def _session_from_row(self, row: Any) -> Session:
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

