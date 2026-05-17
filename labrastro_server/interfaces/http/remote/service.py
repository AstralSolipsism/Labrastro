"""HTTP transport adapter for the remote relay host."""

from __future__ import annotations

import gzip
import json
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
)
from labrastro_server.services.auth.service import AuthService
from labrastro_server.interfaces.http.remote.protocol import (
    ApprovalReplyRequest,
    ApprovalReplyResponse,
    ChatCancelRequest,
    ChatCancelResponse,
    ChatFollowUpCancelRequest,
    ChatFollowUpRequest,
    ChatFollowUpResponse,
    ChatRequest,
    ChatResponse,
    ChatStartRequest,
    ChatStartResponse,
    ChatStreamRequest,
    ChatStreamResponse,
    CleanupResult,
    DisconnectNotice,
    EnvironmentCLIToolManifest,
    EnvironmentMCPServerManifest,
    EnvironmentManifestRequest,
    EnvironmentManifestResponse,
    EnvironmentSkillManifest,
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


@dataclass
class _BufferedChatEvent:
    event: dict[str, Any]
    size_bytes: int


SessionTraceEventSink = Callable[
    [str, str, dict[str, Any], str | None, int | None, str, bool], int | None
]


def _is_replayable_chat_event(event_type: str) -> bool:
    return bool(event_type)


class _ChatEventBuffer:
    def __init__(
        self,
        *,
        chat_id: str,
        artifact_root: Path,
        max_events: int,
        max_payload_bytes: int,
        max_total_bytes: int,
    ) -> None:
        self.chat_id = chat_id
        self.artifact_dir = artifact_root / chat_id
        self.max_events = max(1, int(max_events or 1))
        self.max_payload_bytes = max(1, int(max_payload_bytes or 1))
        self.max_total_bytes = max(self.max_payload_bytes, int(max_total_bytes or self.max_payload_bytes))
        self._items: list[_BufferedChatEvent] = []
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

    def append(self, event: dict[str, Any]) -> None:
        normalized = self._normalize_event(event)
        size_bytes = self._event_size(normalized)
        self._items.append(_BufferedChatEvent(normalized, size_bytes))
        self._total_bytes += size_bytes
        self._prune()

    def events_after(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        cursor = max(0, int(cursor or 0))
        events = [
            dict(item.event)
            for item in self._items
            if int(item.event.get("seq", 0) or 0) > cursor
        ]
        if self._items and cursor < self.first_available_seq - 1:
            lost_seq = self.first_available_seq - 1
            events.insert(
                0,
                {
                    "chat_id": self.chat_id,
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
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload_bytes) <= self.max_payload_bytes:
            return {**event, "payload": payload}

        seq = int(event.get("seq", 0) or 0)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = self.artifact_dir / f"{seq}.json.gz"
        artifact_path.write_bytes(gzip.compress(payload_bytes))
        preview = payload_bytes[:4096].decode("utf-8", errors="replace")
        return {
            **event,
            "payload": {
                "artifact_ref": {
                    "type": "chat_event_payload",
                    "path": str(artifact_path),
                    "encoding": "json+gzip",
                    "bytes": len(payload_bytes),
                    "preview": preview,
                }
            },
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


@dataclass
class _RemoteChatSession:
    chat_id: str
    peer_id: str
    session_hint: str | None = None
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    client_request_id: str | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    status: str = "created"
    last_error: str | None = None
    done: bool = False
    running: bool = False
    seq_next: int = 1
    approval_waiters: dict[str, dict[str, Any]] = field(default_factory=dict)
    follow_up_tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    cancel_callback: Callable[[str], None] | None = None
    follow_up_callback: Callable[[dict[str, Any]], None] | None = None
    follow_up_cancel_callback: Callable[[str], None] | None = None
    artifact_root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "labrastro-chat-events"
    )
    max_events: int = 1000
    max_payload_bytes: int = 256 * 1024
    max_total_bytes: int = 4 * 1024 * 1024
    trace_event_sink: SessionTraceEventSink | None = None
    last_activity_at: float = field(default_factory=time.time)
    _event_buffer: _ChatEventBuffer = field(init=False, repr=False)
    _pending_trace_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.session_id is None:
            self.session_id = self.session_hint
        self._event_buffer = _ChatEventBuffer(
            chat_id=self.chat_id,
            artifact_root=self.artifact_root,
            max_events=self.max_events,
            max_payload_bytes=self.max_payload_bytes,
            max_total_bytes=self.max_total_bytes,
        )

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._event_buffer.snapshot()

    def append_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> int:
        with self.cond:
            seq = self.seq_next
            self.seq_next += 1
            event = {
                "chat_id": self.chat_id,
                "seq": seq,
                "type": event_type,
                "payload": payload or {},
            }
            if isinstance(payload, dict):
                session_id = payload.get("session_id")
                if isinstance(session_id, str) and session_id:
                    self.session_id = session_id
            self._persist_or_queue_trace_event(event)
            self._event_buffer.append(event)
            if event_type == "error":
                self.status = "error"
                message = (payload or {}).get("message") if isinstance(payload, dict) else None
                self.last_error = str(message) if message is not None else "error"
            elif event_type == "chat_cancelled":
                self.status = "cancelled"
                reason = (payload or {}).get("reason") if isinstance(payload, dict) else None
                self.last_error = str(reason) if reason is not None else "chat_cancelled"
            self.last_activity_at = time.time()
            self.cond.notify_all()
            return seq

    def _persist_or_queue_trace_event(self, event: dict[str, Any]) -> None:
        if not _is_replayable_chat_event(str(event.get("type") or "")):
            return
        if self.session_id:
            self._flush_pending_trace_events()
            self._persist_trace_event(event)
            return
        self._pending_trace_events.append(dict(event))

    def _flush_pending_trace_events(self) -> None:
        if not self.session_id or not self._pending_trace_events:
            return
        pending = self._pending_trace_events
        self._pending_trace_events = []
        for event in pending:
            self._persist_trace_event(event)

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
                self.chat_id,
                int(event.get("seq") or 0),
                "remote_chat",
                True,
            )
        except Exception:
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
            while cursor >= self._event_buffer.latest_seq and not self.done:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            out, next_cursor = self._event_buffer.events_after(cursor)
            self.last_activity_at = time.time()
            return out, self.done, next_cursor

    def mark_running(self) -> None:
        with self.cond:
            self.running = True
            self.status = "running"
            self.last_activity_at = time.time()

    def mark_done(self) -> None:
        unconsumed: list[dict[str, Any]] = []
        with self.cond:
            self.running = False
            self.done = True
            if self.status not in {"error", "cancelled"}:
                self.status = "done"
            self.finished_at = time.time()
            self.last_activity_at = self.finished_at
            for waiter in self.approval_waiters.values():
                if waiter.get("done"):
                    continue
                waiter["done"] = True
                waiter["decision"] = "deny_once"
                waiter["reason"] = "chat_closed"
            for ticket in self.follow_up_tickets.values():
                if ticket.get("state") in {"consumed", "cancelled", "unconsumed"}:
                    continue
                ticket["state"] = "unconsumed"
                unconsumed.append(dict(ticket))
            self.cond.notify_all()
        for ticket in unconsumed:
            self.append_event(
                "chat_follow_up_unconsumed",
                self._follow_up_event_payload(ticket),
            )

    def is_stale(self, now: float, *, closed_ttl_sec: float, idle_ttl_sec: float) -> bool:
        if self.done and self.finished_at is not None:
            return now - self.finished_at > closed_ttl_sec
        return now - self.last_activity_at > idle_ttl_sec

    def cleanup_artifacts(self) -> None:
        self._event_buffer.cleanup_artifacts()

    def status_payload(self, cursor: int = 0) -> dict[str, Any]:
        cursor = max(0, int(cursor or 0))
        with self.cond:
            _events, next_cursor = self._event_buffer.events_after(cursor)
            return {
                "ok": True,
                "chat_id": self.chat_id,
                "peer_id": self.peer_id,
                "status": self.status,
                "running": self.running,
                "done": self.done,
                "reconnectable": not self.done,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "first_available_seq": self._event_buffer.first_available_seq,
                "latest_seq": self._event_buffer.latest_seq,
                "dropped_count": self._event_buffer.dropped_count,
                "session_id": self.session_id,
                "mode": self.mode,
                "workflow_mode": self.workflow_mode,
                "taskflow_id": self.taskflow_id,
                "created_at": self.created_at,
                "last_activity_at": self.last_activity_at,
                "finished_at": self.finished_at,
                "error": self.last_error,
            }

    def set_cancel_callback(self, callback: Callable[[str], None]) -> None:
        call_immediately = False
        reason = "chat_cancelled"
        with self.cond:
            self.cancel_callback = callback
            if self.cancel_requested:
                call_immediately = True
                reason = self.cancel_reason or reason
        if call_immediately:
            callback(reason)

    def request_cancel(self, reason: str = "chat_cancelled") -> bool:
        callback: Callable[[str], None] | None
        first_request = False
        with self.cond:
            if not self.cancel_requested:
                first_request = True
            self.cancel_requested = True
            self.cancel_reason = reason
            for waiter in self.approval_waiters.values():
                if waiter.get("done"):
                    continue
                waiter["done"] = True
                waiter["decision"] = "deny_once"
                waiter["reason"] = reason
            callback = self.cancel_callback
            self.cond.notify_all()
        if callback is not None:
            callback(reason)
        return first_request

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
            "chat_follow_up_accepted",
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
        self.append_event("chat_follow_up_cancelled", payload)
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
        self.append_event("chat_follow_up_consumed", payload)
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

    def register_approval(self, approval_id: str) -> None:
        with self.cond:
            self.approval_waiters[approval_id] = {}

    def resolve_approval(
        self, approval_id: str, decision: str, reason: str | None
    ) -> bool:
        with self.cond:
            waiter = self.approval_waiters.get(approval_id)
            if waiter is None:
                return False
            waiter["done"] = True
            waiter["decision"] = decision
            waiter["reason"] = reason
            self.cond.notify_all()
            return True

    def wait_approval(
        self, approval_id: str, timeout_sec: float | None = None
    ) -> tuple[str, str | None]:
        deadline = time.time() + timeout_sec if timeout_sec else None
        with self.cond:
            waiter = self.approval_waiters.setdefault(approval_id, {})
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
            self.approval_waiters.pop(approval_id, None)
            return decision, reason if isinstance(reason, str) else None

    def cancel_pending_approvals(self, reason: str) -> None:
        with self.cond:
            for waiter in self.approval_waiters.values():
                if waiter.get("done"):
                    continue
                waiter["done"] = True
                waiter["decision"] = "deny_once"
                waiter["reason"] = reason
            self.cond.notify_all()


class RemoteRelayHTTPService:
    """Expose ``RelayServer`` over a minimal HTTP API for remote peers."""

    def __init__(
        self,
        relay_server: RelayServer,
        bind: str,
        *,
        ui_bus: UIEventBus | None = None,
        artifact_provider: callable | None = None,
        chat_handler: Callable[[str, str], ChatResponse] | None = None,
        stream_chat_handler: Callable[[str, str, _RemoteChatSession], None]
        | None = None,
        session_handler: Callable[[str, str, dict[str, Any]], dict[str, Any]]
        | None = None,
        session_history_status_provider: Callable[[], dict[str, Any]] | None = None,
        auth_service: AuthService | None = None,
        bootstrap_token_ttl_sec: int = 300,
        mcp_servers: list[Any] | None = None,
        mcp_artifact_root: str | Path = ".rcoder/mcp-artifacts",
        environment_cli_tools: dict[str, Any] | None = None,
        environment_skills: dict[str, Any] | None = None,
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
        chat_max_events: int = 1000,
        chat_max_payload_bytes: int = 256 * 1024,
        chat_max_total_bytes: int = 4 * 1024 * 1024,
        chat_closed_ttl_sec: float = 300.0,
        chat_idle_ttl_sec: float = 30 * 60.0,
        chat_gc_interval_sec: float = 30.0,
        chat_artifact_root: str | Path | None = None,
        require_explicit_chat_model: bool = False,
        require_peer_runtime_context: bool = False,
    ) -> None:
        self.relay_server = relay_server
        self.bind = bind
        self.ui_bus = ui_bus
        self.artifact_provider = artifact_provider
        self.chat_handler = chat_handler
        self.stream_chat_handler = stream_chat_handler
        self.session_handler = session_handler
        self.session_trace_event_sink: SessionTraceEventSink | None = None
        self.session_history_status_provider = session_history_status_provider
        if auth_service is None:
            raise ValueError("auth_service is required")
        self.auth_service = auth_service
        self.bootstrap_token_ttl_sec = bootstrap_token_ttl_sec
        self.mcp_servers = list(mcp_servers or [])
        self.mcp_artifact_root = Path(mcp_artifact_root)
        self.environment_cli_tools = dict(environment_cli_tools or {})
        self.environment_skills = dict(environment_skills or {})
        self.admin_manager = RemoteAdminConfigManager(
            Path(admin_config_path) if admin_config_path is not None else None,
            reload_handler=admin_config_reload_handler,
            provider_test_handler=admin_provider_test_handler,
            provider_models_handler=admin_provider_models_handler,
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
        self._chat_sessions: dict[str, _RemoteChatSession] = {}
        self._chat_sessions_lock = threading.Lock()
        self._chat_max_events = max(1, int(chat_max_events or 1))
        self._chat_max_payload_bytes = max(1, int(chat_max_payload_bytes or 1))
        self._chat_max_total_bytes = max(
            self._chat_max_payload_bytes,
            int(chat_max_total_bytes or self._chat_max_payload_bytes),
        )
        self._chat_session_ttl_sec = max(0.0, float(chat_closed_ttl_sec))
        self._chat_idle_ttl_sec = max(0.0, float(chat_idle_ttl_sec))
        self._chat_gc_interval_sec = max(1.0, float(chat_gc_interval_sec))
        self._chat_artifact_root = (
            Path(chat_artifact_root)
            if chat_artifact_root is not None
            else Path(tempfile.gettempdir()) / "labrastro-chat-events"
        )
        self._chat_gc_stop = threading.Event()
        self._chat_gc_thread: threading.Thread | None = None
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
        self._start_chat_gc()
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
        self._stop_chat_gc()
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._server = None

    def _start_agent_run_recovery(self) -> None:
        if self.runtime_control_plane is None:
            return
        if self._agent_run_recovery_thread is not None:
            return
        self._agent_run_recovery_stop.clear()

        def loop() -> None:
            while not self._agent_run_recovery_stop.wait(
                self._agent_run_recovery_interval_sec
            ):
                try:
                    self.runtime_control_plane.recover_stale_agent_runs()
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

    def _start_chat_gc(self) -> None:
        if self._chat_gc_thread is not None:
            return
        self._chat_gc_stop.clear()

        def loop() -> None:
            while not self._chat_gc_stop.wait(self._chat_gc_interval_sec):
                self._gc_chat_sessions()

        self._chat_gc_thread = threading.Thread(target=loop, daemon=True)
        self._chat_gc_thread.start()

    def _stop_chat_gc(self) -> None:
        self._chat_gc_stop.set()
        if self._chat_gc_thread is not None:
            self._chat_gc_thread.join(timeout=3)
            self._chat_gc_thread = None

    def _start_github_reconcile(self) -> None:
        if self.github_pr_service is None or self.github_reconcile_service is None:
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
                    self.github_reconcile_service.reconcile()
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

    def set_chat_handler(
        self, handler: Callable[[str, str], ChatResponse] | None
    ) -> None:
        self.chat_handler = handler

    def set_stream_chat_handler(
        self,
        handler: Callable[[str, str, _RemoteChatSession], None] | None,
    ) -> None:
        self.stream_chat_handler = handler

    def set_session_handler(
        self,
        handler: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None,
    ) -> None:
        self.session_handler = handler

    def _create_chat_session(
        self,
        peer_id: str,
        session_hint: str | None = None,
        *,
        mode: str | None = None,
        workflow_mode: str | None = None,
        taskflow_id: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        client_request_id: str | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> _RemoteChatSession:
        self._gc_chat_sessions()
        session = _RemoteChatSession(
            chat_id=str(uuid.uuid4()),
            peer_id=peer_id,
            session_hint=session_hint,
            mode=mode,
            workflow_mode=workflow_mode,
            taskflow_id=taskflow_id,
            provider_id=provider_id,
            model_id=model_id,
            client_request_id=client_request_id,
            model_parameters=dict(model_parameters or {}),
            artifact_root=self._chat_artifact_root,
            max_events=self._chat_max_events,
            max_payload_bytes=self._chat_max_payload_bytes,
            max_total_bytes=self._chat_max_total_bytes,
            trace_event_sink=getattr(self, "session_trace_event_sink", None),
        )
        with self._chat_sessions_lock:
            self._chat_sessions[session.chat_id] = session
        return session

    def _get_chat_session_by_request(
        self,
        peer_id: str,
        session_hint: str | None,
        client_request_id: str | None,
    ) -> _RemoteChatSession | None:
        request_id = str(client_request_id or "").strip()
        if not request_id:
            return None
        self._gc_chat_sessions()
        with self._chat_sessions_lock:
            for session in self._chat_sessions.values():
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

    def _gc_chat_sessions(self) -> None:
        now = time.time()
        with self._chat_sessions_lock:
            stale_ids = [
                chat_id
                for chat_id, session in self._chat_sessions.items()
                if session.is_stale(
                    now,
                    closed_ttl_sec=self._chat_session_ttl_sec,
                    idle_ttl_sec=self._chat_idle_ttl_sec,
                )
            ]
            for chat_id in stale_ids:
                session = self._chat_sessions.pop(chat_id, None)
                if session is not None:
                    session.cleanup_artifacts()

    def _get_chat_session(self, chat_id: str) -> _RemoteChatSession | None:
        self._gc_chat_sessions()
        with self._chat_sessions_lock:
            return self._chat_sessions.get(chat_id)

    def _get_peer_chat_lock(self, peer_id: str) -> threading.Lock:
        with self._peer_chat_locks_lock:
            return self._peer_chat_locks.setdefault(peer_id, threading.Lock())

    def _abort_peer_chat_sessions(self, peer_id: str, reason: str) -> None:
        with self._chat_sessions_lock:
            peer_sessions = [
                session
                for session in self._chat_sessions.values()
                if session.peer_id == peer_id and not session.done
            ]
        for session in peer_sessions:
            session.cancel_pending_approvals(reason)
            session.append_event("error", {"message": reason})
            session.mark_done()

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
                if parsed.path == "/remote/agent-runs/claim":
                    self._handle_agent_run_claim()
                    return
                if parsed.path == "/remote/agent-runs/event":
                    self._handle_agent_run_event()
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
                if parsed.path == "/remote/chat":
                    self._handle_chat()
                    return
                if parsed.path == "/remote/chat/start":
                    self._handle_chat_start()
                    return
                if parsed.path == "/remote/chat/stream":
                    self._handle_chat_stream()
                    return
                if parsed.path == "/remote/chat/status":
                    self._handle_chat_status()
                    return
                if parsed.path == "/remote/chat/cancel":
                    self._handle_chat_cancel()
                    return
                if parsed.path == "/remote/chat/follow-up":
                    self._handle_chat_follow_up()
                    return
                if parsed.path == "/remote/chat/follow-up/cancel":
                    self._handle_chat_follow_up_cancel()
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

        Handler.service = service
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
                    requirements=dict(getattr(server, "requirements", {}) or {}),
                )
            )
        return MCPManifestResponse(servers=servers, diagnostics=diagnostics)

    def _build_environment_manifest(
        self, os_name: str, arch: str, workspace: str
    ) -> EnvironmentManifestResponse:
        del os_name, arch, workspace
        tools: list[EnvironmentCLIToolManifest] = []
        for name, tool in sorted(self.environment_cli_tools.items()):
            if not _env_bool_value(_env_tool_value(tool, "enabled", True)):
                continue
            placement = str(_env_tool_value(tool, "placement", "local") or "local")
            if placement == "server":
                continue
            tool_name = str(_env_tool_value(tool, "name", name) or name)
            command = str(_env_tool_value(tool, "command", "") or "")
            check = str(_env_tool_value(tool, "check", "") or "")
            if not tool_name or not command or not check:
                continue
            tags = _env_tool_value(tool, "tags", [])
            if not isinstance(tags, list):
                tags = []
            requirements = _env_tool_value(tool, "requirements", {})
            if not isinstance(requirements, dict):
                requirements = {}
            version = _env_tool_value(tool, "version", None)
            tools.append(
                EnvironmentCLIToolManifest(
                    name=tool_name,
                    command=command,
                    placement=placement,
                    tags=[str(item) for item in tags],
                    requirements={str(k): str(v) for k, v in requirements.items()},
                    check=check,
                    install=str(_env_tool_value(tool, "install", "") or ""),
                    version=str(version) if version is not None else None,
                    source=str(_env_tool_value(tool, "source", "") or ""),
                    description=str(_env_tool_value(tool, "description", "") or ""),
                    repo_url=str(_env_tool_value(tool, "repo_url", "") or ""),
                    docs=_env_docs_value(_env_tool_value(tool, "docs", [])),
                    evidence=_env_string_dict_list_value(
                        _env_tool_value(tool, "evidence", [])
                    ),
                    install_prompt=str(
                        _env_tool_value(tool, "install_prompt", "") or ""
                    ),
                    verify_prompt=str(_env_tool_value(tool, "verify_prompt", "") or ""),
                    notes=_env_string_list_value(_env_tool_value(tool, "notes", [])),
                    credentials=_env_string_list_value(
                        _env_tool_value(tool, "credentials", [])
                    ),
                    risk_level=str(_env_tool_value(tool, "risk_level", "") or ""),
                    last_action=str(_env_tool_value(tool, "last_action", "") or ""),
                    last_updated=str(_env_tool_value(tool, "last_updated", "") or ""),
                )
            )
        mcp_servers: list[EnvironmentMCPServerManifest] = []
        for server in self.mcp_servers:
            if not getattr(server, "enabled", True):
                continue
            if getattr(server, "placement", "server") not in {"peer", "both"}:
                continue
            launch = getattr(server, "launch", None)
            if launch is None:
                command = str(getattr(server, "command", "") or "")
                launch_args = list(getattr(server, "args", []) or [])
                launch_env = dict(getattr(server, "env", {}) or {})
                launch_cwd = getattr(server, "cwd", None)
            else:
                command = str(getattr(launch, "command", "") or "")
                launch_args = list(getattr(launch, "args", []) or [])
                launch_env = dict(getattr(launch, "env", {}) or {})
                launch_cwd = getattr(launch, "cwd", None)
            if not command:
                continue
            mcp_servers.append(
                EnvironmentMCPServerManifest(
                    name=str(getattr(server, "name", "") or ""),
                    command=command,
                    args=[str(arg) for arg in launch_args],
                    env={str(k): str(v) for k, v in launch_env.items()},
                    cwd=str(launch_cwd) if launch_cwd is not None else None,
                    placement=str(getattr(server, "placement", "peer") or "peer"),
                    distribution=str(
                        getattr(server, "distribution", "command") or "command"
                    ),
                    requirements=dict(getattr(server, "requirements", {}) or {}),
                    check=str(getattr(server, "check", "") or ""),
                    install=str(getattr(server, "install", "") or ""),
                    version=(
                        str(getattr(server, "version"))
                        if getattr(server, "version", None) is not None
                        else None
                    ),
                    source=str(getattr(server, "source", "") or ""),
                    description=str(getattr(server, "description", "") or ""),
                    repo_url=str(getattr(server, "repo_url", "") or ""),
                    docs=_env_docs_value(getattr(server, "docs", [])),
                    evidence=_env_string_dict_list_value(
                        getattr(server, "evidence", [])
                    ),
                    install_prompt=str(getattr(server, "install_prompt", "") or ""),
                    verify_prompt=str(getattr(server, "verify_prompt", "") or ""),
                    notes=_env_string_list_value(getattr(server, "notes", [])),
                    credentials=_env_string_list_value(
                        getattr(server, "credentials", [])
                    ),
                    risk_level=str(getattr(server, "risk_level", "") or ""),
                    last_action=str(getattr(server, "last_action", "") or ""),
                    last_updated=str(getattr(server, "last_updated", "") or ""),
                )
            )
        skills: list[EnvironmentSkillManifest] = []
        for name, skill in sorted(self.environment_skills.items()):
            if not _env_bool_value(_env_tool_value(skill, "enabled", True)):
                continue
            skill_name = str(_env_tool_value(skill, "name", name) or name)
            check = str(_env_tool_value(skill, "check", "") or "")
            if not skill_name or not check:
                continue
            version = _env_tool_value(skill, "version", None)
            requirements = _env_tool_value(skill, "requirements", {})
            if not isinstance(requirements, dict):
                requirements = {}
            skills.append(
                EnvironmentSkillManifest(
                    name=skill_name,
                    scope=str(_env_tool_value(skill, "scope", "project") or "project"),
                    check=check,
                    install=str(_env_tool_value(skill, "install", "") or ""),
                    version=str(version) if version is not None else None,
                    source=str(_env_tool_value(skill, "source", "") or ""),
                    description=str(_env_tool_value(skill, "description", "") or ""),
                    path_hint=(
                        str(_env_tool_value(skill, "path_hint"))
                        if _env_tool_value(skill, "path_hint", None) is not None
                        else None
                    ),
                    requirements={str(k): str(v) for k, v in requirements.items()},
                    repo_url=str(_env_tool_value(skill, "repo_url", "") or ""),
                    docs=_env_docs_value(_env_tool_value(skill, "docs", [])),
                    evidence=_env_string_dict_list_value(
                        _env_tool_value(skill, "evidence", [])
                    ),
                    install_prompt=str(
                        _env_tool_value(skill, "install_prompt", "") or ""
                    ),
                    verify_prompt=str(_env_tool_value(skill, "verify_prompt", "") or ""),
                    notes=_env_string_list_value(_env_tool_value(skill, "notes", [])),
                    credentials=_env_string_list_value(
                        _env_tool_value(skill, "credentials", [])
                    ),
                    risk_level=str(_env_tool_value(skill, "risk_level", "") or ""),
                    last_action=str(_env_tool_value(skill, "last_action", "") or ""),
                    last_updated=str(_env_tool_value(skill, "last_updated", "") or ""),
                )
            )
        return EnvironmentManifestResponse(
            cli_tools=tools,
            mcp_servers=mcp_servers,
            skills=skills,
        )


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not host:
        raise ValueError(f"Invalid relay bind address: {bind!r}")
    return host, int(port)


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
