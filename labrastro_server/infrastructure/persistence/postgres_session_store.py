"""Postgres-backed conversation session and UI snapshot store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
import json
import time
import uuid

from reuleauxcoder.domain.context.manager import ensure_message_token_counts
from reuleauxcoder.domain.session.models import (
    Session,
    SessionMetadata,
    SessionRuntimeState,
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
    ) -> None:
        _require_sqlalchemy()
        self.engine = engine

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
            self.delete_snapshot(session_id)
            return int(result.rowcount or 0) > 0

    def list(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> list[SessionMetadata]:
        params: dict[str, Any] = {"limit": max(1, min(100, int(limit or 20)))}
        clauses = ["deleted_at IS NULL", "has_history_content = TRUE"]
        if fingerprint is not None:
            clauses.append("fingerprint=:fingerprint")
            params["fingerprint"] = fingerprint
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, model, saved_at, preview, fingerprint
                    FROM labrastro_sessions
                    WHERE {' AND '.join(clauses)}
                    ORDER BY saved_at DESC, updated_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
        return [
            SessionMetadata(
                id=str(row["id"]),
                model=str(row["model"]),
                saved_at=row["saved_at"].isoformat()
                if hasattr(row["saved_at"], "isoformat")
                else str(row["saved_at"]),
                preview=str(row["preview"] or ""),
                fingerprint=str(row["fingerprint"] or DEFAULT_SESSION_FINGERPRINT),
            )
            for row in rows
        ]

    def get_latest(
        self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT
    ) -> SessionMetadata | None:
        sessions = self.list(limit=1, fingerprint=fingerprint)
        return sessions[0] if sessions else None

    def load_snapshot(self, session_id: str) -> tuple[dict | None, str | None]:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT snapshot FROM labrastro_session_snapshots
                    WHERE session_id=:session_id
                    ORDER BY version DESC
                    LIMIT 1
                    """
                ),
                {"session_id": session_id},
            ).mappings().first()
        if row is not None:
            snapshot = row["snapshot"]
            if isinstance(snapshot, dict):
                return dict(snapshot), None
            return None, "snapshot_not_object"
        return None, None

    def save_snapshot(self, session_id: str, snapshot: dict) -> None:
        stats = snapshot.get("stats", {}) if isinstance(snapshot.get("stats"), dict) else {}
        trace_nodes = snapshot.get("traceNodes", [])
        trace_edges = snapshot.get("traceEdges", [])
        turns = snapshot.get("turns", [])
        with self.engine.begin() as conn:
            version = conn.execute(
                text(
                    """
                    SELECT COALESCE(max(version), 0) + 1
                    FROM labrastro_session_snapshots
                    WHERE session_id=:session_id
                    """
                ),
                {"session_id": session_id},
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_session_snapshots (
                        session_id, version, snapshot, stats, turn_count,
                        trace_node_count, trace_edge_count
                    ) VALUES (
                        :session_id, :version, CAST(:snapshot AS JSONB),
                        CAST(:stats AS JSONB), :turn_count,
                        :trace_node_count, :trace_edge_count
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "version": int(version),
                    "snapshot": _json(snapshot),
                    "stats": _json(stats),
                    "turn_count": len(turns) if isinstance(turns, list) else 0,
                    "trace_node_count": len(trace_nodes)
                    if isinstance(trace_nodes, list)
                    else 0,
                    "trace_edge_count": len(trace_edges)
                    if isinstance(trace_edges, list)
                    else 0,
                },
            )

    def delete_snapshot(self, session_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM labrastro_session_snapshots WHERE session_id=:session_id"),
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

