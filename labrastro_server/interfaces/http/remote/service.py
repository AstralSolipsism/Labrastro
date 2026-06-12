"""HTTP transport adapter for the remote relay host."""

from __future__ import annotations

import gzip
import json
import logging
import queue
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from labrastro_server.services.admin.service import (
    ConfigReloadHandler,
    ProviderModelsHandler,
    ProviderTestHandler,
    RemoteAdminConfigManager,
    lifecycle_hook_recent_results_from_agent_runs,
)
from labrastro_server.services.auth.service import AuthService
from labrastro_server.interfaces.http.remote.protocol import (
    ApprovalReplyRequest,
    ApprovalReplyResponse,
    SessionRunCancelRequest,
    SessionRunCancelResponse,
    ChatCommandDispatchRequest,
    ChatCommandDispatchResponse,
    SessionRunFollowUpCancelRequest,
    SessionRunFollowUpRequest,
    SessionRunFollowUpResponse,
    SessionRunStartRequest,
    SessionRunStartResponse,
    CleanupResult,
    DisconnectNotice,
    EnvironmentManifestRequest,
    EnvironmentManifestResponse,
    EnvironmentRequirementManifest,
    ExecToolResult,
    Heartbeat,
    MCPArtifactManifest,
    MCPLaunchManifest,
    MCPManifestRequest,
    MCPManifestResponse,
    MCPServerManifest,
    PeerMCPToolsReport,
    RegisterRejected,
    RegisterRequest,
    RelayEnvelope,
    SessionDeleteRequest,
    SessionListRequest,
    SessionLoadRequest,
    SessionModelSwitchRequest,
    SessionNewRequest,
    ToolPreviewResult,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.domain.environment_requirements import (
    environment_requirement_kind_from_id,
    environment_requirement_name_from_id,
    normalize_environment_placement,
    normalize_environment_requirement_id,
    normalize_environment_requirement_kind,
)
from reuleauxcoder.domain.capability_packages import capability_package_is_active
from reuleauxcoder.domain.session.locale import session_notice_text
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from labrastro_server.interfaces.http.remote.routes.admin import RemoteAdminRoutes
from labrastro_server.interfaces.http.remote.routes.artifacts import RemoteArtifactRoutes
from labrastro_server.interfaces.http.remote.routes.auth import RemoteAuthRoutes
from labrastro_server.interfaces.http.remote.routes.base import (
    RemoteRelayBaseHandler,
    RemoteRouteError,
)
from labrastro_server.interfaces.http.remote.routes.chat import RemoteChatRoutes
from labrastro_server.interfaces.http.remote.routes.collaboration import RemoteCollaborationRoutes
from labrastro_server.interfaces.http.remote.routes.capability_packages import (
    RemoteCapabilityPackageRoutes,
)
from labrastro_server.interfaces.http.remote.routes.github import RemoteGitHubRoutes
from labrastro_server.interfaces.http.remote.routes.manifests import RemoteManifestRoutes
from labrastro_server.interfaces.http.remote.routes.peer import RemotePeerRoutes
from labrastro_server.interfaces.http.remote.routes.agent_runs import RemoteAgentRunRoutes
from labrastro_server.interfaces.http.remote.routes.sessions import RemoteSessionRoutes
from labrastro_server.interfaces.http.remote.routes.taskflow import RemoteTaskflowRoutes
from labrastro_server.adapters.reuleauxcoder.taskflow_dispatcher import (
    ReuleauxCoderTaskflowDispatcher,
)
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.github.service import (
    PullRequestService,
    ReconcileService,
    WebhookService,
)
from labrastro_server.taskflow.application.taskflow_service import TaskflowService

logger = logging.getLogger(__name__)


@dataclass
class _BufferedSessionRunEvent:
    event: dict[str, Any]
    size_bytes: int


SessionTraceEventSink = Callable[
    [str, str, dict[str, Any], str | None, int | None, str, bool], int | None
]

_COALESCED_SESSION_RUN_EVENTS = frozenset(
    {"assistant_delta", "reasoning_delta", "tool_call_stream"}
)
_LIVE_ONLY_SESSION_RUN_EVENTS = frozenset({"document_draft_preview_chunk"})
_LIVE_EVENT_FLUSH_INTERVAL_SEC = 0.04
_LIVE_EVENT_MAX_CONTENT_CHARS = 1024


def _is_replayable_session_run_event(event_type: str) -> bool:
    return bool(event_type)


def _session_event_message_key(event_type: str, payload: dict[str, Any]) -> str:
    explicit = str(payload.get("message_key") or "").strip()
    if explicit:
        return explicit
    if event_type == "provider_stream_interrupted":
        return "provider_stream_interrupted.recovering"
    if (
        event_type in {"error", "session_run_failed"}
        and str(payload.get("code") or "").strip() == "capability_package_session_failed"
    ):
        return "capability_package.session_failed"
    return ""


def _normalize_session_run_payload(
    event_type: str,
    payload: dict[str, Any],
    locale: str | None,
) -> dict[str, Any]:
    normalized = dict(payload)
    message_key = _session_event_message_key(event_type, normalized)
    if message_key:
        normalized.setdefault("message_key", message_key)
        if not str(normalized.get("message") or "").strip():
            normalized["message"] = session_notice_text(locale, message_key)
    return normalized


@dataclass
class _PendingLiveSessionRunEvent:
    key: str
    event_type: str
    payload: dict[str, Any]
    content: str
    due_at: float


class _SessionRunEventBuffer:
    _ARTIFACTABLE_PAYLOAD_FIELDS = {
        "changes",
        "content",
        "diagnostics",
        "diff",
        "output",
        "patch_delta",
        "patch_preview",
        "patchDelta",
        "patchPreview",
        "raw_args",
        "rawArgs",
        "sections",
        "text",
        "tool_args",
        "tool_output",
        "tool_result",
        "toolArgs",
        "toolOutput",
        "toolResult",
    }
    _ENVELOPE_PAYLOAD_FIELDS = {
        "approval_id",
        "approvalId",
        "created_at",
        "createdAt",
        "draft_id",
        "draftId",
        "content_length",
        "content_sha256",
        "contentLength",
        "contentSha256",
        "event_id",
        "eventId",
        "format",
        "final",
        "item_id",
        "itemId",
        "last_chunk_seq",
        "lastChunkSeq",
        "message",
        "message_key",
        "path",
        "reason",
        "session_id",
        "session_run_id",
        "sessionId",
        "sessionRunId",
        "snapshot_kind",
        "snapshotKind",
        "status",
        "target_path",
        "targetPath",
        "timestamp",
        "title",
        "tool_call_id",
        "tool_name",
        "tool_source",
        "toolCallId",
        "toolName",
        "toolSource",
        "type",
        "updated_at",
        "updatedAt",
    }

    def __init__(
        self,
        *,
        session_run_id: str,
        artifact_root: Path,
        max_events: int,
        max_payload_bytes: int,
        max_total_bytes: int,
        artifact_excluded_event_types: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.session_run_id = session_run_id
        self.artifact_dir = artifact_root / session_run_id
        self.max_events = max(1, int(max_events or 1))
        self.max_payload_bytes = max(1, int(max_payload_bytes or 1))
        self.max_total_bytes = max(self.max_payload_bytes, int(max_total_bytes or self.max_payload_bytes))
        self.artifact_excluded_event_types = frozenset(artifact_excluded_event_types or ())
        self._items: list[_BufferedSessionRunEvent] = []
        self._total_bytes = 0
        self._dropped_count = 0

    @property
    def first_available_seq(self) -> int:
        if self._items:
            return int(self._items[0].event.get("seq", 0) or 0)
        return 0

    @property
    def latest_seq(self) -> int:
        if self._items:
            return int(self._items[-1].event.get("seq", 0) or 0)
        return 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_event(event)
        size_bytes = self._event_size(normalized)
        self._items.append(_BufferedSessionRunEvent(normalized, size_bytes))
        self._total_bytes += size_bytes
        self._prune()
        return normalized

    def patch_event_metadata(self, event: dict[str, Any]) -> None:
        seq = int(event.get("seq", 0) or 0)
        if seq <= 0:
            return
        metadata = {
            key: event[key]
            for key in ("session_event_seq", "last_event_seq", "document_revision")
            if key in event
        }
        if not metadata:
            return
        for item in self._items:
            if int(item.event.get("seq", 0) or 0) != seq:
                continue
            next_event = {**item.event, **metadata}
            next_size = self._event_size(next_event)
            self._total_bytes += next_size - item.size_bytes
            item.event = next_event
            item.size_bytes = next_size
            return

    def events_after(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        cursor = max(0, int(cursor or 0))
        events = [
            dict(item.event)
            for item in self._items
            if int(item.event.get("seq", 0) or 0) > cursor
        ]
        if (
            self._items
            and self._dropped_count > 0
            and cursor < self.first_available_seq - 1
        ):
            lost_seq = self.first_available_seq - 1
            events.insert(
                0,
                {
                    "session_run_id": self.session_run_id,
                    "seq": lost_seq,
                    "type": "events_lost",
                    "payload": {
                        "first_available_seq": self.first_available_seq,
                        "dropped_count": self._dropped_count,
                    },
                },
            )
        next_cursor = cursor
        if events:
            next_cursor = max(
                int(event.get("seq", 0) or 0)
                for event in events
            )
        else:
            next_cursor = max(cursor, self.latest_seq)
        return events, next_cursor

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item.event) for item in self._items]

    def cleanup_artifacts(self) -> None:
        shutil.rmtree(self.artifact_dir, ignore_errors=True)

    def _normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {"value": payload}
        event_type = str(event.get("type") or "")
        original_payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        should_artifact = (
            event_type not in self.artifact_excluded_event_types
            and (
                len(original_payload_bytes) > self.max_payload_bytes
                or self._requires_artifact_payload(event_type, payload)
            )
        )
        if not should_artifact:
            return {**event, "payload": payload}

        artifact_payload = self._artifact_payload(payload)
        payload_bytes = json.dumps(
            artifact_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        seq = int(event.get("seq", 0) or 0)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = self.artifact_dir / f"{seq}.json.gz"
        artifact_path.write_bytes(gzip.compress(payload_bytes))
        envelope = self._artifact_envelope_payload(payload)
        envelope["artifact_ref"] = {
            "type": "session_run_event_payload",
            "path": str(artifact_path),
            "encoding": "json+gzip",
            "bytes": len(payload_bytes),
            "preview": self._artifact_preview(event_type, payload_bytes),
            "fields": sorted(str(key) for key in artifact_payload.keys()),
        }
        return {
            **event,
            "payload": envelope,
        }

    @staticmethod
    def _requires_artifact_payload(event_type: str, payload: dict[str, Any]) -> bool:
        return event_type == "document_draft_snapshot" and "content" in payload

    @staticmethod
    def _artifact_preview(event_type: str, payload_bytes: bytes) -> str:
        if event_type == "document_draft_snapshot":
            return ""
        return payload_bytes[:4096].decode("utf-8", errors="replace")

    @classmethod
    def _artifact_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_payload = {
            key: value
            for key, value in payload.items()
            if key in cls._ARTIFACTABLE_PAYLOAD_FIELDS
        }
        return artifact_payload or dict(payload)

    @classmethod
    def _artifact_envelope_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key in cls._ENVELOPE_PAYLOAD_FIELDS
        }

    def _prune(self) -> None:
        while (
            len(self._items) > self.max_events
            or self._total_bytes > self.max_total_bytes
        ):
            if not self._items:
                break
            dropped = self._items.pop(0)
            self._total_bytes -= dropped.size_bytes
            self._dropped_count += 1
            self._delete_event_artifact(dropped.event)

    def _delete_event_artifact(self, event: dict[str, Any]) -> None:
        payload = event.get("payload")
        artifact = payload.get("artifact_ref") if isinstance(payload, dict) else None
        if not isinstance(artifact, dict):
            return
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            return

    @staticmethod
    def _event_size(event: dict[str, Any]) -> int:
        return len(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )


def _hydrate_stream_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    if str(event.get("type") or "") != "document_draft_snapshot":
        return event
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    artifact = payload.get("artifact_ref")
    if not isinstance(artifact, dict):
        return event
    artifact_payload = _read_event_artifact_payload(artifact)
    if not isinstance(artifact_payload, dict):
        return event
    return {
        **event,
        "payload": {
            **payload,
            **artifact_payload,
            "artifact_ref": artifact,
        },
    }


def _read_event_artifact_payload(artifact: dict[str, Any]) -> dict[str, Any] | None:
    path = artifact.get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        raw = gzip.decompress(Path(path).read_bytes())
        value = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@dataclass
class _RemoteSessionRun:
    session_run_id: str
    peer_id: str
    session_hint: str | None = None
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_id: str | None = None
    agent_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    client_request_id: str | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    runtime_state: dict[str, Any] = field(default_factory=dict)
    locale: str | None = None
    mentions: list[dict[str, Any]] = field(default_factory=list)
    initial_prompt: str | None = None
    session_id: str | None = None
    status: str = "created"
    last_error: str | None = None
    done: bool = False
    running: bool = False
    seq_next: int = 1
    approval_waiters: dict[str, dict[str, Any]] = field(default_factory=dict)
    approval_resolutions: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_input_waiters: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_input_resolutions: dict[str, dict[str, Any]] = field(default_factory=dict)
    follow_up_tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_ticket: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    cancel_callback: Callable[[str], None] | None = None
    follow_up_callback: Callable[[dict[str, Any]], None] | None = None
    follow_up_cancel_callback: Callable[[str], None] | None = None
    artifact_root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "labrastro-session-run-events"
    )
    max_events: int = 1000
    max_payload_bytes: int = 256 * 1024
    max_total_bytes: int = 4 * 1024 * 1024
    trace_event_sink: SessionTraceEventSink | None = None
    trace_persistence_enabled: bool = False
    last_activity_at: float = field(default_factory=time.time)
    _event_buffer: _SessionRunEventBuffer = field(init=False, repr=False)
    _live_event_buffer: _SessionRunEventBuffer = field(init=False, repr=False)
    _pending_trace_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _pending_live_events: list[_PendingLiveSessionRunEvent] = field(default_factory=list, init=False, repr=False)
    _last_live_flush_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _approval_resolved_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _user_input_resolved_event_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.session_id is None:
            self.session_id = self.session_hint
        self._event_buffer = _SessionRunEventBuffer(
            session_run_id=self.session_run_id,
            artifact_root=self.artifact_root,
            max_events=self.max_events,
            max_payload_bytes=self.max_payload_bytes,
            max_total_bytes=self.max_total_bytes,
        )
        self._live_event_buffer = _SessionRunEventBuffer(
            session_run_id=self.session_run_id,
            artifact_root=self.artifact_root / "_live",
            max_events=self.max_events,
            max_payload_bytes=self.max_payload_bytes,
            max_total_bytes=self.max_total_bytes,
            artifact_excluded_event_types=_LIVE_ONLY_SESSION_RUN_EVENTS,
        )

    @property
    def events(self) -> list[dict[str, Any]]:
        with self.cond:
            self._flush_live_events_locked(time.time(), force=True)
            return self._event_buffer.snapshot()

    def append_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> int:
        with self.cond:
            normalized_payload = _normalize_session_run_payload(
                event_type,
                payload if isinstance(payload, dict) else {},
                self.locale,
            )
            now = time.time()
            if event_type in _COALESCED_SESSION_RUN_EVENTS:
                return self._append_live_event_locked(event_type, normalized_payload, now)
            self._flush_live_events_locked(now, force=True)
            if event_type == "approval_resolved":
                seq = self._append_approval_resolved_event_locked(normalized_payload)
            elif event_type == "user_input_resolved":
                seq = self._append_user_input_resolved_event_locked(normalized_payload)
            else:
                seq = self._append_event_locked(event_type, normalized_payload)
            self._update_status_for_event_locked(event_type, normalized_payload)
            self.last_activity_at = now
            self.cond.notify_all()
            return seq

    def append_live_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> int:
        if event_type not in _LIVE_ONLY_SESSION_RUN_EVENTS:
            raise ValueError(f"unsupported live-only session run event: {event_type}")
        with self.cond:
            normalized_payload = _normalize_session_run_payload(
                event_type,
                payload if isinstance(payload, dict) else {},
                self.locale,
            )
            now = time.time()
            self._flush_live_events_locked(now, force=True)
            seq = self._append_live_only_event_locked(event_type, normalized_payload)
            self.last_activity_at = now
            self.cond.notify_all()
            return seq

    def has_event_type(self, event_type: str) -> bool:
        with self.cond:
            self._flush_live_events_locked(time.time(), force=True)
            return any(event.get("type") == event_type for event in self._event_buffer.snapshot())

    def ensure_session_run_start(self, prompt: str | None = None) -> int | None:
        if self.has_event_type("session_run_start"):
            return None
        return self.append_event(
            "session_run_start",
            {
                "prompt": prompt if prompt is not None else self.initial_prompt or "",
                "mode": self.mode,
                "workflow_mode": self.workflow_mode,
                "taskflow_id": self.taskflow_id,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "locale": self.locale,
                "mentions": list(self.mentions),
            },
        )

    def _append_event_locked(self, event_type: str, payload: dict[str, Any]) -> int:
        seq = self.seq_next
        self.seq_next += 1
        event = {
            "session_run_id": self.session_run_id,
            "seq": seq,
            "type": event_type,
            "payload": payload,
        }
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        locale = payload.get("locale")
        if event_type == "session_run_start" and isinstance(locale, str) and locale:
            self.locale = locale
        durable_event = self._event_buffer.append(event)
        self._persist_or_queue_trace_event(durable_event)
        return seq

    def _append_live_only_event_locked(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        seq = self.seq_next
        self.seq_next += 1
        event = {
            "session_run_id": self.session_run_id,
            "seq": seq,
            "type": event_type,
            "payload": payload,
        }
        self._live_event_buffer.append(event)
        return seq

    def _append_approval_resolved_event_locked(self, payload: dict[str, Any]) -> int:
        approval_id = str(payload.get("approval_id") or "")
        if approval_id:
            if approval_id in self._approval_resolved_event_ids:
                return self._event_buffer.latest_seq
            self._approval_resolved_event_ids.add(approval_id)
        return self._append_event_locked("approval_resolved", payload)

    def _append_user_input_resolved_event_locked(self, payload: dict[str, Any]) -> int:
        input_id = str(payload.get("input_id") or "")
        if input_id:
            if input_id in self._user_input_resolved_event_ids:
                return self._event_buffer.latest_seq
            self._user_input_resolved_event_ids.add(input_id)
        return self._append_event_locked("user_input_resolved", payload)

    def _append_live_event_locked(
        self, event_type: str, payload: dict[str, Any], now: float
    ) -> int:
        content = str(payload.get("content") or "")
        if not content:
            return self._event_buffer.latest_seq
        self._flush_live_events_locked(now)
        key = self._live_event_key(event_type, payload)
        last_flush_at = self._last_live_flush_at.get(key, 0.0)
        if now - last_flush_at >= _LIVE_EVENT_FLUSH_INTERVAL_SEC:
            seq = self._append_event_locked(event_type, dict(payload))
            self._last_live_flush_at[key] = now
            self.last_activity_at = now
            self.cond.notify_all()
            return seq

        pending = self._pending_live_event(key)
        if pending is None:
            pending = _PendingLiveSessionRunEvent(
                key=key,
                event_type=event_type,
                payload={**payload, "content": content},
                content=content,
                due_at=last_flush_at + _LIVE_EVENT_FLUSH_INTERVAL_SEC,
            )
            self._pending_live_events.append(pending)
        else:
            pending.content = f"{pending.content}{content}"
            pending.payload.update({k: v for k, v in payload.items() if k != "content"})
            pending.payload["content"] = pending.content
        if len(pending.content) >= _LIVE_EVENT_MAX_CONTENT_CHARS:
            self._flush_live_events_locked(now, force=True, keys={key})
        self.last_activity_at = now
        return self._event_buffer.latest_seq

    def _pending_live_event(self, key: str) -> _PendingLiveSessionRunEvent | None:
        for event in self._pending_live_events:
            if event.key == key:
                return event
        return None

    def _flush_live_events_locked(
        self,
        now: float,
        *,
        force: bool = False,
        keys: set[str] | None = None,
    ) -> bool:
        if not self._pending_live_events:
            return False
        remaining: list[_PendingLiveSessionRunEvent] = []
        flushed = False
        for pending in self._pending_live_events:
            selected = keys is None or pending.key in keys
            due = now >= pending.due_at
            too_large = len(pending.content) >= _LIVE_EVENT_MAX_CONTENT_CHARS
            if selected and (force or due or too_large):
                self._append_event_locked(pending.event_type, dict(pending.payload))
                self._last_live_flush_at[pending.key] = now
                flushed = True
            else:
                remaining.append(pending)
        self._pending_live_events = remaining
        if flushed:
            self.last_activity_at = now
            self.cond.notify_all()
        return flushed

    def _next_live_flush_delay_locked(self, now: float) -> float | None:
        if not self._pending_live_events:
            return None
        due_at = min(event.due_at for event in self._pending_live_events)
        return max(0.0, due_at - now)

    def _latest_stream_seq_locked(self) -> int:
        return max(self._event_buffer.latest_seq, self._live_event_buffer.latest_seq)

    def _first_available_stream_seq_locked(self) -> int:
        first_values = [
            seq
            for seq in (
                self._event_buffer.first_available_seq,
                self._live_event_buffer.first_available_seq,
            )
            if seq > 0
        ]
        return min(first_values) if first_values else 0

    def _events_after_locked(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        durable_events, durable_cursor = self._event_buffer.events_after(cursor)
        live_events, live_cursor = self._live_event_buffer.events_after(cursor)
        events = sorted(
            [*durable_events, *live_events],
            key=lambda event: int(event.get("seq", 0) or 0),
        )
        events = [_hydrate_stream_event_payload(event) for event in events]
        next_cursor = max(cursor, durable_cursor, live_cursor)
        if events:
            next_cursor = max(
                next_cursor,
                max(int(event.get("seq", 0) or 0) for event in events),
            )
        return events, next_cursor

    def _status_next_cursor_locked(self, cursor: int) -> int:
        return max(cursor, self._latest_stream_seq_locked())

    @staticmethod
    def _live_event_key(event_type: str, payload: dict[str, Any]) -> str:
        if event_type != "tool_call_stream":
            return event_type
        return ":".join(
            [
                event_type,
                str(payload.get("tool_call_id") or ""),
                str(payload.get("tool_name") or ""),
                str(payload.get("stream") or ""),
                str(payload.get("format") or ""),
            ]
        )

    def _update_status_for_event_locked(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        if event_type == "error":
            self.status = "error"
            message = payload.get("message")
            self.last_error = str(message) if message is not None else "error"
        elif event_type == "session_run_cancelled":
            self.status = "cancelled"
            reason = payload.get("reason")
            self.last_error = str(reason) if reason is not None else "session_run_cancelled"
        elif event_type == "session_run_interrupted":
            self.status = "interrupted"
            message = payload.get("message")
            self.last_error = str(message) if message is not None else "provider stream interrupted"

    def _persist_or_queue_trace_event(self, event: dict[str, Any]) -> None:
        if not _is_replayable_session_run_event(str(event.get("type") or "")):
            return
        if (
            self.session_id
            and self.trace_persistence_enabled
            and self.trace_event_sink is not None
        ):
            self._flush_pending_trace_events()
            self._persist_trace_event(event)
            return
        self._pending_trace_events.append(event)

    def enable_trace_persistence(self, session_id: str | None = None) -> None:
        with self.cond:
            if session_id:
                self.session_id = session_id
            self.trace_persistence_enabled = True
            self._flush_pending_trace_events()

    def set_trace_event_sink(self, sink: SessionTraceEventSink | None) -> None:
        with self.cond:
            self.trace_event_sink = sink
            if sink is not None and self.trace_persistence_enabled:
                self._flush_pending_trace_events()

    def _flush_pending_trace_events(self) -> None:
        if (
            not self.session_id
            or self.trace_event_sink is None
            or not self._pending_trace_events
        ):
            return
        pending = self._pending_trace_events
        self._pending_trace_events = []
        for event in pending:
            self._persist_trace_event(event)
            self._event_buffer.patch_event_metadata(event)

    def _persist_trace_event(self, event: dict[str, Any]) -> None:
        if not self.session_id or self.trace_event_sink is None:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {"value": payload}
        try:
            session_event_seq = self.trace_event_sink(
                self.session_id,
                str(event.get("type") or ""),
                payload,
                self.session_run_id,
                int(event.get("seq") or 0),
                "remote_session_run",
                True,
            )
        except Exception:
            try:
                payload_bytes = len(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                )
            except Exception:
                payload_bytes = None
            logger.exception(
                "Failed to persist remote session run trace event",
                extra={
                    "session_id": self.session_id,
                    "session_run_id": self.session_run_id,
                    "event_type": str(event.get("type") or ""),
                    "session_run_seq": int(event.get("seq") or 0),
                    "payload_bytes": payload_bytes,
                },
            )
            return
        if session_event_seq is not None:
            event["session_event_seq"] = int(session_event_seq)
            event["last_event_seq"] = int(session_event_seq)
            event["document_revision"] = int(session_event_seq)

    def wait_events(
        self, cursor: int, timeout_sec: float
    ) -> tuple[list[dict[str, Any]], bool, int]:
        deadline = time.time() + max(timeout_sec, 0.0)
        with self.cond:
            while cursor >= self._latest_stream_seq_locked() and not self.done:
                now = time.time()
                if self._flush_live_events_locked(now):
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                live_delay = self._next_live_flush_delay_locked(time.time())
                wait_timeout = remaining if live_delay is None else min(remaining, live_delay)
                self.cond.wait(timeout=max(wait_timeout, 0.001))
            out, next_cursor = self._events_after_locked(cursor)
            self.last_activity_at = time.time()
            return out, self.done, next_cursor

    def mark_running(self) -> None:
        with self.cond:
            self.running = True
            self.status = "running"
            self.last_activity_at = time.time()

    def mark_done(self, reason: str | None = None) -> None:
        unconsumed: list[dict[str, Any]] = []
        with self.cond:
            self._flush_live_events_locked(time.time(), force=True)
            close_reason = (
                reason
                or self.cancel_reason
                or self.last_error
                or "session_run_closed"
            )
            for event_payload in self._cancel_pending_approvals_locked(close_reason):
                self._append_approval_resolved_event_locked(event_payload)
            for event_payload in self._cancel_pending_user_inputs_locked(close_reason):
                self._append_user_input_resolved_event_locked(event_payload)
            self.running = False
            self.done = True
            if self.status not in {"error", "cancelled", "interrupted"}:
                self.status = "done"
            self.finished_at = time.time()
            self.last_activity_at = self.finished_at
            for ticket in self.follow_up_tickets.values():
                if ticket.get("state") in {"consumed", "cancelled", "unconsumed"}:
                    continue
                ticket["state"] = "unconsumed"
                unconsumed.append(dict(ticket))
            self.cond.notify_all()
        for ticket in unconsumed:
            self.append_event(
                "session_run_follow_up_unconsumed",
                self._follow_up_event_payload(ticket),
            )

    def is_stale(self, now: float, *, closed_ttl_sec: float, idle_ttl_sec: float) -> bool:
        if self.done and self.finished_at is not None:
            return now - self.finished_at > closed_ttl_sec
        return now - self.last_activity_at > idle_ttl_sec

    def cleanup_artifacts(self) -> None:
        self._event_buffer.cleanup_artifacts()
        self._live_event_buffer.cleanup_artifacts()

    def status_payload(self, cursor: int = 0) -> dict[str, Any]:
        cursor = max(0, int(cursor or 0))
        with self.cond:
            next_cursor = self._status_next_cursor_locked(cursor)
            return {
                "ok": True,
                "session_run_id": self.session_run_id,
                "peer_id": self.peer_id,
                "status": self.status,
                "running": self.running,
                "done": self.done,
                "reconnectable": not self.done,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "first_available_seq": self._first_available_stream_seq_locked(),
                "latest_seq": self._latest_stream_seq_locked(),
                "dropped_count": self._event_buffer.dropped_count + self._live_event_buffer.dropped_count,
                "session_id": self.session_id,
                "mode": self.mode,
                "workflow_mode": self.workflow_mode,
                "taskflow_id": self.taskflow_id,
                "agent_id": self.agent_id,
                "runtime_state": dict(self.runtime_state),
                "created_at": self.created_at,
                "last_activity_at": self.last_activity_at,
                "finished_at": self.finished_at,
                "error": self.last_error,
                "recovery": dict(self.recovery_ticket) if self.recovery_ticket else None,
                "approvals": self._pending_approvals_locked(),
                "user_inputs": self._pending_user_inputs_locked(),
            }

    def set_cancel_callback(self, callback: Callable[[str], None]) -> None:
        call_immediately = False
        reason = "session_run_cancelled"
        with self.cond:
            self.cancel_callback = callback
            if self.cancel_requested:
                call_immediately = True
                reason = self.cancel_reason or reason
        if call_immediately:
            callback(reason)

    def request_cancel(self, reason: str = "session_run_cancelled") -> tuple[bool, list[dict[str, Any]]]:
        callback: Callable[[str], None] | None
        first_request = False
        resolved_approvals: list[dict[str, Any]]
        with self.cond:
            if not self.cancel_requested:
                first_request = True
            self.cancel_requested = True
            self.cancel_reason = reason
            resolved_approvals = self._cancel_pending_approvals_locked(reason)
            callback = self.cancel_callback
            self.cond.notify_all()
        if callback is not None:
            callback(reason)
        return first_request, resolved_approvals

    def submit_follow_up(
        self,
        text: str,
        *,
        followup_id: str | None = None,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        message_text = text.strip()
        if not message_text:
            raise ValueError("empty_follow_up")
        callback: Callable[[dict[str, Any]], None] | None = None
        with self.cond:
            ticket_id = followup_id or str(uuid.uuid4())
            existing = self.follow_up_tickets.get(ticket_id)
            if existing is not None:
                return dict(existing)
            ticket = {
                "followup_id": ticket_id,
                "text": message_text,
                "state": "pending",
                "client_request_id": client_request_id,
                "created_at": time.time(),
            }
            self.follow_up_tickets[ticket_id] = ticket
            callback = self.follow_up_callback
            self.cond.notify_all()
        self.append_event(
            "session_run_follow_up_accepted",
            self._follow_up_event_payload(ticket),
        )
        if callback is not None:
            callback(dict(ticket))
        return dict(ticket)

    def set_follow_up_callback(
        self, callback: Callable[[dict[str, Any]], None] | None
    ) -> None:
        with self.cond:
            self.follow_up_callback = callback
            pending = [
                dict(ticket)
                for ticket in self.follow_up_tickets.values()
                if ticket.get("state") == "pending"
            ]
        if callback is not None:
            for ticket in pending:
                callback(ticket)

    def set_follow_up_cancel_callback(
        self, callback: Callable[[str], None] | None
    ) -> None:
        with self.cond:
            self.follow_up_cancel_callback = callback

    def cancel_follow_up(self, followup_id: str, reason: str | None = None) -> bool:
        callback: Callable[[str], None] | None = None
        with self.cond:
            ticket = self.follow_up_tickets.get(followup_id)
            if ticket is None:
                return False
            if ticket.get("state") in {"consumed", "cancelled"}:
                return True
            ticket["state"] = "cancelled"
            ticket["reason"] = reason or "cancelled"
            callback = self.follow_up_cancel_callback
            payload = self._follow_up_event_payload(ticket)
            self.cond.notify_all()
        if callback is not None:
            callback(followup_id)
        self.append_event("session_run_follow_up_cancelled", payload)
        return True

    def mark_follow_up_consumed(self, followup_id: str) -> bool:
        with self.cond:
            ticket = self.follow_up_tickets.get(followup_id)
            if ticket is None or ticket.get("state") in {"consumed", "cancelled", "unconsumed"}:
                return False
            ticket["state"] = "consumed"
            ticket["consumed_at"] = time.time()
            payload = self._follow_up_event_payload(ticket)
            self.cond.notify_all()
        self.append_event("session_run_follow_up_consumed", payload)
        return True

    @staticmethod
    def _follow_up_event_payload(ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "followup_id": ticket.get("followup_id"),
            "client_request_id": ticket.get("client_request_id"),
            "state": ticket.get("state"),
            "text": ticket.get("text"),
            **({"reason": ticket.get("reason")} if ticket.get("reason") else {}),
        }

    def register_recovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.cond:
            ticket = {
                "recovery_id": str(uuid.uuid4()),
                "state": "pending",
                "created_at": time.time(),
                "actions": list(payload.get("recovery_actions") or ["continue", "retry"]),
                "payload": dict(payload),
            }
            self.recovery_ticket = ticket
            self.cond.notify_all()
            return dict(ticket)

    def consume_recovery(self, action: str) -> tuple[str, dict[str, Any]]:
        normalized = action if action in {"continue", "retry"} else "continue"
        with self.cond:
            ticket = self.recovery_ticket
            if ticket is None or ticket.get("state") != "pending":
                raise ValueError("recovery_not_available")
            actions = ticket.get("actions")
            if isinstance(actions, list) and normalized not in actions:
                raise ValueError("recovery_action_unavailable")
            ticket["state"] = "consumed"
            ticket["action"] = normalized
            ticket["consumed_at"] = time.time()
            prompt = self._recovery_prompt(normalized, dict(ticket.get("payload") or {}))
            self.done = False
            self.running = True
            self.status = "running"
            self.finished_at = None
            self.last_error = None
            self.cond.notify_all()
            return prompt, dict(ticket)

    def _recovery_prompt(self, action: str, payload: dict[str, Any]) -> str:
        if action == "retry" and self.initial_prompt:
            return self.initial_prompt
        response = str(payload.get("response") or "").strip()
        if len(response) > 4000:
            response = response[-4000:]
        return (
            "<stream_recovery>\n"
            "The previous chat response was interrupted by the provider stream.\n"
            "Continue from the last visible assistant output without repeating completed text.\n"
            "Last visible assistant output:\n"
            f"{response}\n"
            "</stream_recovery>"
        )

    def register_approval(
        self, approval_id: str, payload: dict[str, Any] | None = None
    ) -> None:
        with self.cond:
            approval_id = str(approval_id)
            self.approval_waiters[approval_id] = {
                "approval_id": approval_id,
                "state": "requested",
                "payload": self._approval_payload(approval_id, payload),
                "registered": True,
            }
            self.approval_resolutions.pop(approval_id, None)

    def resolve_approval(
        self, approval_id: str, decision: str, reason: str | None
    ) -> str | None:
        with self.cond:
            waiter = self.approval_waiters.get(approval_id)
            if waiter is None:
                resolved = self.approval_resolutions.get(approval_id)
                if resolved and resolved.get("decision") == decision:
                    return "already_resolved"
                return None
            if waiter.get("done"):
                if waiter.get("decision") == decision:
                    return "already_resolved"
                return None
            waiter["done"] = True
            waiter["decision"] = decision
            waiter["reason"] = reason
            waiter["state"] = "resolved"
            self._record_approval_resolution_locked(
                approval_id, decision, reason, "resolved"
            )
            self.cond.notify_all()
            return "resolved"

    def wait_approval(
        self, approval_id: str, timeout_sec: float | None = None
    ) -> tuple[str, str | None]:
        deadline = time.time() + timeout_sec if timeout_sec else None
        with self.cond:
            waiter = self.approval_waiters.setdefault(
                approval_id,
                {
                    "approval_id": str(approval_id),
                    "state": "requested",
                    "payload": self._approval_payload(approval_id, None),
                    "registered": False,
                },
            )
            waiter.setdefault("approval_id", str(approval_id))
            while not waiter.get("done"):
                if deadline is None:
                    self.cond.wait(timeout=0.5)
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            decision = str(waiter.get("decision", "deny_once"))
            reason = waiter.get("reason")
            self._record_approval_resolution_locked(
                approval_id,
                decision,
                reason if isinstance(reason, str) else None,
                str(waiter.get("state") or "resolved"),
            )
            self.approval_waiters.pop(approval_id, None)
            return decision, reason if isinstance(reason, str) else None

    def cancel_pending_approvals(self, reason: str) -> list[dict[str, Any]]:
        with self.cond:
            resolved_approvals = self._cancel_pending_approvals_locked(reason)
            self.cond.notify_all()
            return resolved_approvals

    def _cancel_pending_approvals_locked(self, reason: str) -> list[dict[str, Any]]:
        resolved_approvals: list[dict[str, Any]] = []
        for waiter in self.approval_waiters.values():
            if waiter.get("done"):
                continue
            waiter["done"] = True
            waiter["decision"] = "deny_once"
            waiter["reason"] = reason
            waiter["state"] = "cancelled"
            self._record_approval_resolution_locked(
                str(waiter.get("approval_id") or ""),
                "deny_once",
                reason,
                "cancelled",
            )
            if waiter.get("registered"):
                event_payload = self._approval_resolved_event_payload_locked(
                    waiter,
                    "deny_once",
                    reason,
                )
                if event_payload:
                    resolved_approvals.append(event_payload)
        return resolved_approvals

    def _approval_resolved_event_payload_locked(
        self,
        waiter: dict[str, Any],
        decision: str,
        reason: str | None,
    ) -> dict[str, Any]:
        approval_id = str(waiter.get("approval_id") or "")
        payload = self._approval_payload(
            approval_id,
            waiter.get("payload") if isinstance(waiter.get("payload"), dict) else None,
        )
        resolved: dict[str, Any] = {
            "approval_id": str(payload.get("approval_id") or approval_id),
            "decision": decision,
        }
        tool_call_id = payload.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            resolved["tool_call_id"] = tool_call_id
        if reason is not None:
            resolved["reason"] = reason
        return resolved

    @staticmethod
    def _approval_payload(
        approval_id: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        out = dict(payload) if isinstance(payload, dict) else {}
        out["approval_id"] = str(out.get("approval_id") or approval_id)
        return out

    def _pending_approvals_locked(self) -> list[dict[str, Any]]:
        approvals: list[dict[str, Any]] = []
        for approval_id, waiter in self.approval_waiters.items():
            if waiter.get("done"):
                continue
            payload = self._approval_payload(
                approval_id,
                waiter.get("payload") if isinstance(waiter.get("payload"), dict) else None,
            )
            payload["state"] = str(waiter.get("state") or "requested")
            approvals.append(payload)
        return approvals

    def _record_approval_resolution_locked(
        self,
        approval_id: str,
        decision: str,
        reason: str | None,
        state: str,
    ) -> None:
        if not approval_id:
            return
        self.approval_resolutions[approval_id] = {
            "approval_id": approval_id,
            "decision": decision,
            "reason": reason,
            "state": state,
        }

    def register_user_input(
        self, input_id: str, payload: dict[str, Any] | None = None
    ) -> None:
        with self.cond:
            input_id = str(input_id)
            self.user_input_waiters[input_id] = {
                "input_id": input_id,
                "state": "requested",
                "payload": self._user_input_payload(input_id, payload),
                "registered": True,
            }
            self.user_input_resolutions.pop(input_id, None)

    def resolve_user_input(
        self,
        input_id: str,
        action: str,
        content: dict[str, Any] | None,
        reason: str | None,
    ) -> str | None:
        with self.cond:
            input_id = str(input_id)
            waiter = self.user_input_waiters.get(input_id)
            normalized_content = dict(content) if isinstance(content, dict) else {}
            if waiter is None:
                resolved = self.user_input_resolutions.get(input_id)
                if (
                    resolved
                    and resolved.get("action") == action
                    and resolved.get("content") == normalized_content
                ):
                    return "already_resolved"
                return None
            if waiter.get("done"):
                if (
                    waiter.get("action") == action
                    and waiter.get("content") == normalized_content
                ):
                    return "already_resolved"
                return None
            waiter["done"] = True
            waiter["action"] = action
            waiter["content"] = normalized_content
            waiter["reason"] = reason
            waiter["state"] = "resolved"
            self._record_user_input_resolution_locked(
                input_id, action, normalized_content, reason, "resolved"
            )
            self.cond.notify_all()
            return "resolved"

    def wait_user_input(
        self, input_id: str, timeout_sec: float | None = None
    ) -> tuple[str, dict[str, Any], str | None]:
        deadline = time.time() + timeout_sec if timeout_sec else None
        with self.cond:
            input_id = str(input_id)
            waiter = self.user_input_waiters.setdefault(
                input_id,
                {
                    "input_id": input_id,
                    "state": "requested",
                    "payload": self._user_input_payload(input_id, None),
                    "registered": False,
                },
            )
            waiter.setdefault("input_id", input_id)
            while not waiter.get("done"):
                if deadline is None:
                    self.cond.wait(timeout=0.5)
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    waiter["done"] = True
                    waiter["action"] = "decline"
                    waiter["content"] = {}
                    waiter["reason"] = "user_input_timeout"
                    waiter["state"] = "timed_out"
                    break
                self.cond.wait(timeout=remaining)
            action = str(waiter.get("action") or "decline")
            content = waiter.get("content")
            if not isinstance(content, dict):
                content = {}
            reason = waiter.get("reason")
            self._record_user_input_resolution_locked(
                input_id,
                action,
                dict(content),
                reason if isinstance(reason, str) else None,
                str(waiter.get("state") or "resolved"),
            )
            self.user_input_waiters.pop(input_id, None)
            return action, dict(content), reason if isinstance(reason, str) else None

    def cancel_pending_user_inputs(self, reason: str) -> list[dict[str, Any]]:
        with self.cond:
            resolved_inputs = self._cancel_pending_user_inputs_locked(reason)
            self.cond.notify_all()
            return resolved_inputs

    def _cancel_pending_user_inputs_locked(self, reason: str) -> list[dict[str, Any]]:
        resolved_inputs: list[dict[str, Any]] = []
        for waiter in self.user_input_waiters.values():
            if waiter.get("done"):
                continue
            waiter["done"] = True
            waiter["action"] = "cancel"
            waiter["content"] = {}
            waiter["reason"] = reason
            waiter["state"] = "cancelled"
            self._record_user_input_resolution_locked(
                str(waiter.get("input_id") or ""),
                "cancel",
                {},
                reason,
                "cancelled",
            )
            if waiter.get("registered"):
                event_payload = self._user_input_resolved_event_payload_locked(
                    waiter,
                    "cancel",
                    {},
                    reason,
                )
                if event_payload:
                    resolved_inputs.append(event_payload)
        return resolved_inputs

    def _pending_user_inputs_locked(self) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []
        for waiter in self.user_input_waiters.values():
            if waiter.get("done"):
                continue
            input_id = str(waiter.get("input_id") or "")
            payload = self._user_input_payload(
                input_id,
                waiter.get("payload") if isinstance(waiter.get("payload"), dict) else None,
            )
            payload["state"] = str(waiter.get("state") or "requested")
            inputs.append(payload)
        return inputs

    def _user_input_resolved_event_payload_locked(
        self,
        waiter: dict[str, Any],
        action: str,
        content: dict[str, Any],
        reason: str | None,
    ) -> dict[str, Any]:
        input_id = str(waiter.get("input_id") or "")
        payload = self._user_input_payload(
            input_id,
            waiter.get("payload") if isinstance(waiter.get("payload"), dict) else None,
        )
        resolved: dict[str, Any] = {
            "input_id": str(payload.get("input_id") or input_id),
            "action": action,
            "content": dict(content),
        }
        kind = payload.get("kind")
        if isinstance(kind, str) and kind:
            resolved["kind"] = kind
        if reason is not None:
            resolved["reason"] = reason
        return resolved

    @staticmethod
    def _user_input_payload(
        input_id: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        out = dict(payload) if isinstance(payload, dict) else {}
        out["input_id"] = str(out.get("input_id") or input_id)
        return out

    def _record_user_input_resolution_locked(
        self,
        input_id: str,
        action: str,
        content: dict[str, Any],
        reason: str | None,
        state: str,
    ) -> None:
        if not input_id:
            return
        self.user_input_resolutions[input_id] = {
            "input_id": input_id,
            "action": action,
            "content": dict(content),
            "reason": reason,
            "state": state,
        }


class RemoteRelayHTTPService:
    """Expose ``RelayServer`` over a minimal HTTP API for remote peers."""

    def __init__(
        self,
        relay_server: RelayServer,
        bind: str,
        *,
        ui_bus: UIEventBus | None = None,
        artifact_provider: Callable[..., Any] | None = None,
        session_run_events_handler: Callable[[str, str, _RemoteSessionRun], None]
        | None = None,
        chat_command_handler: Callable[
            [str, ChatCommandDispatchRequest], ChatCommandDispatchResponse
        ]
        | None = None,
        session_handler: Callable[[str, str, dict[str, Any]], dict[str, Any]]
        | None = None,
        session_history_status_provider: Callable[[], dict[str, Any]] | None = None,
        auth_service: AuthService | None = None,
        bootstrap_token_ttl_sec: int = 300,
        mcp_servers: list[Any] | None = None,
        mcp_artifact_root: str | Path = ".rcoder/mcp-artifacts",
        environment_requirements: dict[str, Any] | None = None,
        capability_packages: dict[str, Any] | None = None,
        environment_requirement_scope_ids: dict[str, set[str]] | None = None,
        admin_config_path: str | Path | None = None,
        admin_config_reload_handler: ConfigReloadHandler | None = None,
        admin_provider_test_handler: ProviderTestHandler | None = None,
        admin_provider_models_handler: ProviderModelsHandler | None = None,
        runtime_control_plane: Any | None = None,
        taskflow_service: TaskflowService | None = None,
        issue_assignment_service: IssueAssignmentService | None = None,
        github_pr_service: PullRequestService | None = None,
        persistence_maintenance_service: Any | None = None,
        max_request_body_bytes: int = 16 * 1024 * 1024,
        session_run_max_events: int = 1000,
        session_run_max_payload_bytes: int = 256 * 1024,
        session_run_max_total_bytes: int = 4 * 1024 * 1024,
        session_run_closed_ttl_sec: float = 300.0,
        session_run_idle_ttl_sec: float = 30 * 60.0,
        session_run_gc_interval_sec: float = 30.0,
        session_run_artifact_root: str | Path | None = None,
        require_explicit_chat_model: bool = False,
        require_peer_runtime_context: bool = False,
    ) -> None:
        self.relay_server = relay_server
        self.bind = bind
        self.ui_bus = ui_bus
        self.artifact_provider = artifact_provider
        self.session_run_events_handler = session_run_events_handler
        self.chat_command_handler = chat_command_handler
        self.session_handler = session_handler
        self.session_trace_event_sink: SessionTraceEventSink | None = None
        self.session_history_status_provider = session_history_status_provider
        if auth_service is None:
            raise ValueError("auth_service is required")
        self.auth_service = auth_service
        self.bootstrap_token_ttl_sec = bootstrap_token_ttl_sec
        self.mcp_servers = list(mcp_servers or [])
        self.mcp_artifact_root = Path(mcp_artifact_root)
        self.environment_requirements = dict(environment_requirements or {})
        self.capability_packages = dict(capability_packages or {})
        self.environment_requirement_scope_ids = (
            _normalize_environment_requirement_scope_ids(
                environment_requirement_scope_ids
            )
        )
        self.admin_manager = RemoteAdminConfigManager(
            Path(admin_config_path) if admin_config_path is not None else None,
            reload_handler=admin_config_reload_handler,
            provider_test_handler=admin_provider_test_handler,
            provider_models_handler=admin_provider_models_handler,
            lifecycle_hook_results_provider=(
                (
                    lambda source, owner_id, runtime=runtime_control_plane: (
                        lifecycle_hook_recent_results_from_agent_runs(
                            runtime,
                            source,
                            owner_id,
                        )
                    )
                )
                if runtime_control_plane is not None
                else None
            ),
        )
        self.runtime_control_plane = runtime_control_plane
        self.taskflow_service = taskflow_service or TaskflowService(
            dispatcher=(
                ReuleauxCoderTaskflowDispatcher(runtime_control_plane)
                if runtime_control_plane is not None
                else None
            )
        )
        self.issue_assignment_service = (
            issue_assignment_service
            or IssueAssignmentService(taskflow_service=self.taskflow_service)
        )
        self.github_pr_service = github_pr_service
        self.github_webhook_service = (
            WebhookService(config=github_pr_service.config, pr_service=github_pr_service)
            if github_pr_service is not None
            else None
        )
        self.github_reconcile_service = (
            ReconcileService(github_pr_service)
            if github_pr_service is not None
            else None
        )
        self.persistence_maintenance_service = persistence_maintenance_service
        self.require_explicit_chat_model = bool(require_explicit_chat_model)
        self.require_peer_runtime_context = bool(require_peer_runtime_context)
        self.max_request_body_bytes = max(1, int(max_request_body_bytes or 1))
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._queues: dict[str, queue.Queue[RelayEnvelope]] = {}
        self._queues_lock = threading.Lock()
        self._peer_chat_locks: dict[str, threading.Lock] = {}
        self._peer_chat_locks_lock = threading.Lock()
        self._capability_package_peer_results: dict[
            str, dict[str, dict[str, Any]]
        ] = {}
        self._capability_package_peer_results_lock = threading.Lock()
        self._session_runs: dict[str, _RemoteSessionRun] = {}
        self._session_runs_lock = threading.Lock()
        self._session_run_max_events = max(1, int(session_run_max_events or 1))
        self._session_run_max_payload_bytes = max(1, int(session_run_max_payload_bytes or 1))
        self._session_run_max_total_bytes = max(
            self._session_run_max_payload_bytes,
            int(session_run_max_total_bytes or self._session_run_max_payload_bytes),
        )
        self._session_run_closed_ttl_sec = max(0.0, float(session_run_closed_ttl_sec))
        self._session_run_idle_ttl_sec = max(0.0, float(session_run_idle_ttl_sec))
        self._session_run_gc_interval_sec = max(1.0, float(session_run_gc_interval_sec))
        self._session_run_artifact_root = (
            Path(session_run_artifact_root)
            if session_run_artifact_root is not None
            else Path(tempfile.gettempdir()) / "labrastro-session-run-events"
        )
        self._session_run_gc_stop = threading.Event()
        self._session_run_gc_thread: threading.Thread | None = None
        self._agent_run_recovery_stop = threading.Event()
        self._agent_run_recovery_thread: threading.Thread | None = None
        self._agent_run_recovery_interval_sec = 2.0
        self._github_reconcile_stop = threading.Event()
        self._github_reconcile_thread: threading.Thread | None = None
        self.relay_server._send_fn = self._enqueue_outbound

    @property
    def base_url(self) -> str:
        host, port = _parse_bind(self.bind)
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._server is not None:
            return
        host, port = _parse_bind(self.bind)
        handler_cls = self._build_handler()
        self._server = ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._start_session_run_gc()
        self._start_persistence_maintenance()
        self._start_agent_run_recovery()
        self._start_github_reconcile()
        self._start_model_capability_sync()
        if self.ui_bus is not None:
            self.ui_bus.info(
                f"Remote relay HTTP service listening on {self.base_url}",
                kind=UIEventKind.REMOTE,
            )

    def stop(self) -> None:
        self._stop_model_capability_sync()
        self._stop_github_reconcile()
        self._stop_agent_run_recovery()
        self._stop_persistence_maintenance()
        self._stop_session_run_gc()
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._server = None

    def _start_agent_run_recovery(self) -> None:
        runtime_control_plane = self.runtime_control_plane
        if runtime_control_plane is None:
            return
        if self._agent_run_recovery_thread is not None:
            return
        self._agent_run_recovery_stop.clear()

        def loop() -> None:
            while not self._agent_run_recovery_stop.wait(
                self._agent_run_recovery_interval_sec
            ):
                try:
                    runtime_control_plane.recover_stale_agent_runs()
                except Exception:
                    continue

        self._agent_run_recovery_thread = threading.Thread(target=loop, daemon=True)
        self._agent_run_recovery_thread.start()

    def _stop_agent_run_recovery(self) -> None:
        self._agent_run_recovery_stop.set()
        if self._agent_run_recovery_thread is not None:
            self._agent_run_recovery_thread.join(timeout=3)
            self._agent_run_recovery_thread = None

    def _start_persistence_maintenance(self) -> None:
        service = self.persistence_maintenance_service
        if service is None:
            return
        start = getattr(service, "start", None)
        if callable(start):
            start()

    def _stop_persistence_maintenance(self) -> None:
        service = self.persistence_maintenance_service
        if service is None:
            return
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()

    def _start_session_run_gc(self) -> None:
        if self._session_run_gc_thread is not None:
            return
        self._session_run_gc_stop.clear()

        def loop() -> None:
            while not self._session_run_gc_stop.wait(self._session_run_gc_interval_sec):
                self._gc_session_runs()

        self._session_run_gc_thread = threading.Thread(target=loop, daemon=True)
        self._session_run_gc_thread.start()

    def _stop_session_run_gc(self) -> None:
        self._session_run_gc_stop.set()
        if self._session_run_gc_thread is not None:
            self._session_run_gc_thread.join(timeout=3)
            self._session_run_gc_thread = None

    def _start_github_reconcile(self) -> None:
        if self.github_pr_service is None or self.github_reconcile_service is None:
            return
        github_reconcile_service = self.github_reconcile_service
        if github_reconcile_service is None:
            return
        interval = max(
            1,
            int(getattr(self.github_pr_service.config, "reconcile_interval_sec", 300) or 300),
        )
        if self._github_reconcile_thread is not None:
            return
        self._github_reconcile_stop.clear()

        def loop() -> None:
            while not self._github_reconcile_stop.wait(interval):
                try:
                    github_reconcile_service.reconcile()
                except Exception:
                    continue

        self._github_reconcile_thread = threading.Thread(target=loop, daemon=True)
        self._github_reconcile_thread.start()

    def _stop_github_reconcile(self) -> None:
        self._github_reconcile_stop.set()
        if self._github_reconcile_thread is not None:
            self._github_reconcile_thread.join(timeout=3)
            self._github_reconcile_thread = None

    def _start_model_capability_sync(self) -> None:
        settings = self.admin_manager.model_capabilities_settings()
        self.admin_manager.model_capability_catalog.start_periodic(
            enabled=bool(settings["enabled"]),
            interval_sec=int(settings["interval_sec"]),
        )

    def _stop_model_capability_sync(self) -> None:
        self.admin_manager.model_capability_catalog.stop_periodic()

    def issue_bootstrap_token(
        self, ttl_sec: int = 300, claims: dict[str, Any] | None = None
    ) -> str:
        return self.relay_server.issue_bootstrap_token(
            ttl_sec=ttl_sec, claims=claims
        )

    def set_session_run_events_handler(
        self,
        handler: Callable[[str, str, _RemoteSessionRun], None] | None,
    ) -> None:
        self.session_run_events_handler = handler

    def set_chat_command_handler(
        self,
        handler: Callable[[str, ChatCommandDispatchRequest], ChatCommandDispatchResponse]
        | None,
    ) -> None:
        self.chat_command_handler = handler

    def set_session_handler(
        self,
        handler: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None,
    ) -> None:
        self.session_handler = handler

    def _create_session_run(
        self,
        peer_id: str,
        session_hint: str | None = None,
        *,
        mode: str | None = None,
        workflow_mode: str | None = None,
        taskflow_id: str | None = None,
        agent_id: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        client_request_id: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        runtime_state: dict[str, Any] | None = None,
        locale: str | None = None,
        mentions: list[dict[str, Any]] | None = None,
        initial_prompt: str | None = None,
    ) -> _RemoteSessionRun:
        self._gc_session_runs()
        session = _RemoteSessionRun(
            session_run_id=str(uuid.uuid4()),
            peer_id=peer_id,
            session_hint=session_hint,
            mode=mode,
            workflow_mode=workflow_mode,
            taskflow_id=taskflow_id,
            agent_id=agent_id,
            provider_id=provider_id,
            model_id=model_id,
            client_request_id=client_request_id,
            model_parameters=dict(model_parameters or {}),
            runtime_state=dict(runtime_state or {}),
            locale=locale,
            mentions=[dict(item) for item in (mentions or []) if isinstance(item, dict)],
            initial_prompt=initial_prompt,
            artifact_root=self._session_run_artifact_root,
            max_events=self._session_run_max_events,
            max_payload_bytes=self._session_run_max_payload_bytes,
            max_total_bytes=self._session_run_max_total_bytes,
            trace_event_sink=getattr(self, "session_trace_event_sink", None),
        )
        with self._session_runs_lock:
            self._session_runs[session.session_run_id] = session
        return session

    def _get_session_run_by_request(
        self,
        peer_id: str,
        session_hint: str | None,
        client_request_id: str | None,
    ) -> _RemoteSessionRun | None:
        request_id = str(client_request_id or "").strip()
        if not request_id:
            return None
        self._gc_session_runs()
        with self._session_runs_lock:
            for session in self._session_runs.values():
                if session.peer_id != peer_id:
                    continue
                if session.client_request_id != request_id:
                    continue
                if (session.session_hint or "") != (session_hint or ""):
                    continue
                return session
        return None

    def set_session_trace_event_sink(
        self, sink: SessionTraceEventSink | None
    ) -> None:
        self.session_trace_event_sink = sink
        with self._session_runs_lock:
            sessions = list(self._session_runs.values())
        for session in sessions:
            session.set_trace_event_sink(sink)

    def _gc_session_runs(self) -> None:
        now = time.time()
        with self._session_runs_lock:
            stale_ids = [
                session_run_id
                for session_run_id, session in self._session_runs.items()
                if session.is_stale(
                    now,
                    closed_ttl_sec=self._session_run_closed_ttl_sec,
                    idle_ttl_sec=self._session_run_idle_ttl_sec,
                )
            ]
            for session_run_id in stale_ids:
                session = self._session_runs.pop(session_run_id, None)
                if session is not None:
                    session.cleanup_artifacts()

    def _get_session_run(self, session_run_id: str) -> _RemoteSessionRun | None:
        self._gc_session_runs()
        with self._session_runs_lock:
            return self._session_runs.get(session_run_id)

    def _get_peer_chat_lock(self, peer_id: str) -> threading.Lock:
        with self._peer_chat_locks_lock:
            return self._peer_chat_locks.setdefault(peer_id, threading.Lock())

    def _abort_peer_session_runs(self, peer_id: str, reason: str) -> None:
        with self._session_runs_lock:
            peer_sessions = [
                session
                for session in self._session_runs.values()
                if session.peer_id == peer_id and not session.done
            ]
        for session in peer_sessions:
            runtime_state = session.runtime_state if isinstance(session.runtime_state, dict) else {}
            if (
                session.mode == "capability_package"
                or session.workflow_mode == "capability_package_ingest"
                or runtime_state.get("workflow_mode") == "capability_package_ingest"
            ):
                session.append_event(
                    "workflow_step",
                    {
                        "lane": "process",
                        "workflow": "capability_package_ingest",
                        "stage": "prepare",
                        "status": "warning",
                        "title": "前端连接已断开，能力包任务继续在服务端运行",
                        "message": "前端连接已断开，能力包任务继续在服务端运行",
                        "summary": "peer_disconnected",
                        "details": {"phase": "peer_disconnected", "reason": reason},
                        "reason": reason,
                    },
                )
                continue
            resolved_approvals = session.cancel_pending_approvals(reason)
            resolved_user_inputs = session.cancel_pending_user_inputs(reason)
            session.append_event("error", {"message": reason})
            for event_payload in resolved_approvals:
                session.append_event("approval_resolved", event_payload)
            for event_payload in resolved_user_inputs:
                session.append_event("user_input_resolved", event_payload)
            session.mark_done(reason)

    def _enqueue_outbound(self, peer_id: str, envelope: RelayEnvelope) -> None:
        with self._queues_lock:
            peer_queue = self._queues.setdefault(peer_id, queue.Queue())
        peer_queue.put(envelope)

    def _next_envelope(self, peer_id: str) -> RelayEnvelope | None:
        with self._queues_lock:
            peer_queue = self._queues.setdefault(peer_id, queue.Queue())
        try:
            return peer_queue.get_nowait()
        except queue.Empty:
            return None

    def _build_handler(self):
        service = self

        class Handler(
            RemoteAuthRoutes,
            RemoteAdminRoutes,
            RemoteSessionRoutes,
            RemoteChatRoutes,
            RemoteAgentRunRoutes,
            RemoteGitHubRoutes,
            RemoteCollaborationRoutes,
            RemoteCapabilityPackageRoutes,
            RemoteTaskflowRoutes,
            RemoteManifestRoutes,
            RemoteArtifactRoutes,
            RemotePeerRoutes,
            RemoteRelayBaseHandler,
            BaseHTTPRequestHandler,
        ):
            def do_GET(self) -> None:  # noqa: N802
                self._dispatch_remote(self._do_GET)

            def _do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path.startswith("/remote/auth/"):
                    if self._handle_auth_get(parsed.path):
                        return
                if parsed.path == "/remote/features":
                    self._handle_features()
                    return
                if parsed.path.startswith("/remote/taskflow/"):
                    self._handle_taskflow_get(parsed)
                    return
                if parsed.path.startswith("/remote/issues/"):
                    self._handle_issue_assignment_get(parsed)
                    return
                if parsed.path.startswith("/remote/assignments/"):
                    self._handle_issue_assignment_get(parsed)
                    return
                if parsed.path.startswith("/remote/mentions/"):
                    self._handle_issue_assignment_get(parsed)
                    return
                if parsed.path.startswith("/remote/agent-runs/"):
                    self._handle_agent_run_events_get(parsed)
                    return
                if parsed.path.startswith("/remote/artifacts/"):
                    self._handle_artifact(parsed.path)
                    return
                if parsed.path == "/remote/admin/github/status":
                    self._handle_admin(parsed.path)
                    return
                if parsed.path.startswith("/remote/mcp/artifacts/"):
                    self._handle_mcp_artifact(parsed.path)
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "not_found")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch_remote(self._do_POST)

            def _do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path.startswith("/remote/auth/"):
                    if self._handle_auth_post(parsed.path):
                        return
                if parsed.path == "/remote/register":
                    self._handle_register()
                    return
                if parsed.path == "/remote/heartbeat":
                    self._handle_heartbeat()
                    return
                if parsed.path == "/remote/poll":
                    self._handle_poll()
                    return
                if parsed.path == "/remote/result":
                    self._handle_result()
                    return
                if parsed.path == "/remote/github/webhook":
                    self._handle_github_webhook()
                    return
                if parsed.path == "/remote/mcp/manifest":
                    self._handle_mcp_manifest()
                    return
                if parsed.path == "/remote/mcp/tools":
                    self._handle_mcp_tools()
                    return
                if parsed.path == "/remote/environment/manifest":
                    self._handle_environment_manifest()
                    return
                if parsed.path == "/remote/capability-packages/install/plan":
                    self._handle_capability_package_install_plan()
                    return
                if parsed.path == "/remote/capability-packages/install/result":
                    self._handle_capability_package_install_result()
                    return
                if parsed.path == "/remote/agent-runs/claim":
                    self._handle_agent_run_claim()
                    return
                if parsed.path == "/remote/agent-runs/event":
                    self._handle_agent_run_event()
                    return
                if parsed.path == "/remote/agent-runs/model-request":
                    self._handle_agent_run_model_request()
                    return
                if parsed.path == "/remote/agent-runs/heartbeat":
                    self._handle_agent_run_heartbeat()
                    return
                if parsed.path == "/remote/agent-runs/session":
                    self._handle_agent_run_session()
                    return
                if parsed.path == "/remote/agent-runs/complete":
                    self._handle_agent_run_complete()
                    return
                if parsed.path == "/remote/disconnect":
                    self._handle_disconnect()
                    return
                if parsed.path == "/remote/session-runs/start":
                    self._handle_session_run_start()
                    return
                if parsed.path == "/remote/chat/command":
                    self._handle_chat_command()
                    return
                if parsed.path == "/remote/session-runs/events":
                    self._handle_session_run_events()
                    return
                if parsed.path == "/remote/session-runs/status":
                    self._handle_session_run_status()
                    return
                if parsed.path == "/remote/session-runs/recover":
                    self._handle_session_run_recover()
                    return
                if parsed.path == "/remote/session-runs/cancel":
                    self._handle_session_run_cancel()
                    return
                if parsed.path == "/remote/session-runs/follow-up":
                    self._handle_session_run_follow_up()
                    return
                if parsed.path == "/remote/session-runs/follow-up/cancel":
                    self._handle_session_run_follow_up_cancel()
                    return
                if parsed.path == "/remote/session-runs/user-input/reply":
                    self._handle_session_run_user_input_reply()
                    return
                if parsed.path == "/remote/approval/reply":
                    self._handle_approval_reply()
                    return
                if parsed.path.startswith("/remote/sessions/"):
                    self._handle_sessions(parsed.path)
                    return
                if parsed.path.startswith("/remote/taskflow/"):
                    self._handle_taskflow_post(parsed.path)
                    return
                if (
                    parsed.path == "/remote/issues"
                    or parsed.path.startswith("/remote/issues/")
                    or parsed.path.startswith("/remote/assignments/")
                    or parsed.path == "/remote/mentions/parse"
                    or parsed.path == "/remote/mentions"
                ):
                    self._handle_issue_assignment_post(parsed.path)
                    return
                if parsed.path.startswith("/remote/admin/"):
                    self._handle_admin(parsed.path)
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "not_found")

            def _dispatch_remote(self, handler: Callable[[], None]) -> None:
                try:
                    handler()
                except RemoteRouteError as exc:
                    self._send_error(
                        exc.status,
                        exc.code,
                        exc.message,
                        exc.details,
                    )
                except Exception:
                    self._send_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "internal_server_error",
                    )

        setattr(Handler, "service", service)
        return Handler

    def _mcp_artifact_root_abs(self) -> Path:
        root = self.mcp_artifact_root.expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve()

    def _resolve_mcp_artifact_path(self, artifact_path: str) -> Path | None:
        if not artifact_path or artifact_path.startswith(("/", "\\")):
            return None
        root = self._mcp_artifact_root_abs()
        resolved = (root / artifact_path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        if not resolved.exists() or not resolved.is_file():
            return None
        return resolved

    def _build_mcp_manifest(self, os_name: str, arch: str) -> MCPManifestResponse:
        platform = f"{os_name}-{arch}"
        servers: list[MCPServerManifest] = []
        diagnostics: list[dict[str, Any]] = []
        for server in self.mcp_servers:
            if not getattr(server, "enabled", True):
                continue
            if getattr(server, "placement", "server") not in {"peer", "both"}:
                continue
            server_name = getattr(server, "name", "")
            distribution = str(getattr(server, "distribution", "") or "").lower()
            if distribution not in {"command", "artifact"}:
                distribution = "artifact" if getattr(server, "artifacts", {}) else "command"
            version = getattr(server, "version", None)
            artifact_manifest: MCPArtifactManifest | None = None
            if distribution == "artifact":
                if not version:
                    diagnostics.append(
                        {
                            "server": server_name,
                            "level": "error",
                            "message": "peer MCP server is missing version",
                        }
                    )
                    continue
                artifacts = getattr(server, "artifacts", {}) or {}
                artifact = artifacts.get(platform)
                if artifact is None:
                    diagnostics.append(
                        {
                            "server": server_name,
                            "level": "error",
                            "message": f"peer MCP server has no artifact for {platform}",
                        }
                    )
                    continue
                artifact_path = getattr(artifact, "path", "")
                sha256 = getattr(artifact, "sha256", "")
                if not artifact_path or not sha256:
                    diagnostics.append(
                        {
                            "server": server_name,
                            "level": "error",
                            "message": f"peer MCP server artifact for {platform} is incomplete",
                        }
                    )
                    continue
                artifact_manifest = MCPArtifactManifest(
                    platform=platform,
                    path=artifact_path,
                    sha256=sha256,
                    url="/remote/mcp/artifacts/" + quote(artifact_path, safe="/"),
                )
                launch = getattr(artifact, "launch", None) or getattr(server, "launch", None)
            else:
                launch = getattr(server, "launch", None)
            if launch is None:
                command = getattr(server, "command", "")
                launch_args = list(getattr(server, "args", []) or [])
                launch_env = dict(getattr(server, "env", {}) or {})
                launch_cwd = getattr(server, "cwd", None)
            else:
                command = getattr(launch, "command", "")
                launch_args = list(getattr(launch, "args", []) or [])
                launch_env = dict(getattr(launch, "env", {}) or {})
                launch_cwd = getattr(launch, "cwd", None)
            if not command:
                diagnostics.append(
                    {
                        "server": server_name,
                        "level": "error",
                        "message": "peer MCP server launch command is empty",
                    }
                )
                continue
            servers.append(
                MCPServerManifest(
                    name=server_name,
                    version=str(version) if version is not None else "",
                    distribution=distribution,
                    artifact=artifact_manifest,
                    launch=MCPLaunchManifest(
                        command=command,
                        args=launch_args,
                        env=launch_env,
                        cwd=launch_cwd,
                    ),
                    permissions=dict(getattr(server, "permissions", {}) or {}),
                    environment_requirement_refs=[
                        str(ref)
                        for ref in getattr(server, "environment_requirement_refs", []) or []
                    ],
                )
            )
        return MCPManifestResponse(servers=servers, diagnostics=diagnostics)

    def _build_environment_manifest(
        self,
        os_name: str,
        arch: str,
        workspace: str,
        *,
        agent_id: str = "",
    ) -> EnvironmentManifestResponse:
        del os_name, arch, workspace
        scope_ids = self._environment_requirement_scope_for_agent(agent_id)
        environment_requirements: list[EnvironmentRequirementManifest] = []
        for requirement_id, requirement in sorted(self.environment_requirements.items()):
            raw_requirement_id = str(
                _env_tool_value(requirement, "id", requirement_id) or requirement_id
            )
            if scope_ids is not None and (
                requirement_id not in scope_ids and raw_requirement_id not in scope_ids
            ):
                continue
            if not _env_bool_value(_env_tool_value(requirement, "enabled", True)):
                continue
            if not self._package_managed_requirement_available(requirement):
                continue
            placement = normalize_environment_placement(
                _env_tool_value(requirement, "placement", "peer")
            )
            if placement == "server":
                continue
            name = str(
                _env_tool_value(
                    requirement,
                    "name",
                    environment_requirement_name_from_id(raw_requirement_id),
                )
                or environment_requirement_name_from_id(raw_requirement_id)
            ).strip()
            kind = normalize_environment_requirement_kind(
                _env_tool_value(
                    requirement,
                    "kind",
                    environment_requirement_kind_from_id(raw_requirement_id),
                )
            )
            check = str(_env_tool_value(requirement, "check", "") or "")
            configure = str(_env_tool_value(requirement, "configure", "") or "")
            install = str(_env_tool_value(requirement, "install", "") or "")
            if not name or not kind:
                continue
            manifest_id = normalize_environment_requirement_id(
                raw_requirement_id,
                kind=kind,
                name=name,
            )
            tags = _env_tool_value(requirement, "tags", [])
            if not isinstance(tags, list):
                tags = []
            requirements = _env_tool_value(requirement, "requirements", {})
            if not isinstance(requirements, dict):
                requirements = {}
            args = _env_tool_value(requirement, "args", [])
            if not isinstance(args, list):
                args = []
            env = _env_tool_value(requirement, "env", {})
            if not isinstance(env, dict):
                env = {}
            version = _env_tool_value(requirement, "version", None)
            environment_requirements.append(
                EnvironmentRequirementManifest(
                    id=manifest_id,
                    kind=kind,
                    name=name,
                    command=str(_env_tool_value(requirement, "command", "") or ""),
                    placement=placement,
                    tags=[str(item) for item in tags],
                    requirements={str(k): str(v) for k, v in requirements.items()},
                    args=[str(item) for item in args],
                    env={str(k): str(v) for k, v in env.items()},
                    cwd=(
                        str(_env_tool_value(requirement, "cwd"))
                        if _env_tool_value(requirement, "cwd", None) is not None
                        else None
                    ),
                    check=check,
                    install=install,
                    configure=configure,
                    version=str(version) if version is not None else None,
                    runtime=str(_env_tool_value(requirement, "runtime", "") or ""),
                    language=str(_env_tool_value(requirement, "language", "") or ""),
                    scope=str(_env_tool_value(requirement, "scope", "") or ""),
                    path=str(_env_tool_value(requirement, "path", "") or ""),
                    source=str(_env_tool_value(requirement, "source", "") or ""),
                    description=str(_env_tool_value(requirement, "description", "") or ""),
                    repo_url=str(_env_tool_value(requirement, "repo_url", "") or ""),
                    docs=_env_docs_value(_env_tool_value(requirement, "docs", [])),
                    evidence=_env_string_dict_list_value(
                        _env_tool_value(requirement, "evidence", [])
                    ),
                    install_prompt=str(
                        _env_tool_value(requirement, "install_prompt", "") or ""
                    ),
                    verify_prompt=str(_env_tool_value(requirement, "verify_prompt", "") or ""),
                    notes=_env_string_list_value(_env_tool_value(requirement, "notes", [])),
                    credentials=_env_string_list_value(
                        _env_tool_value(requirement, "credentials", [])
                    ),
                    risk_level=str(_env_tool_value(requirement, "risk_level", "") or ""),
                    last_action=str(_env_tool_value(requirement, "last_action", "") or ""),
                    last_updated=str(_env_tool_value(requirement, "last_updated", "") or ""),
                )
            )
        return EnvironmentManifestResponse(
            environment_requirements=environment_requirements,
        )

    def _environment_requirement_scope_for_agent(
        self, agent_id: str
    ) -> set[str] | None:
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            return None
        return set(self.environment_requirement_scope_ids.get(normalized_agent_id, set()))

    def _package_managed_requirement_available(self, requirement: Any) -> bool:
        if str(_env_tool_value(requirement, "managed_by", "") or "") != "capability_package":
            return True
        package_ids = _env_string_list_value(_env_tool_value(requirement, "package_ids", []))
        if not package_ids:
            return False
        if not self.capability_packages:
            return True
        for package_id in package_ids:
            package = self.capability_packages.get(package_id)
            if package is None:
                continue
            if _environment_package_is_active(package_id, package):
                return True
        return False


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not host:
        raise ValueError(f"Invalid relay bind address: {bind!r}")
    return host, int(port)


def _normalize_environment_requirement_scope_ids(
    value: dict[str, set[str]] | None,
) -> dict[str, set[str]]:
    if not isinstance(value, dict):
        return {}
    scopes: dict[str, set[str]] = {}
    for agent_id, requirement_ids in value.items():
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            continue
        if not isinstance(requirement_ids, (list, tuple, set, frozenset)):
            scopes[normalized_agent_id] = set()
            continue
        scopes[normalized_agent_id] = {
            str(requirement_id).strip()
            for requirement_id in requirement_ids
            if str(requirement_id).strip()
        }
    return scopes


def _environment_package_is_active(package_id: str, package: Any) -> bool:
    if isinstance(package, dict):
        package_data = dict(package)
    else:
        to_dict = getattr(package, "to_dict", None)
        package_data = to_dict() if callable(to_dict) else {}
    if not isinstance(package_data, dict):
        return False
    package_data.setdefault("id", str(package_id or ""))
    package_data.setdefault("status", "installed")
    return capability_package_is_active(package_data)


def _env_tool_value(tool: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        return tool.get(field_name, default)
    return getattr(tool, field_name, default)


def _env_bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _env_string_list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _env_docs_value(value: Any) -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []
    if not isinstance(value, list):
        return docs
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if not title and not url:
            continue
        docs.append({"title": title, "url": url})
    return docs


def _env_string_dict_list_value(value: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not isinstance(value, list):
        return items
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized = {
            str(key): str(val).strip()
            for key, val in item.items()
            if val is not None and str(val).strip()
        }
        if normalized:
            items.append(normalized)
    return items
