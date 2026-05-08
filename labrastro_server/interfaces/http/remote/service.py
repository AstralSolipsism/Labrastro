"""HTTP transport adapter for the remote relay host."""

from __future__ import annotations

import json
import queue
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
    SessionSnapshotRequest,
    ToolPreviewResult,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from labrastro_server.interfaces.http.remote.routes.admin import RemoteAdminRoutes
from labrastro_server.interfaces.http.remote.routes.artifacts import RemoteArtifactRoutes
from labrastro_server.interfaces.http.remote.routes.auth import RemoteAuthRoutes
from labrastro_server.interfaces.http.remote.routes.base import RemoteRelayBaseHandler
from labrastro_server.interfaces.http.remote.routes.chat import RemoteChatRoutes
from labrastro_server.interfaces.http.remote.routes.collaboration import RemoteCollaborationRoutes
from labrastro_server.interfaces.http.remote.routes.github import RemoteGitHubRoutes
from labrastro_server.interfaces.http.remote.routes.manifests import RemoteManifestRoutes
from labrastro_server.interfaces.http.remote.routes.peer import RemotePeerRoutes
from labrastro_server.interfaces.http.remote.routes.runtime import RemoteRuntimeRoutes
from labrastro_server.interfaces.http.remote.routes.sessions import RemoteSessionRoutes
from labrastro_server.interfaces.http.remote.routes.taskflow import RemoteTaskflowRoutes
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.github.service import (
    PullRequestService,
    ReconcileService,
    WebhookService,
)
from labrastro_server.services.taskflow.service import TaskflowService


@dataclass
class _RemoteChatSession:
    chat_id: str
    peer_id: str
    session_hint: str | None = None
    mode: str | None = None
    workflow_mode: str | None = None
    taskflow_goal_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    running: bool = False
    seq_next: int = 1
    approval_waiters: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    cancel_callback: Callable[[str], None] | None = None

    def append_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> int:
        with self.cond:
            seq = self.seq_next
            self.seq_next += 1
            self.events.append(
                {
                    "chat_id": self.chat_id,
                    "seq": seq,
                    "type": event_type,
                    "payload": payload or {},
                }
            )
            self.cond.notify_all()
            return seq

    def wait_events(
        self, cursor: int, timeout_sec: float
    ) -> tuple[list[dict[str, Any]], bool, int]:
        deadline = time.time() + max(timeout_sec, 0.0)
        with self.cond:
            while cursor >= len(self.events) and not self.done:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            out = self.events[cursor:]
            return out, self.done, len(self.events)

    def mark_running(self) -> None:
        with self.cond:
            self.running = True

    def mark_done(self) -> None:
        with self.cond:
            self.running = False
            self.done = True
            self.finished_at = time.time()
            for waiter in self.approval_waiters.values():
                if waiter.get("done"):
                    continue
                waiter["done"] = True
                waiter["decision"] = "deny_once"
                waiter["reason"] = "chat_closed"
            self.cond.notify_all()

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
    ) -> None:
        self.relay_server = relay_server
        self.bind = bind
        self.ui_bus = ui_bus
        self.artifact_provider = artifact_provider
        self.chat_handler = chat_handler
        self.stream_chat_handler = stream_chat_handler
        self.session_handler = session_handler
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
            runtime_control_plane=runtime_control_plane
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
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._queues: dict[str, queue.Queue[RelayEnvelope]] = {}
        self._queues_lock = threading.Lock()
        self._peer_chat_locks: dict[str, threading.Lock] = {}
        self._peer_chat_locks_lock = threading.Lock()
        self._chat_sessions: dict[str, _RemoteChatSession] = {}
        self._chat_sessions_lock = threading.Lock()
        self._chat_session_ttl_sec = 300.0
        self._runtime_recovery_stop = threading.Event()
        self._runtime_recovery_thread: threading.Thread | None = None
        self._runtime_recovery_interval_sec = 2.0
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
        self._start_runtime_recovery()
        self._start_github_reconcile()
        if self.ui_bus is not None:
            self.ui_bus.info(
                f"Remote relay HTTP service listening on {self.base_url}",
                kind=UIEventKind.REMOTE,
            )

    def stop(self) -> None:
        self._stop_github_reconcile()
        self._stop_runtime_recovery()
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._server = None

    def _start_runtime_recovery(self) -> None:
        if self.runtime_control_plane is None:
            return
        if self._runtime_recovery_thread is not None:
            return
        self._runtime_recovery_stop.clear()

        def loop() -> None:
            while not self._runtime_recovery_stop.wait(
                self._runtime_recovery_interval_sec
            ):
                try:
                    self.runtime_control_plane.recover_stale_tasks()
                except Exception:
                    continue

        self._runtime_recovery_thread = threading.Thread(target=loop, daemon=True)
        self._runtime_recovery_thread.start()

    def _stop_runtime_recovery(self) -> None:
        self._runtime_recovery_stop.set()
        if self._runtime_recovery_thread is not None:
            self._runtime_recovery_thread.join(timeout=3)
            self._runtime_recovery_thread = None

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

    def issue_bootstrap_token(self, ttl_sec: int = 300) -> str:
        return self.relay_server.issue_bootstrap_token(ttl_sec=ttl_sec)

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
        taskflow_goal_id: str | None = None,
    ) -> _RemoteChatSession:
        self._gc_chat_sessions()
        session = _RemoteChatSession(
            chat_id=str(uuid.uuid4()),
            peer_id=peer_id,
            session_hint=session_hint,
            mode=mode,
            workflow_mode=workflow_mode,
            taskflow_goal_id=taskflow_goal_id,
        )
        with self._chat_sessions_lock:
            self._chat_sessions[session.chat_id] = session
        return session

    def _gc_chat_sessions(self) -> None:
        now = time.time()
        with self._chat_sessions_lock:
            stale_ids = [
                chat_id
                for chat_id, session in self._chat_sessions.items()
                if session.done
                and session.finished_at is not None
                and now - session.finished_at > self._chat_session_ttl_sec
            ]
            for chat_id in stale_ids:
                self._chat_sessions.pop(chat_id, None)

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
            RemoteRuntimeRoutes,
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
                parsed = urlparse(self.path)
                if parsed.path.startswith("/remote/auth/"):
                    if self._handle_auth_get(parsed.path):
                        return
                if parsed.path == "/remote/capabilities":
                    self._handle_capabilities()
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
                if parsed.path.startswith("/remote/agent-runtime/tasks/"):
                    self._handle_runtime_events_get(parsed)
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
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
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
                if parsed.path == "/remote/runtime/claim":
                    self._handle_runtime_claim()
                    return
                if parsed.path == "/remote/runtime/event":
                    self._handle_runtime_event()
                    return
                if parsed.path == "/remote/runtime/heartbeat":
                    self._handle_runtime_heartbeat()
                    return
                if parsed.path == "/remote/runtime/session":
                    self._handle_runtime_session()
                    return
                if parsed.path == "/remote/runtime/complete":
                    self._handle_runtime_complete()
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
                if parsed.path == "/remote/chat/cancel":
                    self._handle_chat_cancel()
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
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

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
            capabilities = _env_tool_value(tool, "capabilities", [])
            if not isinstance(capabilities, list):
                capabilities = []
            requirements = _env_tool_value(tool, "requirements", {})
            if not isinstance(requirements, dict):
                requirements = {}
            version = _env_tool_value(tool, "version", None)
            tools.append(
                EnvironmentCLIToolManifest(
                    name=tool_name,
                    command=command,
                    placement=placement,
                    capabilities=[str(item) for item in capabilities],
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
