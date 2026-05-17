"""Session persistence adapter backed by JSON files."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

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
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir

DEFAULT_SESSION_FINGERPRINT = "local"


class SessionStore:
    """File-backed store for conversation sessions."""

    def __init__(self, sessions_dir: Path | None = None):
        self._sessions_dir = sessions_dir or get_sessions_dir()
        self._lock = threading.RLock()

    @property
    def sessions_dir(self) -> Path:
        """Return the underlying session directory."""
        return self._sessions_dir

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
        """Save conversation to disk and return the session ID."""
        with self._lock:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)

            if not session_id:
                session_id = self.generate_session_id()

            saved_messages = [dict(message) for message in messages]
            ensure_message_token_counts(saved_messages)
            if not self.has_history_content(saved_messages):
                path = self._get_session_path(session_id)
                if path.exists():
                    try:
                        existing = Session.from_dict(
                            json.loads(path.read_text(encoding="utf-8"))
                        )
                    except (json.JSONDecodeError, KeyError):
                        existing = None
                    if existing is not None and not self.has_history_content(
                        existing.messages
                    ):
                        path.unlink()
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
                saved_at=datetime.now().isoformat(timespec="microseconds"),
                fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
                messages=saved_messages,
                active_mode=effective_runtime.active_mode or active_mode,
                total_prompt_tokens=total_prompt_tokens,
                total_completion_tokens=total_completion_tokens,
                runtime_state=effective_runtime,
            )
            path = self._get_session_path(session_id)
            path.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._save_document_metadata(session)
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
        """Persist session runtime overrides without adding transcript messages."""
        with self._lock:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
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
                saved_at=datetime.now().isoformat(timespec="microseconds"),
                fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
                messages=saved_messages,
                active_mode=effective_runtime.active_mode or active_mode,
                total_prompt_tokens=total_prompt_tokens,
                total_completion_tokens=total_completion_tokens,
                runtime_state=effective_runtime,
            )
            path = self._get_session_path(session_id)
            path.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._save_document_metadata(session)
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
        """Append a system message to an existing session, creating it if needed."""
        with self._lock:
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

            updated_messages = list(loaded.messages)
            updated_messages.append({"role": "system", "content": content})
            self.save(
                messages=updated_messages,
                model=loaded.model or model,
                session_id=session_id,
                total_prompt_tokens=loaded.total_prompt_tokens,
                total_completion_tokens=loaded.total_completion_tokens,
                active_mode=loaded.active_mode or active_mode,
                runtime_state=runtime_state or loaded.runtime_state,
                fingerprint=loaded.fingerprint or fingerprint,
            )

    @staticmethod
    def generate_session_id() -> str:
        """Generate a new session ID."""
        return f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def load(self, session_id: str) -> Session | None:
        """Load a saved session."""
        with self._lock:
            path = self._get_session_path(session_id)
            if not path.exists():
                return None

            data = json.loads(path.read_text(encoding="utf-8"))
            session = Session.from_dict(data)
            updated_messages = [dict(message) for message in session.messages]
            ensure_message_token_counts(updated_messages)
            session.messages = updated_messages
            if session.runtime_state.model is None:
                session.runtime_state.model = session.model
            if session.runtime_state.active_mode is None:
                session.runtime_state.active_mode = session.active_mode
            if updated_messages != data.get("messages"):
                path.write_text(
                    json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return session

    def delete(self, session_id: str) -> bool:
        """Delete a saved session file if it exists."""
        with self._lock:
            path = self._get_session_path(session_id)
            if not path.exists():
                return False
            path.unlink()
            self.delete_document(session_id)
            self.delete_trace_events(session_id)
            return True

    def load_document(self, session_id: str) -> dict | None:
        path = self._get_document_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def save_document(self, session_id: str, document: dict) -> None:
        if not isinstance(document, dict):
            document = empty_session_document(session_id)
        path = self._get_document_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def delete_document(self, session_id: str) -> bool:
        path = self._get_document_path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

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
        """Append a replayable session trace event to a sidecar JSONL ledger."""
        with self._lock:
            path = self._get_trace_events_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            seq = self.latest_trace_event_seq(session_id) + 1
            event = {
                "session_id": session_id,
                "session_event_seq": seq,
                "chat_id": chat_id,
                "chat_seq": int(chat_seq) if chat_seq is not None else None,
                "seq": int(chat_seq) if chat_seq is not None else seq,
                "type": str(event_type),
                "payload": payload if isinstance(payload, dict) else {},
                "source": source,
                "replayable": bool(replayable),
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            document = self.load_document(session_id) or empty_session_document(session_id)
            document = apply_session_event(
                document,
                session_id=session_id,
                event_type=str(event_type),
                payload=payload if isinstance(payload, dict) else {},
                session_event_seq=seq,
                chat_id=chat_id,
                chat_seq=chat_seq,
            )
            self.save_document(session_id, document)
            return seq

    def list_trace_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        replayable_only: bool = True,
    ) -> list[dict]:
        """Return persisted session trace events after the given session seq."""
        path = self._get_trace_events_path(session_id)
        if not path.exists():
            return []
        events: list[dict] = []
        max_count = max(1, int(limit)) if limit is not None else None
        with self._lock:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                seq = int(event.get("session_event_seq") or event.get("seq") or 0)
                if seq <= int(after_seq or 0):
                    continue
                if replayable_only and event.get("replayable") is False:
                    continue
                events.append(event)
                if max_count is not None and len(events) >= max_count:
                    break
        return events

    def latest_trace_event_seq(self, session_id: str) -> int:
        events = self.list_trace_events(
            session_id, after_seq=0, limit=None, replayable_only=False
        )
        if not events:
            return 0
        return max(int(event.get("session_event_seq") or event.get("seq") or 0) for event in events)

    def delete_trace_events(self, session_id: str) -> bool:
        path = self._get_trace_events_path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> list[SessionMetadata]:
        """List available sessions, newest first."""
        with self._lock:
            if not self._sessions_dir.exists():
                return []

            ranked_sessions: list[
                tuple[tuple[int, datetime, str], SessionMetadata]
            ] = []
            for file_path in self._sessions_dir.glob("*.json"):
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    session = Session.from_dict(data)
                    if fingerprint is not None and session.fingerprint != fingerprint:
                        continue
                    document = self.load_document(session.id or file_path.stem)
                    if not isinstance(document, dict):
                        continue
                    if not isinstance(document.get("turns"), list) or not document.get("turns"):
                        continue

                    metadata_payload = session_metadata_from_document(
                        document,
                        {
                            "id": session.id or file_path.stem,
                            "model": session.model,
                            "saved_at": session.saved_at,
                            "preview": session.get_preview(),
                            "fingerprint": session.fingerprint,
                        },
                    )
                    metadata = SessionMetadata(
                        id=str(metadata_payload["id"]),
                        model=str(metadata_payload["model"]),
                        saved_at=str(metadata_payload["saved_at"]),
                        preview=str(metadata_payload["preview"]),
                        fingerprint=str(metadata_payload["fingerprint"]),
                    )

                    stat = file_path.stat()
                    try:
                        saved_at_rank = datetime.fromisoformat(session.saved_at)
                    except (TypeError, ValueError):
                        try:
                            saved_at_rank = datetime.strptime(
                                session.saved_at, "%Y-%m-%d %H:%M:%S"
                            )
                        except (TypeError, ValueError):
                            saved_at_rank = datetime.fromtimestamp(0)

                    ranked_sessions.append(
                        ((stat.st_mtime_ns, saved_at_rank, metadata.id), metadata)
                    )
                except (json.JSONDecodeError, KeyError):
                    continue

            ranked_sessions.sort(key=lambda item: item[0], reverse=True)
            return [metadata for _, metadata in ranked_sessions[:limit]]

    def get_latest(
        self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT
    ) -> SessionMetadata | None:
        """Return the most recent session metadata, if any."""
        sessions = self.list(limit=1, fingerprint=fingerprint)
        return sessions[0] if sessions else None

    @staticmethod
    def has_history_content(messages: list[dict]) -> bool:
        """Return whether messages contain user-visible conversation content."""
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in {"user", "assistant", "tool"}:
                continue
            if SessionStore._has_content_value(message.get("content")):
                return True
            if SessionStore._has_content_value(message.get("parts")):
                return True
            if SessionStore._has_content_value(message.get("tool_calls")):
                return True
        return False

    @staticmethod
    def _has_content_value(value) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(SessionStore._has_content_value(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(SessionStore._has_content_value(item) for item in value)
        return True

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        """Extract exit time from persisted session messages, if present."""
        for msg in reversed(messages):
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "") or ""
            match = re.search(r"\[SESSION_EXIT\].* at (.+?)\.$", content)
            if match:
                return match.group(1)
        return None

    def _get_session_path(self, session_id: str) -> Path:
        """Map session ID to JSON file path."""
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        return self._sessions_dir / f"{safe_id}.json"

    def _get_snapshot_path(self, session_id: str) -> Path:
        path = self._get_session_path(session_id)
        return path.with_name(f"{path.stem}.ui.json")

    def _get_document_path(self, session_id: str) -> Path:
        path = self._get_session_path(session_id)
        return path.with_name(f"{path.stem}.document.json")

    def _get_trace_events_path(self, session_id: str) -> Path:
        path = self._get_session_path(session_id)
        return path.with_name(f"{path.stem}.events.jsonl")

    def _save_document_metadata(self, session: Session) -> None:
        document = update_session_document_metadata(
            self.load_document(session.id),
            session_id=session.id,
            model=session.model,
            saved_at=session.saved_at,
            preview=session.get_preview(),
            fingerprint=session.fingerprint,
            runtime_state=session.runtime_state.to_dict(),
        )
        self.save_document(session.id, document)

    @staticmethod
    def _snapshot_event_seq(snapshot: dict) -> int:
        for key in ("eventSeq", "event_seq", "snapshotEventSeq", "snapshot_event_seq"):
            value = snapshot.get(key)
            if value is None:
                continue
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
        return 0
