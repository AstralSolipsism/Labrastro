"""Tests for the HTTP transport adapter around the remote relay host."""

from __future__ import annotations

import gzip
import json
import hashlib
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

import pytest


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

_GO_AVAILABLE = shutil.which("go") is not None

from labrastro_server.interfaces.http.remote.service import (
    RemoteRelayHTTPService as _RemoteRelayHTTPService,
    _SessionRunEventBuffer,
    _SessionRunProjection,
)
from labrastro_server.interfaces.http.remote.routes.chat import (
    _remote_runtime_error_payload,
)
from labrastro_server.interfaces.http.remote.routes.admin import (
    _session_run_failed_http_error,
)
from labrastro_server.services.auth.models import AuthPrincipal
from labrastro_server.services.admin.service import RemoteAdminConfigManager
from reuleauxcoder.domain.config.models import (
    EnvironmentRequirementConfig,
    AgentRegistryConfig,
    RuntimeProfilesConfig,
    RunLimitsConfig,
    build_agent_run_snapshot,
    MCPArtifactConfig,
    MCPLaunchConfig,
    MCPServerConfig,
)
from reuleauxcoder.domain.providers.models import ProviderResponse
from reuleauxcoder.domain.agent_runtime.models import (
    AgentRunSource,
    ExecutionLocation,
    ModelRequestOrigin,
    WorkerKind,
)
from reuleauxcoder.services.providers.stream_supervisor import ProviderStreamInterruptedError
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.executor_backend import ExecutorEvent
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader
from labrastro_server.interfaces.http.remote.protocol import (
    ChatCommandDispatchRequest,
    ChatCommandDispatchResponse,
    LocalActionRecord,
    SessionRunStartRequest,
    SessionDeleteRequest,
    SessionListRequest,
    SessionLoadRequest,
    SessionNewRequest,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.interfaces.entrypoint.runner import (
    _default_create_remote_artifact_provider,
)
from reuleauxcoder.interfaces.events import UIEventBus


TEST_ADMIN_TOKEN = "test-admin-token"


def _current_activation_id(control: AgentRunControlPlane, task_id: str) -> str:
    return str(control.get_agent_run(task_id).current_activation_id or "")


def _with_main_branch(payload: dict | None = None) -> dict:
    out = dict(payload or {})
    out.setdefault("branch_binding_id", "main")
    return out


def _append_main_event(
    session: _SessionRunProjection,
    event_type: str,
    payload: dict | None = None,
) -> int:
    _ensure_main_branch(session)
    return session.append_event(event_type, _with_main_branch(payload))


def _append_main_live_event(
    session: _SessionRunProjection,
    event_type: str,
    payload: dict | None = None,
) -> int:
    _ensure_main_branch(session)
    return session.append_live_event(event_type, _with_main_branch(payload))


def _record_session_branch(
    session: _SessionRunProjection,
    branch_binding_id: str,
    *,
    agent_run_id: str | None = None,
    selected: bool = False,
) -> None:
    session.record_branch_binding(
        {
            "id": f"binding-{branch_binding_id}",
            "session_run_id": session.session_run_id,
            "branch_binding_id": branch_binding_id,
            "agent_run_id": agent_run_id or f"agent-{branch_binding_id}",
            "target_agent_run_id": agent_run_id or f"agent-{branch_binding_id}",
            "selected": selected,
            "status": "active",
        }
    )


def _ensure_session_branch(
    session: _SessionRunProjection,
    branch_binding_id: str,
    *,
    agent_run_id: str | None = None,
    selected: bool = False,
) -> None:
    existing = session.branch_bindings.get(branch_binding_id)
    if isinstance(existing, dict):
        if not str(existing.get("agent_run_id") or "").strip():
            session.record_branch_binding(
                {
                    **existing,
                    "agent_run_id": agent_run_id or f"agent-{branch_binding_id}",
                    "target_agent_run_id": agent_run_id or f"agent-{branch_binding_id}",
                    "selected": selected or bool(existing.get("selected")),
                }
            )
        return
    _record_session_branch(
        session,
        branch_binding_id,
        agent_run_id=agent_run_id,
        selected=selected,
    )


def _ensure_main_branch(session: _SessionRunProjection) -> None:
    _ensure_session_branch(
        session,
        "main",
        agent_run_id=str(session.agent_run_id or "agent-run-main"),
        selected=True,
    )


def test_session_run_start_failure_retry_ignores_runtime_failure_events() -> None:
    session = _SessionRunProjection(
        session_run_id="session-run-runtime-failed-not-start",
        peer_id="peer-1",
        branch_binding_id="main",
        agent_run_id="agent-main",
    )
    session.append_event(
        "session_run_failed",
        {
            "branch_binding_id": "main",
            "operation": "runtime",
            "http_status": 409,
            "code": "runtime_failed",
            "message": "runtime failed",
        },
    )

    assert _session_run_failed_http_error(session) is None


def test_remote_runtime_error_payload_preserves_remote_protocol_code() -> None:
    class RemoteProtocolLikeError(Exception):
        code = "REMOTE_PREVIEW_EMPTY"
        message = "remote peer preview did not include diff or sections"

    assert _remote_runtime_error_payload(RemoteProtocolLikeError("wrapped")) == {
        "message": "remote peer preview did not include diff or sections",
        "code": "REMOTE_PREVIEW_EMPTY",
    }


def test_remote_runtime_error_payload_preserves_provider_diagnostic() -> None:
    class RemoteChatProviderError(Exception):
        code = "REMOTE_CHAT_ERROR"
        message = "Try provider type openai_chat. Upstream error: invalid csrf token"
        provider_diagnostic = {
            "code": "REMOTE_CHAT_ERROR",
            "message": "Try provider type openai_chat. Upstream error: invalid csrf token",
            "provider_id": "Zenmux",
            "provider_type": "anthropic_messages",
            "recommended_action": "Try provider type openai_chat.",
            "upstream_message": "invalid csrf token",
        }

    payload = _remote_runtime_error_payload(RemoteChatProviderError("wrapped"))

    assert payload["message"] == "Try provider type openai_chat. Upstream error: invalid csrf token"
    assert payload["code"] == "REMOTE_CHAT_ERROR"
    assert payload["provider_diagnostic"]["provider_id"] == "Zenmux"
    assert payload["provider_diagnostic"]["recommended_action"] == "Try provider type openai_chat."


def _agent_run_settings_from_config(data: dict) -> tuple[RunLimitsConfig, dict]:
    run_limits = RunLimitsConfig.from_dict(data.get("run_limits", {}))
    runtime_profiles = RuntimeProfilesConfig.from_dict(data.get("runtime_profiles", {}))
    agent_registry = AgentRegistryConfig.from_dict(data.get("agent_registry", {}))
    return run_limits, build_agent_run_snapshot(
        agent_registry=agent_registry,
        runtime_profiles=runtime_profiles,
        run_limits=run_limits,
    )
TEST_ADMIN_HEADERS = {"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}


class _TestAuditStore:
    def __init__(self) -> None:
        self.audit_events: list[dict] = []

    def append_audit_event(self, event: dict) -> None:
        self.audit_events.append(dict(event))


class _TestAuthService:
    def __init__(self) -> None:
        self.store = _TestAuditStore()

    def state(self) -> dict:
        return {"ok": True, "auth_enabled": True, "login_required": True}

    def authenticate_access_token(self, token: str):
        if token == TEST_ADMIN_TOKEN:
            return AuthPrincipal("usr_test", "admin", "superadmin", "dev_test")
        return None

    def me(self, principal):
        return {"ok": True, "user": principal.public_user(), "device": None}

    def bootstrap_token(self, principal, ttl_sec: int):
        return {
            "ok": True,
            "bootstrap_token": self._issue_bootstrap_token(ttl_sec),
            "expires_in": ttl_sec,
        }

    def login(self, username: str, password: str, device_label: str):
        raise NotImplementedError

    def refresh(self, refresh_token: str):
        raise NotImplementedError

    def logout(self, refresh_token: str) -> None:
        return None


def RemoteRelayHTTPService(*args, **kwargs):  # noqa: N802
    service = _TestAuthService()
    auth_service = kwargs.pop("auth_service", service)
    instance = _RemoteRelayHTTPService(*args, auth_service=auth_service, **kwargs)
    service._issue_bootstrap_token = instance.issue_bootstrap_token
    return instance


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _sse_request(
    method: str,
    url: str,
    payload: dict | None = None,
) -> tuple[int, str, str, list[dict]]:
    data = None
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        content_type = resp.headers.get("Content-Type", "")
        return resp.status, content_type, body, _parse_sse_frames(body)


def _sse_first_frame_request(
    method: str,
    url: str,
    payload: dict | None = None,
) -> tuple[int, str, dict]:
    data = None
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        lines: list[str] = []
        while True:
            line = resp.readline().decode("utf-8")
            if line in {"", "\n", "\r\n"}:
                break
            lines.append(line)
        frames = _parse_sse_frames("".join(lines) + "\n")
        assert frames
        return resp.status, resp.headers.get("Content-Type", ""), frames[0]


def _session_run_events_body(
    base_url: str,
    peer_token: str,
    session_run_id: str,
    *,
    cursor: int = 0,
    timeout_sec: float = 1,
) -> dict:
    status, content_type, _raw_body, frames = _sse_request(
        "POST",
        f"{base_url}/remote/session-runs/events",
        {
            "peer_token": peer_token,
            "session_run_id": session_run_id,
            "cursor": cursor,
            "timeout_sec": timeout_sec,
        },
    )
    assert status == 200
    assert content_type.startswith("text/event-stream")
    return _merge_session_run_event_frames(frames)


def _session_run_events_first_body(
    base_url: str,
    peer_token: str,
    session_run_id: str,
    *,
    cursor: int = 0,
    timeout_sec: float = 1,
) -> dict:
    status, content_type, frame = _sse_first_frame_request(
        "POST",
        f"{base_url}/remote/session-runs/events",
        {
            "peer_token": peer_token,
            "session_run_id": session_run_id,
            "cursor": cursor,
            "timeout_sec": timeout_sec,
        },
    )
    assert status == 200
    assert content_type.startswith("text/event-stream")
    return frame["data"]


def _merge_session_run_event_frames(frames: list[dict]) -> dict:
    merged = {"events": [], "done": False, "next_cursor": 0, "error": None}
    for frame in frames:
        if frame["event"] != "session_run":
            continue
        data = frame["data"]
        merged["events"].extend(data.get("events", []))
        merged["done"] = bool(data.get("done", False))
        merged["next_cursor"] = data.get("next_cursor", merged["next_cursor"])
        merged["error"] = data.get("error")
    return merged


def _parse_sse_frames(body: str) -> list[dict]:
    frames: list[dict] = []
    for raw_frame in body.replace("\r\n", "\n").split("\n\n"):
        event_name = "message"
        data_lines: list[str] = []
        for line in raw_frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if not separator:
                continue
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            frames.append(
                {
                    "event": event_name,
                    "data": json.loads("\n".join(data_lines)),
                }
            )
    return frames


def _peer_register_payload(
    relay: RelayServer,
    *,
    cwd: str = "/tmp/peer",
    workspace_root: str | None = None,
    host_info_min: dict | None = None,
    features: list[str] | None = None,
) -> dict:
    return {
        "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
        "cwd": cwd,
        "workspace_root": workspace_root or cwd,
        "features": features or ["shell"],
        "host_info_min": host_info_min
        or {
            "os": "linux",
            "arch": "amd64",
            "shell": "bash",
            "hostname": "test-peer",
        },
    }


def test_remote_session_run_session_flushes_pending_replayable_events_when_trace_persistence_is_enabled(
    tmp_path: Path,
) -> None:
    persisted: list[dict] = []

    def sink(
        session_id: str,
        event_type: str,
        payload: dict,
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        seq = len(persisted) + 1
        persisted.append(
            {
                "session_id": session_id,
                "type": event_type,
                "payload": payload,
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return seq

    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )

    session.append_event("session_run_start", {"prompt": "hi"})
    _append_main_event(session, "assistant_delta", {"content": "live-coalesced"})
    _append_main_event(
        session,
        "document_draft_progress",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content_length": 5,
            "content_sha256": "progress-sha",
            "last_chunk_seq": 1,
        },
    )
    _append_main_event(
        session,
        "document_draft_snapshot",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content": "draft",
            "content_length": 5,
            "content_sha256": "snapshot-sha",
            "snapshot_kind": "final",
            "final": True,
            "last_chunk_seq": 1,
        },
    )
    _append_main_event(session, "tool_call_stream", {"tool_call_id": "tool-1", "content": "live"})
    assert persisted == []

    _append_main_event(session, "remote_peer_ready", {"session_id": "session-1"})
    assert persisted == []

    session.enable_trace_persistence("session-1")
    _append_main_event(session, "assistant_message", {"content": "stored"})
    _append_main_event(session, "session_run_end", {"response": "done"})

    assert [event["type"] for event in persisted] == [
        "session_run_start",
        "assistant_delta",
        "document_draft_progress",
        "document_draft_snapshot",
        "tool_call_stream",
        "remote_peer_ready",
        "assistant_message",
        "session_run_end",
    ]
    assert [event["session_id"] for event in persisted] == ["session-1"] * 8
    event_by_type = {event["type"]: event for event in session.events}
    assert event_by_type["assistant_delta"]["session_event_seq"] == 2
    assert event_by_type["document_draft_progress"]["session_event_seq"] == 3
    assert event_by_type["document_draft_snapshot"]["session_event_seq"] == 4
    assert event_by_type["tool_call_stream"]["session_event_seq"] == 5
    assert event_by_type["remote_peer_ready"]["session_event_seq"] == 6
    assert event_by_type["assistant_message"]["session_event_seq"] == 7
    assert event_by_type["session_run_end"]["session_event_seq"] == 8


def test_remote_session_run_localizes_message_key_at_session_boundary(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        locale="zh-CN",
    )

    _append_main_event(
        session,
        "provider_stream_interrupted",
        {
            "message_key": "provider_stream_interrupted.recovering",
            "diagnostic_message": "peer closed connection without sending complete message body",
        },
    )

    event = session.events[-1]
    assert event["payload"]["message"] == "模型输出流中断，正在尝试恢复。"
    assert event["payload"]["message_key"] == "provider_stream_interrupted.recovering"
    assert (
        event["payload"]["diagnostic_message"]
        == "peer closed connection without sending complete message body"
    )


def test_session_run_terminal_event_updates_selected_runtime_lifecycle(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-terminal-event-lifecycle",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )
    writer.mark_running()

    writer.append_event("session_run_end", {"response": "done"})
    status = session.status_payload(0, branch_binding_id="main")

    assert status["status"] == "done"
    assert status["done"] is True
    assert session.status == "done"
    assert session.running is False
    assert session.done is True
    assert session.finished_at is not None


def test_session_run_sibling_terminal_event_does_not_update_selected_lifecycle(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-sibling-terminal-event-lifecycle",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-a",
            "session_run_id": "run-sibling-terminal-event-lifecycle",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-run-a",
            "target_agent_run_id": "agent-run-a",
            "selected": False,
            "status": "active",
        }
    )
    branch_writer = session.scoped_writer(
        branch_binding_id="branch-a",
        agent_run_id="agent-run-a",
    )
    branch_writer.mark_running()
    selected_model_status_before = session.status

    branch_writer.append_event("session_run_end", {"response": "branch done"})
    branch_status = session.status_payload(0, branch_binding_id="branch-a")
    selected_status = session.status_payload(0, branch_binding_id="main")

    assert branch_status["status"] == "done"
    assert branch_status["done"] is True
    assert selected_status["status"] == "active"
    assert selected_status["done"] is False
    assert session.status == selected_model_status_before
    assert session.running is False
    assert session.done is False
    assert session.finished_at is None


def test_session_run_status_response_fails_closed_for_unbound_branch(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-response-unbound-branch",
        peer_id="peer-1",
        agent_run_id="agent-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.scoped_writer(
            branch_binding_id="branch-a",
            agent_run_id="agent-branch-a",
        )


def test_peer_disconnect_without_scoped_branch_fails_closed(tmp_path: Path) -> None:
    relay = RelayServer()
    service = RemoteRelayHTTPService(
        relay_server=relay,
        bind="127.0.0.1:0",
        session_run_artifact_root=tmp_path,
    )
    session = _SessionRunProjection(
        session_run_id="run-disconnect-unscoped",
        peer_id="peer-unscoped",
        artifact_root=tmp_path,
        status="running",
        running=True,
    )
    session.branch_bindings["branch-without-agent"] = {
        "branch_binding_id": "branch-without-agent",
        "agent_run_id": "",
        "selected": True,
        "status": "active",
    }

    with service._session_runs_lock:
        service._session_runs[session.session_run_id] = session

    service._abort_peer_session_runs("peer-unscoped", "peer_disconnected")

    assert session.status == "running"
    assert session.running is True
    assert session.done is False
    assert session.finished_at is None
    assert session.events == []


def test_session_run_agent_projection_is_raw_event_idempotent(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-agent-projection-idempotent",
        peer_id="peer-1",
        session_hint="session-1",
        branch_binding_id="main",
        agent_run_id="agent-main",
        artifact_root=tmp_path,
    )
    _ensure_main_branch(session)
    events = [
        {
            "agent_run_id": "agent-main",
            "seq": 1,
            "type": "log",
            "payload": {
                "type": "log",
                "text": "loading source bundle",
                "data": {"level": "info"},
            },
        },
        {
            "agent_run_id": "agent-main",
            "seq": 2,
            "type": "error",
            "payload": {"message": "preview failed"},
        },
    ]

    assert session.project_agent_run_events("main", events) == 2
    first_snapshot = session.events
    session._agent_run_projection_cursors_by_branch["main"] = 0

    assert session.project_agent_run_events("main", events) == 0
    assert session.events == first_snapshot


def test_session_run_agent_projection_maps_cancelled_terminal_to_cancelled(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-agent-projection-cancelled",
        peer_id="peer-1",
        session_hint="session-1",
        branch_binding_id="main",
        agent_run_id="agent-main",
        artifact_root=tmp_path,
    )
    _ensure_main_branch(session)

    assert session.project_agent_run_events(
        "main",
        [
            {
                "agent_run_id": "agent-main",
                "seq": 1,
                "type": "cancelled",
                "payload": {
                    "agent_run": {
                        "status": "cancelled",
                        "cancel_reason": "user_stop",
                    }
                },
            }
        ],
    ) == 2

    event_types = [event["type"] for event in session.events]
    assert "session_run_cancelled" in event_types
    assert "session_run_failed" not in event_types
    assert session.branch_bindings["main"]["status"] == "cancelled"
    assert session.status == "cancelled"
    assert session.last_error == "user_stop"


def test_remote_session_run_adds_server_enqueue_latency_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "labrastro_server.interfaces.http.remote.service.time.time",
        lambda: 10.25,
    )
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    _append_main_event(session, "stream_observability", {"emitted_at": 10.0})

    payload = session.events[0]["payload"]
    assert payload["server_enqueued_at"] == 10.25
    assert payload["server_enqueue_latency_ms"] == 250


def test_remote_session_run_uses_english_notice_for_english_locale(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        locale="en",
    )

    _append_main_event(
        session,
        "error",
        {
            "code": "capability_package_session_failed",
            "message_key": "capability_package.session_failed",
            "diagnostic_message": "boom",
        },
    )

    event = session.events[-1]
    assert event["payload"]["message"] == "Capability package workflow failed."
    assert event["payload"]["diagnostic_message"] == "boom"


def test_remote_session_run_session_coalesces_fast_live_events(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    _append_main_event(session, "assistant_delta", {"content": "A"})
    events, _done, cursor = session.wait_events(0, 0, branch_binding_id="main")
    assert [event["payload"]["content"] for event in events] == ["A"]

    _append_main_event(session, "assistant_delta", {"content": "B"})
    _append_main_event(session, "assistant_delta", {"content": "C"})
    events, _done, cursor = session.wait_events(cursor, 0, branch_binding_id="main")
    assert events == []

    events, _done, _cursor = session.wait_events(
        cursor,
        0.2,
        branch_binding_id="main",
    )
    assert [event["type"] for event in events] == ["assistant_delta"]
    assert events[0]["payload"]["content"] == "BC"


def test_remote_session_run_live_coalescing_is_branch_scoped(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-live-branch-scope",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-a",
            "session_run_id": "run-live-branch-scope",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-a",
            "selected": True,
            "status": "running",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-b",
            "session_run_id": "run-live-branch-scope",
            "branch_binding_id": "branch-b",
            "agent_run_id": "agent-b",
            "selected": False,
            "status": "running",
        }
    )

    session.append_event(
        "assistant_delta",
        {
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-a",
            "content": "A0",
        },
    )
    events, _done, cursor_a = session.wait_events(
        0,
        0,
        branch_binding_id="branch-a",
    )
    assert [event["payload"]["content"] for event in events] == ["A0"]

    session.append_event(
        "assistant_delta",
        {
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-a",
            "content": "A1",
        },
    )
    session.append_event(
        "assistant_delta",
        {
            "branch_binding_id": "branch-b",
            "agent_run_id": "agent-b",
            "content": "B1",
        },
    )

    branch_b_events, _done, _cursor_b = session.wait_events(
        0,
        0,
        branch_binding_id="branch-b",
    )
    assert [event["payload"]["content"] for event in branch_b_events] == ["B1"]

    branch_a_events, _done, _cursor_a = session.wait_events(
        cursor_a,
        0.2,
        branch_binding_id="branch-a",
    )
    assert [event["payload"]["content"] for event in branch_a_events] == ["A1"]


def test_remote_session_run_tool_call_stream_coalescing_is_branch_scoped(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-tool-stream-branch-scope",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )
    for branch_binding_id, agent_run_id, selected in (
        ("branch-a", "agent-a", True),
        ("branch-b", "agent-b", False),
    ):
        session.record_branch_binding(
            {
                "id": f"binding-{branch_binding_id}",
                "session_run_id": "run-tool-stream-branch-scope",
                "branch_binding_id": branch_binding_id,
                "agent_run_id": agent_run_id,
                "selected": selected,
                "status": "running",
            }
        )

    base_payload = {
        "tool_call_id": "tool-call-shared",
        "tool_name": "lookup",
        "stream": "stdout",
        "format": "text",
    }
    session.append_event(
        "tool_call_stream",
        {
            **base_payload,
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-a",
            "content": "A0",
        },
    )
    events, _done, cursor_a = session.wait_events(
        0,
        0,
        branch_binding_id="branch-a",
    )
    assert [event["payload"]["content"] for event in events] == ["A0"]

    session.append_event(
        "tool_call_stream",
        {
            **base_payload,
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-a",
            "content": "A1",
        },
    )
    session.append_event(
        "tool_call_stream",
        {
            **base_payload,
            "branch_binding_id": "branch-b",
            "agent_run_id": "agent-b",
            "content": "B1",
        },
    )

    branch_b_events, _done, _cursor_b = session.wait_events(
        0,
        0,
        branch_binding_id="branch-b",
    )
    assert [event["payload"]["content"] for event in branch_b_events] == ["B1"]

    branch_a_events, _done, _cursor_a = session.wait_events(
        cursor_a,
        0.2,
        branch_binding_id="branch-a",
    )
    assert [event["payload"]["content"] for event in branch_a_events] == ["A1"]


def test_remote_session_run_wait_events_requires_branch_scope(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-events-require-scope",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.wait_events(0, 0)


def test_remote_session_run_does_not_create_main_scope_without_explicit_branch(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-no-implicit-main",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    assert session.branch_binding_id is None
    assert session.selected_branch_binding_id == ""
    assert session.branch_summaries() == []


def test_remote_session_run_lifecycle_mutators_require_branch_scope(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-lifecycle-require-scope",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.mark_running()
    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.mark_done()


def test_remote_session_run_events_lost_notice_is_scoped_to_requested_branch(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-events-lost-scope",
        peer_id="peer-1",
        artifact_root=tmp_path,
        max_events=1,
    )
    _append_main_event(session, "assistant_message", {"content": "dropped"})
    _append_main_event(session, "assistant_message", {"content": "kept"})

    events, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="main",
    )

    assert [event["type"] for event in events] == ["events_lost", "assistant_message"]
    assert events[0]["payload"]["branch_binding_id"] == "main"
    assert events[0]["payload"]["dropped_count"] == 1
    assert events[1]["payload"]["content"] == "kept"


def test_remote_session_run_branch_projection_uses_parent_prefix_without_sibling_tail(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-branch",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "active",
        }
    )
    session.append_event(
        "user_message",
        {
            "session_item_id": "msg-1",
            "branch_binding_id": "main",
            "content": "original question",
        },
    )
    session.append_event(
        "assistant_message",
        {
            "session_item_id": "msg-2",
            "branch_binding_id": "main",
            "content": "source branch answer after edit point",
        },
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-1",
            "session_run_id": "run-branch",
            "branch_binding_id": "branch-1",
            "agent_run_id": "agent-run-branch-1",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "source_agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-branch-1",
            "selected": True,
            "status": "active",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-2",
            "session_run_id": "run-branch",
            "branch_binding_id": "branch-2",
            "agent_run_id": "agent-run-branch-2",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "source_agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-branch-2",
            "selected": False,
            "status": "active",
        }
    )
    session.append_event(
        "user_message",
        {
            "session_item_id": "branch-msg-1",
            "branch_binding_id": "branch-1",
            "content": "edited question",
        },
    )
    session.append_event(
        "assistant_message",
        {
            "session_item_id": "branch-msg-2",
            "branch_binding_id": "branch-1",
            "content": "derived branch answer",
        },
    )
    session.append_event(
        "assistant_message",
        {
            "session_item_id": "msg-3",
            "branch_binding_id": "main",
            "content": "source branch keeps running",
        },
    )

    events, _done, _cursor = session.wait_events(0, 0, branch_binding_id="branch-1")
    contents = [event["payload"].get("content") for event in events]

    assert contents == [
        "original question",
        "edited question",
        "derived branch answer",
    ]
    assert (
        session.compose_branch_prompt(
            source_branch_binding_id="main",
            base_session_item_id="msg-1",
            prompt="edited question",
        )
        == "User: original question\n\nUser: edited question"
    )
    assert (
        session.compose_branch_prompt(
            source_branch_binding_id="main",
            base_session_item_id="missing-msg",
            prompt="edited question",
        )
        == "edited question"
    )
    assert (
        session.compose_branch_prompt(
            source_branch_binding_id="",
            base_session_item_id="msg-1",
            prompt="edited question",
        )
        == "edited question"
    )
    assert len(session.events) == 5
    branches = {
        item["branch_binding_id"]: item
        for item in session.status_payload(0, branch_binding_id="branch-1")["branches"]
    }
    assert branches["branch-1"]["selected"] is True
    assert branches["branch-1"]["current_index"] == 1
    assert branches["branch-1"]["total_sibling_count"] == 2
    assert branches["branch-2"]["total_sibling_count"] == 2
    assert branches["main"]["has_updates"] is True


def test_remote_session_run_branch_summary_sibling_order_uses_created_at_not_binding_id(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-summary-order",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-branch-summary-order",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "active",
            "created_at": "2026-06-20T00:00:00+00:00",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-z",
            "session_run_id": "run-branch-summary-order",
            "branch_binding_id": "branch-z",
            "agent_run_id": "agent-run-branch-z",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "source_agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-branch-z",
            "selected": False,
            "status": "active",
            "created_at": "2026-06-20T00:00:01+00:00",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-a",
            "session_run_id": "run-branch-summary-order",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-run-branch-a",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "source_agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-branch-a",
            "selected": False,
            "status": "active",
            "created_at": "2026-06-20T00:00:02+00:00",
        }
    )

    summaries = session.status_payload(0, branch_binding_id="branch-z")["branches"]
    sibling_summaries = [
        item
        for item in summaries
        if item["parent_branch_binding_id"] == "main"
        and item["base_session_item_id"] == "msg-1"
    ]
    by_branch = {item["branch_binding_id"]: item for item in sibling_summaries}

    assert [item["branch_binding_id"] for item in sibling_summaries] == [
        "branch-z",
        "branch-a",
    ]
    assert by_branch["branch-z"]["current_index"] == 1
    assert by_branch["branch-a"]["current_index"] == 2


def test_remote_session_run_branch_projection_root_base_uses_empty_parent_prefix(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-root-branch",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-root-branch",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "active",
        }
    )
    session.append_event(
        "user_message",
        {
            "session_item_id": "source-msg-1",
            "branch_binding_id": "main",
            "content": "original first question",
        },
    )
    session.append_event(
        "assistant_message",
        {
            "session_item_id": "source-msg-2",
            "branch_binding_id": "main",
            "content": "source first answer",
        },
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-root",
            "session_run_id": "run-root-branch",
            "branch_binding_id": "branch-root",
            "agent_run_id": "agent-run-branch-root",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "__root__",
            "source_agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-branch-root",
            "selected": True,
            "status": "active",
        }
    )
    session.append_event(
        "user_message",
        {
            "session_item_id": "branch-msg-1",
            "branch_binding_id": "branch-root",
            "content": "edited first question",
        },
    )
    session.append_event(
        "assistant_message",
        {
            "session_item_id": "branch-msg-2",
            "branch_binding_id": "branch-root",
            "content": "derived first answer",
        },
    )

    events, _done, _cursor = session.wait_events(0, 0, branch_binding_id="branch-root")
    contents = [event["payload"].get("content") for event in events]

    assert contents == [
        "edited first question",
        "derived first answer",
    ]
    assert len(session.events) == 4


def test_remote_session_run_explicit_branch_wait_ignores_selected_done(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-background-wait",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-background-wait",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "done",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-bg",
            "session_run_id": "run-background-wait",
            "branch_binding_id": "branch-bg",
            "agent_run_id": "agent-run-bg",
            "target_agent_run_id": "agent-run-bg",
            "selected": False,
            "status": "running",
        }
    )
    session.mark_done(branch_binding_id="main")

    def append_background_event() -> None:
        time.sleep(0.02)
        session.append_event(
            "assistant_message",
            {
                "session_item_id": "bg-msg-1",
                "branch_binding_id": "branch-bg",
                "content": "background branch finished later",
            },
        )

    thread = threading.Thread(target=append_background_event)
    thread.start()
    try:
        events, done, _cursor = session.wait_events(
            0,
            0.5,
            branch_binding_id="branch-bg",
        )
    finally:
        thread.join(timeout=1)

    assert done is False
    assert [event["payload"].get("content") for event in events] == [
        "background branch finished later"
    ]


def test_remote_session_run_wait_events_ignores_hidden_branch_activity_for_wait(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-wait-hidden",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-a",
            "session_run_id": "run-branch-wait-hidden",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-run-a",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "selected": False,
            "status": "running",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-b",
            "session_run_id": "run-branch-wait-hidden",
            "branch_binding_id": "branch-b",
            "agent_run_id": "agent-run-b",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "selected": False,
            "status": "running",
        }
    )
    session.append_event(
        "assistant_message",
        {
            "session_item_id": "branch-b-msg",
            "branch_binding_id": "branch-b",
            "content": "hidden from branch A",
        },
    )

    started_at = time.monotonic()
    events, done, _cursor = session.wait_events(
        0,
        0.05,
        branch_binding_id="branch-a",
    )
    elapsed = time.monotonic() - started_at

    assert events == []
    assert done is False
    assert elapsed >= 0.03


def test_remote_session_run_pending_inputs_are_branch_local(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-pending",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-branch-pending",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "active",
        }
    )
    session.register_approval(
        "approval-main",
        {"tool_call_id": "tool-main", "branch_binding_id": "main"},
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-1",
            "session_run_id": "run-branch-pending",
            "branch_binding_id": "branch-1",
            "agent_run_id": "agent-run-branch-1",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "source_agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-branch-1",
            "selected": True,
            "status": "active",
        }
    )
    session.register_user_input(
        "input-branch",
        {"kind": "clarification", "branch_binding_id": "branch-1"},
    )

    status = session.status_payload(0, branch_binding_id="branch-1")
    branches = {item["branch_binding_id"]: item for item in status["branches"]}

    assert status["approvals"] == []
    assert [item["input_id"] for item in status["user_inputs"]] == ["input-branch"]
    main_status = session.status_payload(0, branch_binding_id="main")
    assert [item["approval_id"] for item in main_status["approvals"]] == ["approval-main"]
    assert main_status["user_inputs"] == []
    assert branches["main"]["pending_approval_count"] == 1
    assert branches["branch-1"]["pending_user_input_count"] == 1
    assert (
        session.resolve_approval(
            "approval-main",
            "allow_once",
            None,
            branch_binding_id="branch-1",
        )
        is None
    )
    assert (
        session.resolve_approval(
            "approval-main",
            "allow_once",
            None,
            branch_binding_id="main",
        )
        == "resolved"
    )
    assert (
        session.resolve_user_input(
            "input-branch",
            "decline",
            {},
            None,
            branch_binding_id="main",
        )
        is None
    )
    assert (
        session.resolve_user_input(
            "input-branch",
            "decline",
            {},
            None,
            branch_binding_id="branch-1",
        )
        == "resolved"
    )


def test_remote_session_run_approval_ids_are_branch_local(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-approval-id-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    for branch_binding_id, tool_call_id in (
        ("branch-a", "tool-a"),
        ("branch-b", "tool-b"),
    ):
        session.record_branch_binding(
            {
                "id": f"binding-{branch_binding_id}",
                "session_run_id": "run-approval-id-scope",
                "branch_binding_id": branch_binding_id,
                "agent_run_id": f"agent-{branch_binding_id}",
                "selected": branch_binding_id == "branch-a",
                "status": "active",
            }
        )
        session.register_approval(
            "approval-shared",
            {
                "tool_call_id": tool_call_id,
                "branch_binding_id": branch_binding_id,
                "agent_run_id": f"agent-{branch_binding_id}",
            },
        )

    status_a = session.status_payload(0, branch_binding_id="branch-a")
    status_b = session.status_payload(0, branch_binding_id="branch-b")
    assert [item["tool_call_id"] for item in status_a["approvals"]] == ["tool-a"]
    assert [item["tool_call_id"] for item in status_b["approvals"]] == ["tool-b"]

    assert (
        session.resolve_approval(
            "approval-shared",
            "allow_once",
            None,
            branch_binding_id="branch-a",
        )
        == "resolved"
    )
    assert (
        session.resolve_approval(
            "approval-shared",
            "deny_once",
            "branch-b denied",
            branch_binding_id="branch-b",
        )
        == "resolved"
    )

    session.append_event(
        "approval_resolved",
        {
            "approval_id": "approval-shared",
            "decision": "allow_once",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-branch-a",
        },
    )
    session.append_event(
        "approval_resolved",
        {
            "approval_id": "approval-shared",
            "decision": "deny_once",
            "reason": "branch-b denied",
            "branch_binding_id": "branch-b",
            "agent_run_id": "agent-branch-b",
        },
    )

    events_a, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-a",
    )
    events_b, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-b",
    )
    approval_a = [
        event for event in events_a if event["type"] == "approval_resolved"
    ]
    approval_b = [
        event for event in events_b if event["type"] == "approval_resolved"
    ]
    assert [event["payload"]["decision"] for event in approval_a] == ["allow_once"]
    assert [event["payload"]["decision"] for event in approval_b] == ["deny_once"]


def test_remote_session_run_approval_payload_id_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-approval-id-mismatch",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="approval_id_mismatch"):
        session.register_approval(
            "approval-state-key",
            {
                "approval_id": "approval-visible-payload",
                "branch_binding_id": "main",
                "tool_call_id": "tool-main",
            },
        )


@pytest.mark.parametrize(
    ("event_type", "id_field", "payload_extra"),
    [
        ("approval_resolved", "approval_id", {"decision": "allow_once"}),
        ("user_input_resolved", "input_id", {"action": "accept", "content": {}}),
    ],
)
def test_remote_session_run_resolved_event_unknown_branch_failure_does_not_poison_dedupe(
    tmp_path: Path,
    event_type: str,
    id_field: str,
    payload_extra: dict,
) -> None:
    session = _SessionRunProjection(
        session_run_id=f"run-{event_type}-unknown-branch-dedupe",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    payload = {
        id_field: "shared-id",
        "branch_binding_id": "branch-late",
        "agent_run_id": "agent-late",
        **payload_extra,
    }

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.append_event(event_type, dict(payload))

    _record_session_branch(session, "branch-late", agent_run_id="agent-late")
    session.append_event(event_type, dict(payload))

    events, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-late",
    )
    assert [event["type"] for event in events if event["type"] == event_type] == [
        event_type
    ]


@pytest.mark.parametrize(
    ("event_type", "id_field", "payload_extra"),
    [
        ("approval_resolved", "approval_id", {"decision": "allow_once"}),
        ("user_input_resolved", "input_id", {"action": "accept", "content": {}}),
    ],
)
def test_remote_session_run_resolved_event_missing_agent_failure_does_not_poison_dedupe(
    tmp_path: Path,
    event_type: str,
    id_field: str,
    payload_extra: dict,
) -> None:
    session = _SessionRunProjection(
        session_run_id=f"run-{event_type}-missing-agent-dedupe",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-metadata-only",
            "session_run_id": session.session_run_id,
            "branch_binding_id": "metadata-only",
            "selected": False,
            "status": "active",
        }
    )
    payload = {
        id_field: "shared-id",
        "branch_binding_id": "metadata-only",
        "agent_run_id": "agent-metadata",
        **payload_extra,
    }

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.append_event(event_type, dict(payload))

    _record_session_branch(session, "metadata-only", agent_run_id="agent-metadata")
    session.append_event(event_type, dict(payload))

    events, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="metadata-only",
    )
    assert [event["type"] for event in events if event["type"] == event_type] == [
        event_type
    ]


def test_remote_session_run_user_input_ids_are_branch_local(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-user-input-id-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    for branch_binding_id, message in (
        ("branch-a", "question A"),
        ("branch-b", "question B"),
    ):
        session.record_branch_binding(
            {
                "id": f"binding-{branch_binding_id}",
                "session_run_id": "run-user-input-id-scope",
                "branch_binding_id": branch_binding_id,
                "agent_run_id": f"agent-{branch_binding_id}",
                "selected": branch_binding_id == "branch-a",
                "status": "active",
            }
        )
        session.register_user_input(
            "input-shared",
            {
                "kind": "clarification",
                "message": message,
                "branch_binding_id": branch_binding_id,
                "agent_run_id": f"agent-{branch_binding_id}",
            },
        )

    status_a = session.status_payload(0, branch_binding_id="branch-a")
    status_b = session.status_payload(0, branch_binding_id="branch-b")
    assert [item["message"] for item in status_a["user_inputs"]] == ["question A"]
    assert [item["message"] for item in status_b["user_inputs"]] == ["question B"]

    assert (
        session.resolve_user_input(
            "input-shared",
            "accept",
            {"answer": "A"},
            None,
            branch_binding_id="branch-a",
        )
        == "resolved"
    )
    assert session.wait_user_input(
        "input-shared",
        timeout_sec=0,
        branch_binding_id="branch-a",
    ) == ("accept", {"answer": "A"}, None)
    assert (
        session.resolve_user_input(
            "input-shared",
            "decline",
            {},
            "branch-b declined",
            branch_binding_id="branch-b",
        )
        == "resolved"
    )
    assert session.wait_user_input(
        "input-shared",
        timeout_sec=0,
        branch_binding_id="branch-b",
    ) == ("decline", {}, "branch-b declined")

    session.append_event(
        "user_input_resolved",
        {
            "input_id": "input-shared",
            "action": "accept",
            "content": {"answer": "A"},
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-branch-a",
        },
    )
    session.append_event(
        "user_input_resolved",
        {
            "input_id": "input-shared",
            "action": "decline",
            "content": {},
            "reason": "branch-b declined",
            "branch_binding_id": "branch-b",
            "agent_run_id": "agent-branch-b",
        },
    )

    events_a, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-a",
    )
    events_b, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-b",
    )
    input_a = [
        event for event in events_a if event["type"] == "user_input_resolved"
    ]
    input_b = [
        event for event in events_b if event["type"] == "user_input_resolved"
    ]
    assert [event["payload"]["action"] for event in input_a] == ["accept"]
    assert [event["payload"]["action"] for event in input_b] == ["decline"]


def test_remote_session_run_user_input_payload_id_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-user-input-id-mismatch",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="input_id_mismatch"):
        session.register_user_input(
            "input-state-key",
            {
                "input_id": "input-visible-payload",
                "branch_binding_id": "main",
                "kind": "clarification",
            },
        )


def test_remote_session_run_revision_feedback_ids_are_branch_local(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-revision-feedback-id-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    _record_session_branch(session, "branch-a", agent_run_id="agent-branch-a")
    _record_session_branch(session, "branch-b", agent_run_id="agent-branch-b")

    ticket_a = session.submit_revision_feedback(
        "revise branch A",
        revision_feedback_id="revision-shared",
        branch_binding_id="branch-a",
    )
    ticket_b = session.submit_revision_feedback(
        "revise branch B",
        revision_feedback_id="revision-shared",
        branch_binding_id="branch-b",
    )

    assert ticket_a["text"] == "revise branch A"
    assert ticket_b["text"] == "revise branch B"

    session.mark_revision_feedback_consumed(
        "revision-shared",
        branch_binding_id="branch-b",
    )

    live_a = session.revision_feedback_ticket(
        "revision-shared",
        branch_binding_id="branch-a",
    )
    live_b = session.revision_feedback_ticket(
        "revision-shared",
        branch_binding_id="branch-b",
    )
    assert live_a is not None
    assert live_b is not None
    assert live_a["state"] == "pending"
    assert live_b["state"] == "consumed"

    events_a, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-a",
    )
    events_b, _done, _cursor = session.wait_events(
        0,
        0,
        branch_binding_id="branch-b",
    )
    assert [
        event["payload"]["text"]
        for event in events_a
        if event["type"] == "session_run_revision_feedback_accepted"
    ] == ["revise branch A"]
    assert [
        event["payload"]["text"]
        for event in events_b
        if event["type"] == "session_run_revision_feedback_accepted"
    ] == ["revise branch B"]
    assert [
        event["payload"]["branch_binding_id"]
        for event in events_b
        if event["type"] == "session_run_revision_feedback_consumed"
    ] == ["branch-b"]


def test_remote_session_run_revision_feedback_unknown_branch_fails_without_ticket(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-revision-feedback-unknown-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.submit_revision_feedback(
            "orphan revision",
            revision_feedback_id="revision-orphan",
            branch_binding_id="branch-missing",
        )

    assert (
        session.revision_feedback_ticket(
            "revision-orphan",
            branch_binding_id="branch-missing",
        )
        is None
    )
    assert [
        event
        for event in session.events
        if event["type"] == "session_run_revision_feedback_accepted"
    ] == []


def test_remote_session_run_revision_feedback_requires_branch_agent_without_ticket(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-revision-feedback-missing-agent",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-metadata-only",
            "session_run_id": "run-revision-feedback-missing-agent",
            "branch_binding_id": "metadata-only",
            "selected": False,
            "status": "active",
        }
    )

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.submit_revision_feedback(
            "metadata-only revision",
            revision_feedback_id="revision-metadata-only",
            branch_binding_id="metadata-only",
        )

    assert (
        session.revision_feedback_ticket(
            "revision-metadata-only",
            branch_binding_id="metadata-only",
        )
        is None
    )
    assert [
        event
        for event in session.events
        if event["type"] == "session_run_revision_feedback_accepted"
    ] == []


def test_remote_session_run_status_payload_is_branch_runtime_scoped(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-status-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-a",
            "session_run_id": "run-branch-status-scope",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-run-a",
            "parent_branch_binding_id": "main",
            "base_session_item_id": "msg-1",
            "selected": False,
            "status": "running",
        }
    )

    session.append_event(
        "error",
        {
            "branch_binding_id": "branch-a",
            "message": "branch A failed",
        },
    )

    branch_status = session.status_payload(0, branch_binding_id="branch-a")
    main_status = session.status_payload(0, branch_binding_id="main")
    branch_summaries = {
        item["branch_binding_id"]: item for item in branch_status["branches"]
    }

    assert branch_status["status"] == "error"
    assert branch_status["done"] is True
    assert branch_status["running"] is False
    assert branch_status["reconnectable"] is False
    assert branch_status["error"] == "branch A failed"
    assert main_status["status"] == "active"
    assert main_status["done"] is False
    assert main_status["error"] is None
    assert branch_summaries["branch-a"]["status"] == "error"
    assert branch_summaries["main"]["status"] == "active"


def test_remote_session_run_status_payload_rejects_unknown_branch_scope(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-unknown-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.status_payload(0, branch_binding_id="branch-missing")


def test_remote_session_run_branch_summaries_do_not_fabricate_selected_scope(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-summary-no-fabricated-selected",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.selected_branch_binding_id = "branch-ghost"

    status = session.status_payload(0, branch_binding_id="main")

    assert {
        item["branch_binding_id"] for item in status["branches"]
    } == {"main"}


def test_remote_session_run_branch_summaries_skip_branch_without_agent_proof(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-summary-skip-metadata-only",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-metadata-only",
            "session_run_id": "run-summary-skip-metadata-only",
            "branch_binding_id": "metadata-only",
            "selected": False,
            "status": "active",
        }
    )

    status = session.status_payload(0, branch_binding_id="main")

    assert {
        item["branch_binding_id"] for item in status["branches"]
    } == {"main"}


def test_remote_session_run_projection_requires_branch_proof_after_start(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-requires-branch-proof",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    session.append_event("session_run_start", {"prompt": "hello"})
    start_event = session.events[-1]
    assert start_event["payload"]["branch_binding_id"] == "main"

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.append_event("assistant_message", {"content": "missing scope"})

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.append_live_event(
            "document_draft_preview_chunk",
            {"draft_id": "draft-1", "content": "missing scope"},
        )

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.register_approval("approval-missing", {"tool_call_id": "tool-1"})

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.register_user_input("input-missing", {"kind": "clarification"})

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.register_recovery({"response": "missing scope"})

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.request_cancel("missing scope")

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.cancel_pending_approvals("missing scope")

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.cancel_pending_user_inputs("missing scope")


def test_remote_session_run_start_without_existing_branch_scope_fails_closed(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-start-requires-branch-proof",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.append_event("session_run_start", {"prompt": "missing scope"})


def test_remote_session_run_events_response_reads_runtime_snapshot_after_wait(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-events-runtime-after-wait",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )
    append_done = threading.Event()

    def append_after_wait_starts() -> None:
        time.sleep(0.05)
        writer.append_event("assistant_message", {"content": "wake wait"})
        append_done.set()

    def runtime_snapshot() -> tuple[str, bool]:
        assert append_done.is_set()
        return "completed", True

    thread = threading.Thread(target=append_after_wait_starts)
    thread.start()
    try:
        batch = writer.events_response_payload(
            0,
            1,
            runtime_snapshot=runtime_snapshot,
            selected=True,
        )
    finally:
        thread.join(timeout=2)

    assert batch["done"] is True
    assert batch["events"]


def test_remote_session_run_selected_runtime_scope_does_not_reuse_previous_error(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-selected-error-isolation",
        peer_id="peer-1",
        agent_run_id="agent-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.last_error = "main failed"
    session.record_branch_binding(
        {
            "id": "binding-branch-a",
            "session_run_id": "run-selected-error-isolation",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-branch-a",
            "selected": True,
            "status": "active",
        }
    )
    writer = session.scoped_writer(
        branch_binding_id="branch-a",
        agent_run_id="agent-branch-a",
    )

    writer.apply_selected_runtime_scope(
        runtime_status="cancelled",
        terminal=True,
    )

    assert session.status == "cancelled"
    assert session.last_error is None


def test_remote_session_run_projection_rejects_unknown_branch_scope_after_start(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-unknown-branch-event",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.append_event(
            "assistant_message",
            {"branch_binding_id": "branch-missing", "content": "orphan"},
        )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.register_approval(
            "approval-missing-branch",
            {"branch_binding_id": "branch-missing", "tool_call_id": "tool-1"},
        )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.register_user_input(
            "input-missing-branch",
            {"branch_binding_id": "branch-missing", "kind": "clarification"},
        )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.register_recovery(
            {"branch_binding_id": "branch-missing", "response": "orphan recovery"},
        )

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.mark_running(branch_binding_id="branch-missing")

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.mark_done(branch_binding_id="branch-missing")


def test_remote_session_run_projection_rejects_branch_scope_without_agent_proof(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-missing-branch-agent",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-metadata-only",
            "session_run_id": "run-missing-branch-agent",
            "branch_binding_id": "metadata-only",
            "selected": False,
            "status": "active",
        }
    )

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.append_event(
            "assistant_message",
            {"branch_binding_id": "metadata-only", "content": "orphan"},
        )

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.register_approval(
            "approval-metadata-only",
            {"branch_binding_id": "metadata-only", "tool_call_id": "tool-1"},
        )

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.register_user_input(
            "input-metadata-only",
            {"branch_binding_id": "metadata-only", "kind": "clarification"},
        )

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.mark_running(branch_binding_id="metadata-only")

    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.mark_done(branch_binding_id="metadata-only")


def test_remote_session_run_scoped_writer_requires_agent_and_known_branch(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-scoped-writer-proof",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )

    with pytest.raises(ValueError, match="agent_run_id_required"):
        session.scoped_writer(branch_binding_id="main")

    with pytest.raises(ValueError, match="session_run_branch_binding_not_found"):
        session.scoped_writer(
            branch_binding_id="branch-missing",
            agent_run_id="agent-run-main",
        )

    with pytest.raises(ValueError, match="agent_run_id_mismatch"):
        session.scoped_writer(
            branch_binding_id="main",
            agent_run_id="agent-run-other",
        )

    session.record_branch_binding(
        {
            "id": "binding-metadata-only",
            "session_run_id": "run-scoped-writer-proof",
            "branch_binding_id": "metadata-only",
            "selected": False,
            "status": "active",
        }
    )
    with pytest.raises(ValueError, match="session_run_branch_agent_run_required"):
        session.scoped_writer(
            branch_binding_id="metadata-only",
            agent_run_id="agent-run-metadata",
        )


def test_remote_session_run_branch_runtime_marks_do_not_pollute_selected_scope(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-runtime-mark-scope",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-branch-runtime-mark-scope",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "active",
        }
    )
    session.record_branch_binding(
        {
            "id": "binding-branch-a",
            "session_run_id": "run-branch-runtime-mark-scope",
            "branch_binding_id": "branch-a",
            "agent_run_id": "agent-run-a",
            "target_agent_run_id": "agent-run-a",
            "selected": False,
            "status": "active",
        }
    )
    branch_writer = session.scoped_writer(
        branch_binding_id="branch-a",
        agent_run_id="agent-run-a",
    )

    branch_writer.mark_running()

    branch_status = session.status_payload(0, branch_binding_id="branch-a")
    main_status = session.status_payload(0, branch_binding_id="main")
    assert branch_status["status"] == "running"
    assert branch_status["running"] is True
    assert branch_status["agent_run_id"] == "agent-run-a"
    assert branch_status["runtime_state"]["agent_run_id"] == "agent-run-a"
    assert branch_status["runtime_state"]["branch_binding_id"] == "branch-a"
    assert main_status["status"] == "active"
    assert main_status["running"] is False
    assert main_status["agent_run_id"] == "agent-run-main"
    assert session.running is False

    session.append_event(
        "error",
        {
            "branch_binding_id": "main",
            "message": "main failed",
        },
    )
    selected_finished_at = session.finished_at

    branch_writer.mark_done("branch finished")

    branch_status = session.status_payload(0, branch_binding_id="branch-a")
    main_status = session.status_payload(0, branch_binding_id="main")
    assert branch_status["status"] == "done"
    assert branch_status["done"] is True
    assert branch_status["finished_at"] is not None
    assert branch_status["error"] is None
    assert main_status["status"] == "error"
    assert main_status["error"] == "main failed"
    assert main_status["finished_at"] is not None
    assert session.status == "error"
    assert session.finished_at == selected_finished_at


def test_remote_session_run_status_response_updates_target_branch_runtime_metadata(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-response-sync",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )

    response = writer.status_response_payload(
        0,
        agent_run_status="completed",
        terminal=True,
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")
    _, wait_done, _ = writer.wait_events(0, 0)

    assert response["done"] is True
    assert response["branches"][0]["status"] == "done"
    assert persisted["status"] == "done"
    assert persisted["branches"][0]["status"] == "done"
    assert wait_done is True


def test_remote_session_run_status_response_uses_branch_runtime_truth_for_done(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-response-runtime-truth",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )

    response = writer.status_response_payload(
        0,
        agent_run_status="failed",
        terminal=False,
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")

    assert response["status"] == "error"
    assert response["done"] is True
    assert response["reconnectable"] is False
    assert response["branches"][0]["status"] == "error"
    assert response["runtime_state"]["agent_run_status"] == "failed"
    assert persisted["status"] == "error"
    assert persisted["done"] is True


def test_remote_session_run_status_response_treats_waiting_as_active_runtime(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-response-waiting-active",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )

    response = writer.status_response_payload(
        0,
        agent_run_status="waiting",
        terminal=False,
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")

    assert response["status"] == "waiting"
    assert response["running"] is True
    assert response["done"] is False
    assert persisted["running"] is True
    assert session.running is True


def test_remote_session_run_status_response_preserves_terminal_branch_against_stale_running(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-response-terminal-absorbs-stale",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )
    writer.append_event("session_run_failed", {"message": "model failed"})

    response = writer.status_response_payload(
        0,
        agent_run_status="running",
        terminal=False,
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")

    assert response["status"] == "error"
    assert response["done"] is True
    assert response["running"] is False
    assert response["branches"][0]["status"] == "error"
    assert response["runtime_state"]["agent_run_status"] == "running"
    assert response["finished_at"] is not None
    assert persisted["status"] == "error"
    assert persisted["done"] is True


def test_remote_session_run_events_response_updates_target_branch_runtime_metadata(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-events-response-sync",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )

    response = writer.events_response_payload(
        0,
        0,
        runtime_snapshot=lambda: ("completed", True),
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")
    _, wait_done, _ = writer.wait_events(0, 0)

    assert response["done"] is True
    assert response["branches"][0]["status"] == "done"
    assert persisted["status"] == "done"
    assert persisted["branches"][0]["status"] == "done"
    assert wait_done is True


def test_remote_session_run_events_response_preserves_terminal_branch_against_stale_running(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-events-response-terminal-absorbs-stale",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )
    writer.append_event("session_run_failed", {"message": "model failed"})

    response = writer.events_response_payload(
        0,
        0,
        runtime_snapshot=lambda: ("running", False),
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")

    assert response["done"] is True
    assert response["branches"][0]["status"] == "error"
    assert persisted["status"] == "error"
    assert persisted["done"] is True


def test_remote_session_run_status_response_preserves_interrupted_branch_metadata(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-status-response-interrupted",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    session.record_branch_binding(
        {
            "id": "binding-main",
            "session_run_id": "run-status-response-interrupted",
            "branch_binding_id": "main",
            "agent_run_id": "agent-run-main",
            "target_agent_run_id": "agent-run-main",
            "selected": True,
            "status": "interrupted",
            "last_error": "provider stream interrupted",
        }
    )
    writer = session.scoped_writer(
        branch_binding_id="main",
        agent_run_id="agent-run-main",
    )

    response = writer.status_response_payload(
        0,
        agent_run_status="completed",
        terminal=True,
        selected=True,
    )
    persisted = session.status_payload(0, branch_binding_id="main")

    assert response["branches"][0]["status"] == "interrupted"
    assert persisted["status"] == "interrupted"
    assert persisted["branches"][0]["status"] == "interrupted"
    assert persisted["error"] == "provider stream interrupted"


def test_remote_session_run_mark_done_preserves_sibling_branch_error_reason(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-done-preserves-error",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    _record_session_branch(session, "branch-a", agent_run_id="agent-branch-a")

    session.append_event(
        "error",
        {
            "branch_binding_id": "branch-a",
            "message": "branch A failed",
        },
    )
    session.mark_done(branch_binding_id="branch-a")

    branch_status = session.status_payload(0, branch_binding_id="branch-a")
    main_status = session.status_payload(0, branch_binding_id="main")
    assert branch_status["status"] == "error"
    assert branch_status["error"] == "branch A failed"
    assert main_status["status"] == "active"
    assert main_status["error"] is None


def test_remote_session_run_mark_done_does_not_use_selected_error_for_sibling_cancel(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-done-selected-error-isolated",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    _record_session_branch(session, "branch-a", agent_run_id="agent-branch-a")
    session.append_event(
        "error",
        {
            "branch_binding_id": "main",
            "message": "main failed",
        },
    )
    session.register_approval(
        "approval-branch-a",
        {
            "branch_binding_id": "branch-a",
            "tool_call_id": "tool-branch-a",
        },
    )

    session.mark_done(branch_binding_id="branch-a")

    decision, reason, _meta = session.wait_approval(
        "approval-branch-a",
        timeout_sec=0,
        branch_binding_id="branch-a",
    )
    branch_status = session.status_payload(0, branch_binding_id="branch-a")
    assert decision == "deny_once"
    assert reason == "session_run_closed"
    assert branch_status["status"] == "done"
    assert branch_status["error"] is None


def test_remote_session_run_explicit_branch_stream_rejects_branchless_events(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branchless-filter",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    branchless_event = {
        "session_run_id": session.session_run_id,
        "seq": 1,
        "type": "assistant_message",
        "payload": {"content": "no branch proof"},
    }

    assert session._event_visible_in_branch_locked(branchless_event, "main") is False
    assert session._event_visible_in_branch_locked(branchless_event, "branch-a") is False


def test_remote_session_run_recovery_tickets_are_branch_local(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-recovery",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    _record_session_branch(session, "branch-a", agent_run_id="agent-run-a")
    _record_session_branch(session, "branch-b", agent_run_id="agent-run-b")
    ticket_a = session.register_recovery(
        {
            "response": "branch A stopped",
            "branch_binding_id": "branch-a",
            "recovery_actions": ["continue"],
        }
    )
    ticket_b = session.register_recovery(
        {
            "response": "branch B stopped",
            "branch_binding_id": "branch-b",
            "recovery_actions": ["continue"],
        }
    )

    assert session.status_payload(0, branch_binding_id="branch-a")["recovery"][
        "recovery_id"
    ] == ticket_a["recovery_id"]
    assert session.status_payload(0, branch_binding_id="branch-b")["recovery"][
        "recovery_id"
    ] == ticket_b["recovery_id"]
    assert session.status_payload(0, branch_binding_id="main")["recovery"] is None

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.consume_recovery("continue")

    with pytest.raises(ValueError, match="recovery_not_available"):
        session.consume_recovery("continue", branch_binding_id="main")

    prompt, consumed = session.consume_recovery(
        "continue", branch_binding_id="branch-a"
    )

    assert "branch A stopped" in prompt
    assert consumed["recovery_id"] == ticket_a["recovery_id"]
    assert session.status_payload(0, branch_binding_id="branch-a")["recovery"][
        "state"
    ] == "consumed"
    assert session.status_payload(0, branch_binding_id="branch-b")["recovery"][
        "state"
    ] == "pending"


def test_remote_session_run_cancel_requests_are_branch_local(tmp_path: Path) -> None:
    session = _SessionRunProjection(
        session_run_id="run-branch-cancel",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
    )
    _record_session_branch(session, "branch-a", agent_run_id="agent-run-a")
    _record_session_branch(session, "branch-b", agent_run_id="agent-run-b")
    session.register_approval(
        "approval-a",
        {"tool_call_id": "tool-a", "branch_binding_id": "branch-a"},
    )
    session.register_approval(
        "approval-b",
        {"tool_call_id": "tool-b", "branch_binding_id": "branch-b"},
    )
    session.register_user_input(
        "input-a",
        {"kind": "clarification", "branch_binding_id": "branch-a"},
    )
    session.register_user_input(
        "input-b",
        {"kind": "clarification", "branch_binding_id": "branch-b"},
    )

    with pytest.raises(ValueError, match="branch_binding_id_required"):
        session.request_branch_cancel("cancel-missing-branch", "")

    first_a, approvals_a, inputs_a = session.request_branch_cancel(
        "cancel-a",
        "branch-a",
    )
    second_a, approvals_a_again, inputs_a_again = session.request_branch_cancel(
        "cancel-a-again",
        "branch-a",
    )
    first_b, approvals_b, inputs_b = session.request_branch_cancel(
        "cancel-b",
        "branch-b",
    )

    assert first_a is True
    assert second_a is False
    assert first_b is True
    assert [item["approval_id"] for item in approvals_a] == ["approval-a"]
    assert [item["input_id"] for item in inputs_a] == ["input-a"]
    assert approvals_a_again == []
    assert inputs_a_again == []
    assert [item["approval_id"] for item in approvals_b] == ["approval-b"]
    assert [item["input_id"] for item in inputs_b] == ["input-b"]
    assert session.status_payload(0, branch_binding_id="branch-a")["approvals"] == []
    assert session.status_payload(0, branch_binding_id="branch-a")["user_inputs"] == []
    assert session.status_payload(0, branch_binding_id="branch-b")["approvals"] == []
    assert session.status_payload(0, branch_binding_id="branch-b")["user_inputs"] == []


def test_remote_session_run_document_draft_preview_chunk_is_live_only(
    tmp_path: Path,
) -> None:
    persisted: list[dict] = []

    def sink(
        session_id: str,
        event_type: str,
        payload: dict,
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "type": event_type,
                "payload": payload,
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )
    session.enable_trace_persistence("session-1")

    _append_main_live_event(
        session,
        "document_draft_preview_chunk",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "chunk_seq": 1,
            "start_offset": 0,
            "end_offset": 1,
            "content": "A",
            "content_sha256": "sha-a",
        },
    )

    events, _done, _cursor = session.wait_events(0, 0, branch_binding_id="main")
    assert [event["type"] for event in events] == ["document_draft_preview_chunk"]
    assert events[0]["payload"]["content"] == "A"
    assert persisted == []
    assert session.events == []


def test_remote_session_run_document_draft_preview_chunk_keeps_large_body_without_artifact(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        max_payload_bytes=32,
    )
    content = "live preview body " * 16

    _append_main_live_event(
        session,
        "document_draft_preview_chunk",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "chunk_seq": 1,
            "start_offset": 0,
            "end_offset": len(content),
            "content": content,
            "content_sha256": "sha-a",
        },
    )

    events, _done, _cursor = session.wait_events(0, 0, branch_binding_id="main")

    assert [event["type"] for event in events] == ["document_draft_preview_chunk"]
    payload = events[0]["payload"]
    assert payload["content"] == content
    assert "artifact_ref" not in payload
    assert session.events == []


def test_remote_session_run_hydrates_large_document_draft_snapshot_for_stream(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
        max_payload_bytes=128,
    )
    content = "# Architecture\n\n" + ("large body\n" * 64)

    _append_main_event(
        session,
        "document_draft_snapshot",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content": content,
            "content_length": len(content),
            "content_sha256": "snapshot-sha",
            "snapshot_kind": "final",
            "final": True,
            "last_chunk_seq": 7,
            "status": "streaming",
        },
    )

    stored_payload = session.events[-1]["payload"]
    assert "artifact_ref" in stored_payload
    assert "content" not in stored_payload

    events, _done, _cursor = session.wait_events(0, 0, branch_binding_id="main")

    payload = events[-1]["payload"]
    assert payload["content"] == content
    assert payload["content_length"] == len(content)
    assert payload["content_sha256"] == "snapshot-sha"
    assert payload["snapshot_kind"] == "final"
    assert payload["final"] is True
    assert payload["last_chunk_seq"] == 7
    assert payload["draft_id"] == "draft-1"
    assert payload["target_path"] == "docs/a.md"
    assert "artifact_ref" in payload
    assert payload["artifact_ref"]["preview"] == ""


def test_remote_session_run_persists_document_draft_snapshot_trace_without_body(
    tmp_path: Path,
) -> None:
    persisted: list[dict] = []

    def sink(
        session_id: str,
        event_type: str,
        payload: dict,
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "type": event_type,
                "payload": payload,
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )
    session.enable_trace_persistence("session-1")
    content = "# Architecture\n\nTrace must not store this body."

    _append_main_event(
        session,
        "document_draft_snapshot",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content": content,
            "content_length": len(content),
            "content_sha256": "snapshot-sha",
            "snapshot_kind": "checkpoint",
            "last_chunk_seq": 3,
        },
    )

    assert len(persisted) == 1
    trace_payload = persisted[0]["payload"]
    assert "content" not in trace_payload
    assert trace_payload["content_length"] == len(content)
    assert trace_payload["content_sha256"] == "snapshot-sha"
    artifact = trace_payload["artifact_ref"]
    assert artifact["encoding"] == "json+gzip"
    assert artifact["preview"] == ""
    assert content not in json.dumps(trace_payload, ensure_ascii=False)
    with gzip.open(Path(artifact["path"]), "rt", encoding="utf-8") as fh:
        assert json.load(fh) == {"content": content}

    stored_payload = session.events[-1]["payload"]
    assert "content" not in stored_payload
    assert stored_payload["artifact_ref"] == artifact
    assert content not in json.dumps(stored_payload, ensure_ascii=False)

    events, _done, _cursor = session.wait_events(0, 0, branch_binding_id="main")
    assert events[-1]["payload"]["content"] == content
    assert events[-1]["payload"]["artifact_ref"] == artifact


def test_remote_session_run_flushes_pending_document_draft_snapshot_trace_without_body(
    tmp_path: Path,
) -> None:
    persisted: list[dict] = []

    def sink(
        session_id: str,
        event_type: str,
        payload: dict,
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "type": event_type,
                "payload": payload,
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )
    content = "# Architecture\n\nPending trace must not store this body."

    _append_main_event(
        session,
        "document_draft_snapshot",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content": content,
            "content_length": len(content),
            "content_sha256": "snapshot-sha",
            "snapshot_kind": "checkpoint",
            "last_chunk_seq": 3,
        },
    )
    assert persisted == []

    session.enable_trace_persistence("session-1")

    assert len(persisted) == 1
    trace_payload = persisted[0]["payload"]
    assert "content" not in trace_payload
    assert trace_payload["content_length"] == len(content)
    artifact = trace_payload["artifact_ref"]
    assert artifact["preview"] == ""
    assert content not in json.dumps(trace_payload, ensure_ascii=False)
    with gzip.open(Path(artifact["path"]), "rt", encoding="utf-8") as fh:
        assert json.load(fh) == {"content": content}


def test_remote_session_run_status_does_not_hydrate_large_document_draft_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        agent_run_id="agent-run-main",
        branch_binding_id="main",
        artifact_root=tmp_path,
        max_payload_bytes=128,
    )
    content = "# Architecture\n\n" + ("large body\n" * 64)
    _append_main_event(
        session,
        "document_draft_snapshot",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content": content,
            "content_length": len(content),
            "content_sha256": "snapshot-sha",
            "snapshot_kind": "final",
            "final": True,
            "last_chunk_seq": 7,
            "status": "streaming",
        },
    )
    assert "artifact_ref" in session.events[-1]["payload"]

    def fail_hydration(_artifact: dict) -> None:
        raise AssertionError("status_payload must not hydrate artifact payloads")

    monkeypatch.setattr(
        "labrastro_server.interfaces.http.remote.service._read_event_artifact_payload",
        fail_hydration,
    )

    status = session.status_payload(0, branch_binding_id="main")

    assert status["next_cursor"] == 1
    assert status["latest_seq"] == 1


def test_remote_session_run_interleaves_live_and_durable_events_without_false_loss(
    tmp_path: Path,
) -> None:
    session = _SessionRunProjection(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    _append_main_live_event(
        session,
        "document_draft_preview_chunk",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "chunk_seq": 1,
            "start_offset": 0,
            "end_offset": 1,
            "content": "A",
            "content_sha256": "sha-a",
        },
    )
    _append_main_event(
        session,
        "document_draft_progress",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content_length": 1,
            "content_sha256": "sha-a",
            "last_chunk_seq": 1,
        },
    )

    events, _done, cursor = session.wait_events(0, 0, branch_binding_id="main")

    assert [event["type"] for event in events] == [
        "document_draft_preview_chunk",
        "document_draft_progress",
    ]
    assert [event["seq"] for event in events] == [1, 2]
    assert cursor == 2


def _raw_request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def _text_request(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = request.Request(url, headers=headers or {}, method="GET")
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _bytes_request(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = request.Request(url, headers=headers or {}, method="GET")
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, resp.read()


def _build_go_agent_binary() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "reuleauxcoder-agent"
    target_dir = Path(tempfile.mkdtemp(prefix="rc-go-agent-bin-"))
    binary_path = target_dir / "reuleauxcoder-agent"
    subprocess.run(
        ["go", "build", "-o", str(binary_path), "./cmd/reuleauxcoder-agent"],
        cwd=agent_dir,
        check=True,
        timeout=120,
    )
    return binary_path


def _cleanup_provider_build_dir(provider: object) -> None:
    build_dir = getattr(provider, "_build_dir", None)
    if isinstance(build_dir, Path):
        shutil.rmtree(build_dir, ignore_errors=True)


def _fake_gh_env(tmp_path: Path, *, pr_url: str = "https://example.test/pr/fake") -> tuple[dict[str, str], Path]:
    gh_dir = tmp_path / "fake-gh"
    gh_dir.mkdir()
    log_path = gh_dir / "gh.log"
    if os.name == "nt":
        gh_path = gh_dir / "gh.bat"
        gh_path.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"pr\" if \"%2\"==\"view\" goto view\r\n"
            "if \"%1\"==\"pr\" if \"%2\"==\"create\" goto create\r\n"
            "exit /b 2\r\n"
            ":view\r\n"
            "echo pr view>>\"%LABRASTRO_FAKE_GH_LOG%\"\r\n"
            "exit /b 1\r\n"
            ":create\r\n"
            "echo pr create>>\"%LABRASTRO_FAKE_GH_LOG%\"\r\n"
            "echo %LABRASTRO_FAKE_GH_CREATE_URL%\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        gh_path = gh_dir / "gh"
        gh_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = pr ] && [ \"$2\" = view ]; then echo pr view >> \"$LABRASTRO_FAKE_GH_LOG\"; exit 1; fi\n"
            "if [ \"$1\" = pr ] && [ \"$2\" = create ]; then echo pr create >> \"$LABRASTRO_FAKE_GH_LOG\"; echo \"$LABRASTRO_FAKE_GH_CREATE_URL\"; exit 0; fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        gh_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(gh_dir) + os.pathsep + env.get("PATH", "")
    env["LABRASTRO_FAKE_GH_LOG"] = str(log_path)
    env["LABRASTRO_FAKE_GH_CREATE_URL"] = pr_url
    return env, log_path


class TestRemoteRelayHTTPService:
    def _start_bound_agent_run_session(self):
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        _, register_body = _json_request(
            "POST",
            f"{service.base_url}/remote/register",
            _peer_register_payload(
                relay,
                features=["agent_runs", "agent_runs.local_workspace"],
            ),
        )
        _, start_body = _json_request(
            "POST",
            f"{service.base_url}/remote/session-runs/start",
            {
                "peer_token": register_body["payload"]["peer_token"],
                "prompt": "first",
                "session_hint": "chat-session-steer",
                "client_request_id": "start-steer-1",
            },
        )
        return relay, service, control, register_body, start_body

    def _mark_claim_running(
        self,
        control: AgentRunControlPlane,
        peer_id: str,
        claim: dict,
    ) -> None:
        ok, reason = control.append_executor_event(
            claim["agent_run"]["id"],
            ExecutorEvent.status("running"),
            request_id=claim["request_id"],
            activation_id=claim["activation_id"],
            worker_id=claim["worker_id"],
            peer_id=peer_id,
        )
        assert ok, reason

    def _claim_bound_agent_run_server_worker(
        self,
        control: AgentRunControlPlane,
    ) -> dict:
        claim = control.claim_agent_run_activation(
            worker_id="server-worker-1",
            worker_kind=WorkerKind.SERVER_WORKER,
            executors=["reuleauxcoder", "codex", "fake"],
            peer_features=[
                "agent_runs",
                "worker_kind:server_worker",
                "agent_runs.remote_server",
            ],
        )
        assert claim is not None
        return claim.to_dict()

    def test_local_action_claim_complete_uses_lease_and_agent_run_events(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(
                    relay,
                    workspace_root=r"D:\AboutDEV\vika_mcp",
                    features=["local_actions", "local_action:read_workspace_file"],
                ),
            )
            peer = register_body["payload"]
            task = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="needs local file",
                    owner_session_run_id="session-run-local-action",
                    source=AgentRunSource.CHAT,
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-local-action-http",
            )
            service.local_action_service.create_local_action(
                LocalActionRecord.from_dict(
                    {
                        "scope": "activation_scoped",
                        "local_action_id": "local-action-http-1",
                        "agent_run_id": task.id,
                        "activation_id": "activation-local-action-1",
                        "session_run_id": "session-run-local-action",
                        "branch_binding_id": "main",
                        "action_kind": "read_workspace_file",
                        "status": "waiting_peer",
                        "workspace_root": r"D:\AboutDEV\vika_mcp",
                        "payload": {"path": "README.md"},
                    }
                )
            )

            status, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/local-actions/claim",
                {
                    "peer_token": peer["peer_token"],
                    "peer_id": peer["peer_id"],
                    "worker_kind": "local_peer",
                    "features": ["local_actions", "local_action:read_workspace_file"],
                    "workspace_root": r"D:\AboutDEV\vika_mcp",
                    "max_actions": 1,
                },
            )

            assert status == 200
            claimed = claim_body["actions"][0]
            assert claimed["local_action_id"] == "local-action-http-1"
            assert claimed["lease_id"]

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/local-actions/complete",
                    {
                        "peer_token": peer["peer_token"],
                        "local_action_id": "local-action-http-1",
                        "lease_id": "wrong-lease",
                        "status": "completed",
                        "result": {"summary": "read 120 lines"},
                    },
                )
            assert excinfo.value.code == 409

            status, complete_body = _json_request(
                "POST",
                f"{service.base_url}/remote/local-actions/complete",
                {
                    "peer_token": peer["peer_token"],
                    "local_action_id": "local-action-http-1",
                    "lease_id": claimed["lease_id"],
                    "status": "completed",
                    "result": {"summary": "read 120 lines"},
                },
            )

            assert status == 200
            assert complete_body["ok"] is True
            assert complete_body["action"]["status"] == "completed"
            event_types = [event.type for event in control.list_events(task.id)]
            assert "local_action_requested" in event_types
            assert "local_action_waiting_peer" in event_types
            assert "local_action_started" in event_types
            assert "local_action_completed" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_admin_provider_and_model_endpoints_require_login_and_mask_keys(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        reloads: list[str] = []
        config_path = tmp_path / "config.yaml"
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: reloads.append("reload"),
            admin_provider_test_handler=lambda provider, model, prompt: {
                "ok": True,
                "provider_id": provider.id,
                "model": model,
                "prompt": prompt,
            },
            admin_provider_models_handler=lambda provider: {
                "ok": True,
                "provider_id": provider.id,
                "unsupported": False,
                "models": [
                    {"id": "deepseek-chat", "owned_by": "deepseek"},
                    {"id": "deepseek-reasoner", "owned_by": "deepseek"},
                ],
            },
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST", f"{service.base_url}/remote/admin/providers/list", {}
                )
                raise AssertionError("admin endpoint should require login")
            except HTTPError as exc:
                assert exc.code == 401

            admin_headers = TEST_ADMIN_HEADERS
            status, record = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/record",
                {
                    "provider_id": "deepseek",
                    "type": "openai_chat",
                    "compat": "deepseek",
                    "api_key": "sk-secret-value",
                    "base_url": "https://api.deepseek.com",
                },
                headers=admin_headers,
            )
            assert status == 200
            assert record["ok"] is True
            assert record["provider"]["api_key_hint"] == "sk-s...alue"
            assert "api_key" not in record["provider"]

            _, update = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/record",
                {
                    "provider_id": "deepseek",
                    "type": "openai_chat",
                    "compat": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                },
                headers=admin_headers,
            )
            assert update["created"] is False

            _, providers = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/list",
                {},
                headers=admin_headers,
            )
            assert providers["providers"][0]["api_key_hint"] == "sk-s...alue"
            assert "api_key" not in providers["providers"][0]
            assert providers["providers"][0]["enabled"] is True

            _, model_list = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/models",
                {"provider_id": "deepseek"},
                headers=admin_headers,
            )
            assert model_list["models"][0]["id"] == "deepseek-chat"

            _, test_result = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/test",
                {"provider_id": "deepseek", "model": "deepseek-chat", "prompt": "ping"},
                headers=admin_headers,
            )
            assert test_result == {
                "ok": True,
                "provider_id": "deepseek",
                "model": "deepseek-chat",
                "prompt": "ping",
            }

            _, profile = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/record",
                {
                    "profile_id": "deepseek-main",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "max_tokens": 4096,
                    "max_context_tokens": 128000,
                    "temperature": 0,
                    "thinking_enabled": True,
                },
                headers=admin_headers,
            )
            assert profile["model_profile"]["provider"] == "deepseek"
            assert "api_key" not in profile["model_profile"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/providers/delete",
                    {"provider_id": "deepseek"},
                    headers=admin_headers,
                )
                raise AssertionError("delete should be blocked while profiles reference provider")
            except HTTPError as exc:
                assert exc.code == 409
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "provider_in_use"
                assert body["details"]["blockers"][0]["profile_id"] == "deepseek-main"

            _, active = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/activate",
                {"profile_id": "deepseek-main", "target": "both"},
                headers=admin_headers,
            )
            assert active["active_main"] == "deepseek-main"
            assert active["active_sub"] == "deepseek-main"

            _, disabled = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/enable",
                {"provider_id": "deepseek", "enabled": False},
                headers=admin_headers,
            )
            assert disabled["provider"]["enabled"] is False
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/models/activate",
                    {"profile_id": "deepseek-main", "target": "main"},
                    headers=admin_headers,
                )
                raise AssertionError("disabled provider should block activation")
            except HTTPError as exc:
                assert exc.code == 409
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "provider_disabled"

            _, copied = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/copy",
                {"provider_id": "deepseek", "target_id": "deepseek-copy"},
                headers=admin_headers,
            )
            assert copied["provider"]["id"] == "deepseek-copy"
            assert copied["provider"]["enabled"] is True
            assert copied["provider"]["api_key_hint"] == "sk-s...alue"

            _, models = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/list",
                {},
                headers=admin_headers,
            )
            assert models["active_main"] == "deepseek-main"
            assert models["model_profiles"][0]["id"] == "deepseek-main"
            _, deleted = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/delete",
                {"provider_id": "deepseek-copy"},
                headers=admin_headers,
            )
            assert deleted["ok"] is True
            assert deleted["provider_id"] == "deepseek-copy"
            assert "config_etag" in deleted
            assert len(reloads) == 8
            raw = config_path.read_text(encoding="utf-8")
            assert "sk-secret-value" in raw
            assert "active_main: deepseek-main" in raw
            assert "deepseek-copy" not in raw
        finally:
            service.stop()
            relay.stop()

    def test_session_run_branch_select_switches_selected_projection_without_stopping_runs(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            source_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch prompt",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-branch-select",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-select-1",
                selected=False,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=source_agent_run_id,
                target_agent_run_id=branch.id,
            )

            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/branches/select",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-select-1",
                    "cursor": 0,
                },
            )

            assert status == 200
            assert body["session_run_id"] == session_run_id
            assert body["agent_run_id"] == branch.id
            assert body["branch_binding_id"] == "branch-select-1"
            branches = {item["branch_binding_id"]: item for item in body["branches"]}
            assert branches["main"]["selected"] is False
            assert branches["branch-select-1"]["selected"] is True
            active_session = service._get_session_run(session_run_id)
            assert active_session is not None
            assert active_session.agent_run_id == branch.id
            assert active_session.branch_binding_id == "branch-select-1"
            assert active_session.runtime_state["agent_run_id"] == branch.id
            assert active_session.runtime_state["branch_binding_id"] == "branch-select-1"
            assert active_session.runtime_state["scope_id"] == (
                f"{session_run_id}:branch-select-1"
            )
            assert control.agent_run_to_dict(source_agent_run_id)["status"] == "queued"
            assert control.agent_run_to_dict(branch.id)["status"] == "queued"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_branch_select_reports_binding_store_unavailable_when_selection_write_fails(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            session_run_id = start_body["session_run_id"]

            def fail_selection_write(*_args, **_kwargs):
                raise RuntimeError("binding store unavailable")

            control.select_session_run_branch = fail_selection_write  # type: ignore[method-assign]

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/branches/select",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "cursor": 0,
                    },
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 503
            assert body["error"] == "session_run_binding_store_unavailable"
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            (
                "/remote/admin/agent-runs/branch",
                {
                    "source_agent_run_id": "agent-source",
                    "base_session_item_id": "message-1",
                    "runtime_root": "D:/repo",
                    "prompt": "branch",
                },
            ),
            (
                "/remote/admin/agent-runs/fork",
                {
                    "source_agent_run_id": "agent-source",
                    "base_session_item_id": "message-1",
                    "fork_workspace_ref": "workspace-ref",
                    "target_owner_session_run_id": "run-target",
                    "prompt": "fork",
                },
            ),
        ],
    )
    def test_admin_agent_run_branching_requires_target_branch_binding_id(
        self,
        path: str,
        payload: dict[str, object],
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=AgentRunControlPlane(),
        )
        service.start()
        try:
            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}{path}",
                    payload,
                    headers=TEST_ADMIN_HEADERS,
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "branch_binding_id_required"
        finally:
            service.stop()
            relay.stop()

    def test_admin_agent_run_branch_response_keeps_target_branch_proof_when_projection_is_empty(
        self,
    ) -> None:
        class FakeControlPlane:
            def get_agent_run(self, _agent_run_id: str) -> SimpleNamespace:
                return SimpleNamespace(owner_session_run_id="")

            def branch_agent_run(self, **kwargs: object) -> SimpleNamespace:
                assert kwargs["branch_binding_id"] == "branch-response-1"
                return SimpleNamespace(id="branch-run-1")

            def agent_run_to_dict(self, agent_run_id: str) -> dict[str, object]:
                return {"id": agent_run_id}

        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=FakeControlPlane(),
        )
        service.start()
        try:
            with patch(
                "labrastro_server.interfaces.http.remote.routes.admin._record_agent_run_branch_projection",
                return_value="",
            ):
                status, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/agent-runs/branch",
                    {
                        "source_agent_run_id": "agent-source",
                        "base_session_item_id": "message-1",
                        "runtime_root": "D:/repo",
                        "branch_binding_id": "branch-response-1",
                        "prompt": "branch",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )

            assert status == 200
            assert body["branch_binding_id"] == "branch-response-1"
            assert body["agent_run"]["id"] == "branch-run-1"
        finally:
            service.stop()
            relay.stop()

    def test_admin_agent_run_branch_uses_session_branch_projection_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()

        class FakePlan:
            def __init__(self, runtime_root: Path, branch_name: str) -> None:
                self.runtime_root = runtime_root
                self.branch_name = branch_name
                self.worktree_path = runtime_root / "worktrees" / branch_name.replace("/", "-")

        class FakePrepared:
            def __init__(self, plan: FakePlan, source_repo: Path) -> None:
                self.runtime_root = plan.runtime_root
                self.source_repo = source_repo
                self.branch_name = plan.branch_name
                self.base_git_ref = "base-commit"
                self.base_tree_ref = "base-tree"
                self.branch_git_ref = f"refs/heads/{plan.branch_name}"
                self.branch_worktree_ref = str(plan.worktree_path)
                self.worktree_path = plan.worktree_path

        class FakeWorktreeManager:
            def __init__(self, runtime_root: str | Path) -> None:
                self.runtime_root = Path(runtime_root)

            def plan(
                self,
                *,
                workspace_id: str,
                task_id: str,
                agent_id: str,
                repo_url: str | None = None,
                branch_name: str | None = None,
            ) -> FakePlan:
                del workspace_id, task_id, agent_id, repo_url
                return FakePlan(self.runtime_root, branch_name or "agent/chat/branch-run")

            def create_branch_worktree(
                self,
                *,
                source_repo: str | Path,
                plan: FakePlan,
                base_ref: str = "HEAD",
            ) -> FakePrepared:
                del base_ref
                plan.worktree_path.mkdir(parents=True, exist_ok=True)
                return FakePrepared(plan, Path(source_repo))

            def cleanup_branch_worktree(self, **_kwargs: object) -> None:
                return None

        try:
            session = service._create_session_run(
                "peer-1",
                "chat-session-branch",
                agent_run_id="source-run",
                branch_binding_id="main",
            )
            source_repo = tmp_path / "source-repo"
            source_repo.mkdir()
            source = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="base",
                    owner_session_run_id=session.session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                    workdir=str(source_repo),
                ),
                task_id="source-run",
            )
            binding = control.create_session_run_binding(
                session_run_id=session.session_run_id,
                session_id=session.session_id or "chat-session-branch",
                peer_id="peer-1",
                agent_run_id=source.id,
                branch_binding_id="main",
                selected=True,
                target_agent_run_id=source.id,
                metadata={"binding_kind": "mainline"},
            )
            session.agent_run_id = source.id
            session.record_branch_binding(binding)
            session.append_event(
                "user_message",
                {
                    "session_item_id": "msg-1",
                    "branch_binding_id": "main",
                    "content": "original question",
                },
            )
            session.append_event(
                "assistant_message",
                {
                    "session_item_id": "msg-2",
                    "branch_binding_id": "main",
                    "content": "source branch answer after edit point",
                },
            )

            with patch(
                "labrastro_server.services.agent_runtime.control_plane.WorktreeManager",
                FakeWorktreeManager,
            ):
                status, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/agent-runs/branch",
                    {
                        "source_agent_run_id": source.id,
                        "base_session_item_id": "msg-1",
                        "runtime_root": str(tmp_path / "runtime"),
                        "repo_root": str(source_repo),
                        "agent_run_id": "branch-run",
                        "branch_binding_id": "branch-edit-1",
                        "prompt": "edited question",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )

            assert status == 200
            assert body["branch_binding_id"] == "branch-edit-1"
            detail = control.load_agent_run_detail(body["agent_run"]["id"])
            assert detail["activations"][-1]["prompt"] == (
                "User: original question\n\nUser: edited question"
            )
            assert (
                "source branch answer after edit point"
                not in detail["activations"][-1]["prompt"]
            )
            events, _done, _cursor = session.wait_events(
                0,
                0,
                branch_binding_id="branch-edit-1",
            )
            assert [event["payload"].get("content") for event in events] == [
                "original question",
                "edited question",
            ]
            assert all(
                event["payload"].get("content")
                != "source branch answer after edit point"
                for event in events
            )
            selected_binding = control.find_session_run_binding(
                session_run_id=session.session_run_id
            )
            assert selected_binding is not None
            assert selected_binding.branch_binding_id == "branch-edit-1"
        finally:
            service.stop()
            relay.stop()

    def test_admin_status_returns_chat_modes(self) -> None:
        config_data = {
            "modes": {
                "active": "planner",
                "profiles": {
                    "coder": {"description": "Code changes"},
                    "planner": {
                        "description": "Plan first",
                        "tools": ["read_file"],
                    },
                },
            }
        }
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=Path("unused-config.yaml"),
        )
        service.start()
        try:
            with patch.object(RemoteAdminConfigManager, "_load_data", return_value=config_data):
                status, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/status",
                    {},
                    headers=TEST_ADMIN_HEADERS,
                )

            assert status == 200
            assert body["active_mode"] == "planner"
            modes = {mode["name"]: mode for mode in body["modes"]}
            assert set(modes) >= {"coder", "planner", "debugger", "taskflow"}
            assert modes["coder"]["description"] == "Code changes"
            assert modes["planner"]["description"] == "Plan first"
            assert modes["planner"]["tools"] == ["read_file"]
        finally:
            service.stop()
            relay.stop()

    def test_admin_chat_config_read_returns_lightweight_chat_selection_state(
        self,
    ) -> None:
        config_data = {
            "providers": {
                "items": {
                    "deepseek": {
                        "type": "openai_chat",
                        "api_key": "sk-ds",
                        "models": [{"id": "deepseek-chat"}],
                    }
                }
            },
            "models": {
                "active_main": "deepseek-main",
                "profiles": {
                    "deepseek-main": {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "max_tokens": 4096,
                        "max_context_tokens": 128000,
                    }
                },
            },
            "modes": {
                "active": "planner",
                "profiles": {
                    "planner": {
                        "description": "Plan first",
                        "tools": ["read_file"],
                    },
                },
            },
            "agent_registry": {
                "agents": {
                    "planner": {
                        "model": {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                        }
                    }
                }
            },
        }
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=Path("unused-config.yaml"),
        )
        service.start()
        try:
            with patch.object(RemoteAdminConfigManager, "_load_data", return_value=config_data):
                status, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/chat-config/read",
                    {},
                    headers=TEST_ADMIN_HEADERS,
                )

            assert status == 200
            assert body["ok"] is True
            assert body["active_mode"] == "planner"
            assert body["active_main"] == "deepseek-main"
            assert body["active_agent_model"] == {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "parameters": {},
            }
            assert body["model_profiles"][0]["id"] == "deepseek-main"
            assert "server_settings" not in body
            assert "provider_model_catalog" not in body
            assert "model_capabilities" not in body
            assert "agent_runs" not in body
            assert "github" not in body
        finally:
            service.stop()
            relay.stop()

    def test_admin_status_falls_back_to_builtin_chat_modes(self) -> None:
        manager = RemoteAdminConfigManager(Path("unused-config.yaml"))
        with patch.object(manager, "_load_data", return_value={}):
            status = manager.status()

        assert status["active_mode"] == "coder"
        assert {mode["name"] for mode in status["modes"]} >= {
            "coder",
            "planner",
            "debugger",
        }

    def test_admin_manager_accepts_string_config_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(config_path, {"providers": {"items": {}}})

        manager = RemoteAdminConfigManager(str(config_path))

        assert manager.model_capabilities_status()["model_capabilities"]["enabled"] is True

    def test_admin_record_provider_writes_reloadable_stream_recovery(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "remote_exec": {"enabled": True, "host_mode": True},
                "auth": {
                    "enabled": True,
                    "token_secret": "test-secret",
                    "superadmins": [
                        {"username": "admin", "password": "test-password"}
                    ],
                },
                "providers": {"items": {}},
            },
        )
        manager = RemoteAdminConfigManager(config_path, reload_handler=lambda: None)

        result = manager.record_provider(
            {
                "provider_id": "zenmux",
                "type": "openai_chat",
                "compat": "zenmux",
                "api_key": "sk-test",
                "base_url": "https://gateway.example/v1",
            }
        )

        assert result.ok is True
        loaded = ConfigLoader.from_path(config_path)
        provider = loaded.providers.items["zenmux"]
        assert provider.stream_recovery.enabled is True
        assert loaded.validate() == []

    def test_admin_reload_failure_logs_original_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(config_path, {"providers": {"items": {}}})
        manager = RemoteAdminConfigManager(
            config_path,
            reload_handler=lambda: (_ for _ in ()).throw(
                RuntimeError("reload exploded")
            ),
        )

        with caplog.at_level(logging.ERROR):
            result = manager.record_provider(
                {
                    "provider_id": "broken",
                    "type": "openai_chat",
                    "api_key": "sk-test",
                    "base_url": "https://broken.invalid/v1",
                }
            )

        assert result.ok is False
        assert result.status == 500
        assert result.payload == {
            "error": "config_reload_failed",
            "message": "reload exploded",
        }
        assert str(config_path) in caplog.text
        assert "RuntimeError" in caplog.text
        assert "reload exploded" in caplog.text

    def test_admin_record_model_profile_rejects_profile_credentials(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        manager = RemoteAdminConfigManager(config_path)

        result = manager.record_model_profile(
            {
                "profile_id": "deepseek-main",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "sk-old",
                "base_url": "https://api.deepseek.com",
                "max_tokens": 8192,
                "max_context_tokens": 128000,
                "capability_user_configured": True,
            }
        )

        assert result.ok is False
        assert result.status == 400
        assert result.payload["error"] == "unknown_model_profile_field"
        assert result.payload["fields"] == ["api_key", "base_url"]

    def test_admin_record_model_profile_requires_capability_or_explicit_limits(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "custom": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        manager = RemoteAdminConfigManager(config_path)

        result = manager.record_model_profile(
            {
                "profile_id": "custom-main",
                "provider": "custom",
                "model": "custom-model",
            }
        )

        assert result.ok is False
        assert result.status == 400
        assert result.payload["error"] == "model_capability_required"
        assert result.payload["provider_id"] == "custom"
        assert result.payload["model"] == "custom-model"

    def test_admin_record_model_profile_uses_deepseek_v4_capability_defaults(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "compat": "deepseek",
                            "api_key": "sk-test",
                            "base_url": "https://api.deepseek.com",
                        }
                    }
                }
            },
        )
        manager = RemoteAdminConfigManager(
            config_path,
            reload_handler=lambda: None,
        )

        result = manager.record_model_profile(
            {
                "profile_id": "deepseek-v4-pro-main",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "max_tokens": 4096,
                "max_context_tokens": 128000,
                "capability_user_configured": False,
            }
        )

        assert result.ok is True
        profile = result.payload["model_profile"]
        assert profile["max_tokens"] == 384000
        assert profile["max_context_tokens"] == 1000000
        assert profile["capability_user_configured"] is False

    def test_admin_record_model_profile_writes_reloadable_capability_metadata(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "zenmux": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                            "base_url": "https://zenmux.ai/api/v1",
                        }
                    }
                }
            },
        )
        manager = RemoteAdminConfigManager(
            config_path,
            reload_handler=lambda: ConfigLoader.from_path(config_path),
        )

        result = manager.record_model_profile(
            {
                "profile_id": "Zenmux-anthropic-claude-opus-4.6",
                "provider": "zenmux",
                "model": "anthropic/claude-opus-4.6",
                "max_tokens": 32000,
                "max_context_tokens": 200000,
                "capability_user_configured": True,
            }
        )

        assert result.ok is True
        loaded = ConfigLoader.from_path(config_path)
        profile = loaded.model_profiles["Zenmux-anthropic-claude-opus-4.6"]
        assert profile.capability_user_configured is True
        assert profile.capability_source is None

    def test_admin_delete_model_profile_preserves_provider_models_and_repairs_active(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "zenmux": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                            "models": [
                                {"id": "anthropic/claude-opus-4.6"},
                                {"id": "anthropic/claude-sonnet-4.5"},
                            ],
                        }
                    }
                },
                "models": {
                    "active_main": "opus",
                    "active_sub": "opus",
                    "profiles": {
                        "opus": {
                            "provider": "zenmux",
                            "model": "anthropic/claude-opus-4.6",
                            "max_tokens": 32000,
                            "max_context_tokens": 200000,
                        },
                        "sonnet": {
                            "provider": "zenmux",
                            "model": "anthropic/claude-sonnet-4.5",
                            "max_tokens": 64000,
                            "max_context_tokens": 200000,
                        },
                    },
                },
            },
        )
        manager = RemoteAdminConfigManager(
            config_path,
            reload_handler=lambda: ConfigLoader.from_path(config_path),
        )

        result = manager.delete_model_profile({"profile_id": "opus"})

        assert result.ok is True
        assert result.payload["deleted"] is True
        assert result.payload["profile_id"] == "opus"
        assert result.payload["active_main"] == "sonnet"
        assert result.payload["active_sub"] == "sonnet"
        assert [profile["id"] for profile in result.payload["model_profiles"]] == ["sonnet"]
        raw = load_yaml_config(config_path)
        assert raw["providers"]["items"]["zenmux"]["models"] == [
            {"id": "anthropic/claude-opus-4.6"},
            {"id": "anthropic/claude-sonnet-4.5"},
        ]
        loaded = ConfigLoader.from_path(config_path)
        assert set(loaded.model_profiles) == {"sonnet"}
        assert loaded.active_main_model_profile == "sonnet"
        assert loaded.active_sub_model_profile == "sonnet"

    def test_admin_delete_model_profile_rejects_missing_profile(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "models": {
                    "profiles": {
                        "main": {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "max_tokens": 4096,
                            "max_context_tokens": 128000,
                        }
                    }
                }
            },
        )
        manager = RemoteAdminConfigManager(config_path)

        result = manager.delete_model_profile({"profile_id": "missing"})

        assert result.ok is False
        assert result.status == 404
        assert result.payload == {
            "error": "profile_not_found",
            "profile_id": "missing",
        }

    def test_admin_delete_model_profile_http_endpoint(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                            "models": [{"id": "deepseek-chat"}],
                        }
                    }
                },
                "models": {
                    "active_main": "main",
                    "active_sub": "main",
                    "profiles": {
                        "main": {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "max_tokens": 4096,
                            "max_context_tokens": 128000,
                        }
                    },
                },
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/delete",
                {"profile_id": "main"},
                headers=TEST_ADMIN_HEADERS,
            )

            assert status == 200
            assert body["ok"] is True
            assert body["deleted"] is True
            assert body["profile_id"] == "main"
            assert body["model_profiles"] == []
            assert body["active_main"] is None
            assert body["active_sub"] is None
            raw = load_yaml_config(config_path)
            assert raw["providers"]["items"]["deepseek"]["models"] == [
                {"id": "deepseek-chat"}
            ]
            assert raw["models"]["profiles"] == {}
        finally:
            service.stop()
            relay.stop()

    def test_admin_model_capabilities_list_and_apply_profile_recommendation(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "compat": "deepseek",
                            "api_key": "sk-test",
                            "base_url": "https://api.deepseek.com",
                        }
                    }
                },
                "models": {
                    "profiles": {
                        "deepseek-v4-pro-main": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                            "max_tokens": 4096,
                            "max_context_tokens": 128000,
                        }
                    }
                },
            },
        )
        manager = RemoteAdminConfigManager(
            config_path,
            reload_handler=lambda: None,
        )

        listed = manager.list_model_capabilities(
            {"provider": "deepseek", "model": "deepseek-v4-pro"}
        )
        apply_result = manager.apply_model_capability_recommendation(
            {"profile_id": "deepseek-v4-pro-main"}
        )

        assert listed["model_capabilities"]["models"][0]["max_output_tokens"] == 384000
        assert apply_result.ok is True
        profile = apply_result.payload["model_profiles"][0]
        assert profile["max_tokens"] == 384000
        assert profile["max_context_tokens"] == 1000000
        assert "capability_recommendation" not in profile

    def test_admin_model_capabilities_http_endpoints(self, tmp_path: Path) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "compat": "deepseek",
                            "api_key": "sk-test",
                            "base_url": "https://api.deepseek.com",
                        }
                    }
                },
                "models": {
                    "profiles": {
                        "deepseek-v4-pro-main": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                            "max_tokens": 4096,
                            "max_context_tokens": 128000,
                        }
                    }
                },
            },
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: None,
        )
        service.start()
        try:
            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/model-capabilities/status",
                {},
                TEST_ADMIN_HEADERS,
            )
            assert status == 200
            assert body["model_capabilities"]["enabled"] is True

            status, listed = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/model-capabilities/list",
                {"provider": "deepseek", "model": "deepseek-v4-pro"},
                TEST_ADMIN_HEADERS,
            )
            assert status == 200
            assert listed["model_capabilities"]["models"][0]["max_output_tokens"] == 384000

            status, applied = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/model-capabilities/apply",
                {"profile_id": "deepseek-v4-pro-main"},
                TEST_ADMIN_HEADERS,
            )
            assert status == 200
            assert applied["model_profiles"][0]["max_tokens"] == 384000
        finally:
            service.stop()
            relay.stop()

    def test_admin_environment_and_mcp_endpoints_manage_manifest(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        reloads: list[str] = []
        config_path = tmp_path / "config.yaml"
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: reloads.append("reload"),
        )
        service.start()
        try:
            admin_headers = TEST_ADMIN_HEADERS

            _, executable = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment-requirements/record",
                {
                    "environment_requirement": {
                        "id": "envreq:executable:gitnexus",
                        "kind": "executable",
                        "name": "gitnexus",
                        "command": "gitnexus",
                        "placement": "both",
                        "tags": ["repo_index"],
                        "requirements": {"node": ">=20"},
                        "check": "gitnexus --version",
                        "install": "npm install -g gitnexus",
                        "repo_url": "https://example.test/gitnexus/repo",
                        "docs": [{"title": "GitNexus", "url": "https://example.test/gitnexus"}],
                        "evidence": [
                            {
                                "field": "install",
                                "title": "GitNexus install",
                                "url": "https://example.test/gitnexus",
                                "excerpt": "Install with npm.",
                            }
                        ],
                        "install_prompt": "Use npm.",
                        "verify_prompt": "Run version check.",
                        "notes": ["PATH changes need approval."],
                        "credentials": ["GITNEXUS_TOKEN"],
                        "risk_level": "medium",
                    },
                },
                headers=admin_headers,
            )
            assert executable["environment_requirement"]["name"] == "gitnexus"
            assert executable["environment_requirement"]["kind"] == "executable"
            assert executable["environment_requirement"]["placement"] == "both"
            assert executable["environment_requirement"]["docs"][0]["title"] == "GitNexus"
            assert executable["environment_requirement"]["evidence"][0]["field"] == "install"

            _, mcp = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/mcp-servers/record",
                {
                    "mcp_server": {
                        "name": "gitnexus-mcp",
                        "command": "gitnexus",
                        "args": ["mcp"],
                        "placement": "peer",
                        "distribution": "command",
                        "environment_requirement_refs": ["envreq:runtime:node"],
                        "check": "gitnexus --version",
                        "install_prompt": "Start MCP with args.",
                    },
                },
                headers=admin_headers,
            )
            assert mcp["mcp_server"]["args"] == ["mcp"]
            assert mcp["mcp_server"]["environment_requirement_refs"] == [
                "envreq:runtime:node"
            ]

            _, skill = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/skills/record",
                {
                    "skill": {
                        "name": "code-review",
                        "path_hint": "~/.agents/skills/code-review/SKILL.md",
                        "source_path": "~/.agents/skills/code-review",
                        "description": "Review code changes.",
                        "docs": [
                            {
                                "title": "Skill docs",
                                "url": "https://example.test/skills/code-review",
                            }
                        ],
                        "evidence": [
                            {
                                "field": "path_hint",
                                "excerpt": "SKILL.md exists.",
                            }
                        ],
                        "install_prompt": "Install the code-review skill.",
                        "verify_prompt": "Open SKILL.md.",
                    },
                },
                headers=admin_headers,
            )
            assert skill["skill"]["name"] == "code-review"
            assert skill["skill"]["path_hint"] == "~/.agents/skills/code-review/SKILL.md"
            assert skill["skill"]["managed_by"] == "user"
            skill_record_events = [
                event
                for event in service.auth_service.store.audit_events
                if event["payload"]["path"] == "/remote/admin/skills/record"
            ]
            assert skill_record_events[-1]["payload"]["target"] == "code-review"

            _, skill_path = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment-requirements/record",
                {
                    "environment_requirement": {
                        "id": "envreq:path:collaborating-with-claude-skill",
                        "kind": "path",
                        "name": "collaborating-with-claude-skill",
                        "scope": "user",
                        "check": "Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                        "install": "python install-skill.py",
                        "path": "~/.agents/skills/collaborating-with-claude/SKILL.md",
                        "verify_prompt": "Check SKILL.md.",
                    },
                },
                headers=admin_headers,
            )
            assert skill_path["environment_requirement"]["scope"] == "user"

            _, listed = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment-requirements/list",
                {},
                headers=admin_headers,
            )
            requirements = {
                item["id"]: item for item in listed["environment_requirements"]
            }
            assert requirements["envreq:executable:gitnexus"]["name"] == "gitnexus"
            assert (
                requirements["envreq:path:collaborating-with-claude-skill"]["kind"]
                == "path"
            )
            _, listed_mcp = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/mcp-servers/list",
                {},
                headers=admin_headers,
            )
            assert listed_mcp["mcp_servers"][0]["name"] == "gitnexus-mcp"
            _, listed_skills = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/skills/list",
                {},
                headers=admin_headers,
            )
            assert listed_skills["skills"][0]["name"] == "code-review"

            _, env_dashboard = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment-requirements/dashboard",
                {},
                headers=admin_headers,
            )
            rows = {item["id"]: item for item in env_dashboard["items"]}
            assert env_dashboard["summary"]["total"] == 2
            assert rows["envreq:executable:gitnexus"]["kind"] == "executable"
            assert rows["envreq:executable:gitnexus"]["placement"] == "both"
            assert (
                rows["envreq:executable:gitnexus"]["repo_url"]
                == "https://example.test/gitnexus/repo"
            )
            assert rows["envreq:executable:gitnexus"]["credentials"] == ["GITNEXUS_TOKEN"]
            assert rows["envreq:path:collaborating-with-claude-skill"]["scope"] == "user"
            _, mcp_dashboard = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/mcp-servers/dashboard",
                {},
                headers=admin_headers,
            )
            mcp_rows = {item["id"]: item for item in mcp_dashboard["items"]}
            assert mcp_dashboard["summary"]["total"] == 1
            assert mcp_rows["mcp:gitnexus-mcp"]["placement"] == "peer"
            _, skills_dashboard = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/skills/dashboard",
                {},
                headers=admin_headers,
            )
            skill_rows = {item["id"]: item for item in skills_dashboard["items"]}
            assert skills_dashboard["summary"]["total"] == 1
            assert skill_rows["skill:code-review"]["path_hint"] == "~/.agents/skills/code-review/SKILL.md"
            assert skill_rows["skill:code-review"]["source_path"] == "~/.agents/skills/code-review"

            _, disabled = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment-requirements/enable",
                {
                    "id": "envreq:executable:gitnexus",
                    "enabled": False,
                },
                headers=admin_headers,
            )
            assert disabled["environment_requirement"]["enabled"] is False

            _, disabled_skill = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/skills/enable",
                {"name": "code-review", "enabled": False},
                headers=admin_headers,
            )
            assert disabled_skill["skill"]["enabled"] is False

            _, deleted = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/mcp-servers/delete",
                {"name": "gitnexus-mcp"},
                headers=admin_headers,
            )
            assert deleted["ok"] is True
            assert deleted["kind"] == "mcp_server"
            assert deleted["name"] == "gitnexus-mcp"
            assert "config_etag" in deleted

            _, deleted_skill = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/skills/delete",
                {"name": "code-review"},
                headers=admin_headers,
            )
            assert deleted_skill["ok"] is True
            assert deleted_skill["kind"] == "skill"
            assert deleted_skill["name"] == "code-review"

            raw = config_path.read_text(encoding="utf-8")
            assert "envreq:executable:gitnexus" in raw
            assert "enabled: false" in raw
            assert "envreq:path:collaborating-with-claude-skill" in raw
            assert "gitnexus-mcp" not in raw
            assert "code-review" not in raw
            assert len(reloads) == 8
        finally:
            service.stop()
            relay.stop()

    def test_admin_environment_requirement_record_rejects_invalid_kind(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=tmp_path / "config.yaml",
        )
        service.start()
        try:
            with pytest.raises(HTTPError) as explicit_kind:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/environment-requirements/record",
                    {
                        "environment_requirement": {
                            "kind": "runttime",
                            "name": "node",
                        },
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
            assert explicit_kind.value.code == 400
            explicit_body = json.loads(explicit_kind.value.read().decode("utf-8"))
            assert explicit_body["error"] == "invalid_environment_requirement"

            with pytest.raises(HTTPError) as id_kind:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/environment-requirements/record",
                    {
                        "environment_requirement": {
                            "id": "envreq:runttime:node",
                            "name": "node",
                        },
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
            assert id_kind.value.code == 400
            id_body = json.loads(id_kind.value.read().decode("utf-8"))
            assert id_body["error"] == "invalid_environment_requirement"
        finally:
            service.stop()
            relay.stop()

    def test_admin_behavior_catalog_exposes_chat_commands_mentions_ui_actions_tools_and_packages(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "environment": {
                    "requirements": {
                        "envreq:executable:gh": {
                            "id": "envreq:executable:gh",
                            "kind": "executable",
                            "name": "gh",
                            "command": "gh",
                            "enabled": True,
                            "description": "GitHub CLI",
                            "component_id": "envreq:executable:gh",
                            "package_ids": ["review"],
                        }
                    }
                },
                "mcp": {
                    "servers": {
                        "github": {
                            "command": "github-mcp",
                            "enabled": True,
                            "description": "GitHub MCP",
                            "component_id": "mcp:github",
                            "package_ids": ["review"],
                        }
                    }
                },
                "runtime_profiles": {
                    "codex_remote": {
                        "executor": "codex",
                        "execution_location": "remote_server",
                    }
                },
                "agent_registry": {
                    "agents": {
                        "reviewer": {
                            "runtime_profile": "codex_remote",
                            "capability_refs": ["review"],
                        }
                    }
                },
                "capability_packages": {
                    "review": {
                        "name": "Review",
                        "description": "Repository review tools",
                        "components": ["envreq:executable:gh", "mcp:github"],
                    }
                },
                "capability_components": {
                    "envreq:executable:gh": {
                        "kind": "environment_requirement",
                        "name": "gh",
                        "config": {"kind": "executable", "command": "gh"},
                        "package_ids": ["review"],
                    },
                    "mcp:github": {
                        "kind": "mcp",
                        "name": "github",
                        "package_ids": ["review"],
                    },
                },
            },
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, catalog = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/behavior/catalog",
                {},
                headers=TEST_ADMIN_HEADERS,
            )

            assert catalog["ok"] is True
            assert "user_actions" in catalog

            commands = {item["id"]: item for item in catalog["chat_commands"]}
            assert "system.help" in commands
            assert commands["system.help"]["feature_id"] == "system"
            assert commands["system.help"]["trigger"] == "/help"
            assert commands["system.help"]["trigger_kind"] == "slash"
            assert commands["system.help"]["ui_targets"] == ["cli", "tui", "vscode"]
            assert commands["system.help"]["supports_args"] is False
            assert commands["system.help"]["selection_behavior"] == "dispatch"
            assert commands["system.help"]["available_during_run"] is True
            assert commands["system.help"]["visibility"] == "visible"
            assert commands["system.debug"]["trigger"] == "/debug"
            assert commands["system.debug"]["supports_args"] is True
            assert commands["system.debug"]["args_hint"] == "on|off"
            assert commands["system.debug"]["selection_behavior"] == "insert_for_args"
            assert commands["model.switch"]["trigger"] == "/model <profile>"
            assert commands["model.switch"]["supports_args"] is True
            assert commands["model.switch"]["args_hint"] == "profile"
            assert commands["model.switch"]["selection_behavior"] == "insert_for_args"

            ui_actions = {item["id"]: item for item in catalog["ui_actions"]}
            assert "settings.environment_requirements.refresh_manifest" in ui_actions
            assert (
                ui_actions["settings.environment_requirements.refresh_manifest"]["source_type"]
                == "settings_ui"
            )
            settings_registration_paths = {
                str(item["registration_path"])
                for item in ui_actions.values()
                if str(item["id"]).startswith("settings.")
            }
            assert settings_registration_paths == {
                "dogcode.webview-ui.settings.CapabilitiesTab"
            }
            old_settings_tab = "dogcode.webview-ui.settings." + "Tool" + "chains" + "Tab"
            assert all(
                old_settings_tab not in str(item["registration_path"])
                for item in ui_actions.values()
            )
            assert "settings.environment_requirements.refresh_manifest" not in commands

            mention_providers = {
                item["id"]: item for item in catalog["mention_providers"]
            }
            assert mention_providers["workspace_files"]["trigger"] == "@"
            assert mention_providers["capability_packages"]["source_type"] == "config"
            assert mention_providers["agent_tools"]["source_type"] == "behavior_catalog"
            user_actions = {item["id"]: item for item in catalog["user_actions"]}
            assert user_actions["slash:system.help"]["trigger"] == "/help"
            assert user_actions["slash:system.help"]["reference_only"] is False
            assert user_actions["mention:agent_tools"]["trigger"] == "@"
            assert user_actions["mention:agent_tools"]["reference_only"] is True

            tools = {item["id"]: item for item in catalog["agent_tools"]}
            assert tools["builtin:fetch_capabilities"]["source_type"] == "builtin"
            assert tools["builtin:fetch_capabilities"]["execution_policy"] in {
                "allow",
                "deny",
                "require_user",
                "escalate",
                "inherit",
            }
            assert (
                tools["builtin:fetch_capabilities"]["registration_path"]
                == "reuleauxcoder.extensions.tools.registry"
            )
            assert tools["builtin:fetch_capabilities"]["related_package_ids"] == [
                "capability_packager_builtin_tools",
                "core_builtin_tools",
            ]
            assert tools["builtin:fetch_capabilities"]["related_components"] == [
                "builtin_tool:fetch_capabilities"
            ]
            assert tools["builtin:fetch_capabilities"]["permission"]["action"] == "allow"
            assert (
                tools["builtin:fetch_capabilities"]["permission"]["capability_matched"]
                == "capability:core_builtin_tools:builtin_tool:fetch_capabilities"
            )
            assert tools["capability_package:review"]["source_type"] == "capability_package"
            assert tools["capability_package:review"]["execution_policy"] == "inherit"
            assert (
                tools["capability_package:review"]["registration_path"]
                == "agent.capability_refs[]"
            )
            assert tools["capability_package:review"]["related_components"] == [
                "envreq:executable:gh",
                "mcp:github",
            ]
            assert tools["mcp:github"]["related_package_ids"] == ["review"]

            with pytest.raises(HTTPError) as old_route:
                old_behavior_path = (
                    "/remote/admin/environment-requirements/" + "behavior-catalog"
                )
                _json_request(
                    "POST",
                    f"{service.base_url}{old_behavior_path}",
                    {},
                    headers=TEST_ADMIN_HEADERS,
                )
            assert old_route.value.code == 404
        finally:
            service.stop()
            relay.stop()

    def test_admin_write_rolls_back_when_reload_fails(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "providers:\n"
            "  items:\n"
            "    existing:\n"
            "      type: openai_chat\n"
            "      api_key: sk-existing\n"
            "      base_url: https://example.invalid/v1\n",
            encoding="utf-8",
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: (_ for _ in ()).throw(
                RuntimeError("reload failed")
            ),
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/providers/record",
                    {
                        "provider_id": "broken",
                        "type": "openai_chat",
                        "api_key": "sk-broken",
                        "base_url": "https://broken.invalid/v1",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
                raise AssertionError("reload failure should surface as HTTP 500")
            except HTTPError as exc:
                assert exc.code == 500
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "config_reload_failed"
                assert body["message"] == "reload failed"
            raw = config_path.read_text(encoding="utf-8")
            assert "existing" in raw
            assert "sk-existing" in raw
            assert "broken" not in raw
            assert "sk-broken" not in raw
            audit_text = json.dumps(service.auth_service.store.audit_events)
            assert "admin_config_failed" in audit_text
            assert "sk-broken" not in audit_text
        finally:
            service.stop()
            relay.stop()

    def test_admin_config_etag_conflict_and_audit_redacts_secrets(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("models:\n  profiles: {}\n", encoding="utf-8")
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, read_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/read",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            initial_etag = read_body["config_etag"]

            _, created = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/record",
                {
                    "if_match": initial_etag,
                    "provider_id": "deepseek",
                    "type": "openai_chat",
                    "api_key": "sk-secret-value",
                    "base_url": "https://api.deepseek.com/v1",
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert created["ok"] is True
            assert created["config_etag"] != initial_etag

            _, enabled = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/enable",
                {"provider_id": "deepseek", "enabled": False},
                headers={**TEST_ADMIN_HEADERS, "If-Match": created["config_etag"]},
            )
            assert enabled["ok"] is True

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/providers/record",
                    {
                        "if_match": initial_etag,
                        "provider_id": "blocked",
                        "type": "openai_chat",
                        "base_url": "https://blocked.invalid/v1",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
                raise AssertionError("stale if_match should be rejected")
            except HTTPError as exc:
                assert exc.code == 409
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "config_version_conflict"
                assert body["details"]["config_etag"] == enabled["config_etag"]

            raw = config_path.read_text(encoding="utf-8")
            assert "blocked" not in raw
            events = service.auth_service.store.audit_events
            event_types = [event["type"] for event in events]
            assert "admin_config_updated" in event_types
            assert "admin_config_conflict" in event_types
            audit_text = json.dumps(events)
            assert "sk-secret-value" not in audit_text
            assert "***" in audit_text
        finally:
            service.stop()
            relay.stop()

    def test_admin_diagnostics_settings_and_tool_diagnostic_stats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        diagnostics_dir = tmp_path / ".rcoder" / "diagnostics"
        diagnostics_dir.mkdir(parents=True)
        (diagnostics_dir / "tool_diagnostics.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-14 20:00:00",
                    "metadata": {
                        "provider_id": "deepseek",
                        "compat": "deepseek",
                        "model": "deepseek-v4",
                        "tool": "apply_patch",
                    },
                    "diagnostics": [
                        {
                            "stage": "argument_validation",
                            "kind": "schema_issue",
                            "severity": "error",
                            "code": "missing_required",
                            "path": "$.patch",
                            "expected": "string",
                            "actual": "missing",
                            "message": "$.patch: expected string, got missing",
                            "repairable": False,
                        },
                        {
                            "stage": "argument_validation",
                            "kind": "repair_applied",
                            "severity": "warning",
                            "code": "optional_null_omitted",
                            "action": "optional_null_omitted",
                            "path": "$.encoding",
                            "message": "optional_null_omitted",
                            "repairable": True,
                        },
                    ],
                    "validation": {
                        "tool_name": "apply_patch",
                        "final_valid": False,
                        "initial_issues": [
                            {
                                "code": "missing_required",
                                "path": "$.patch",
                                "expected": "string",
                                "actual": "missing",
                            }
                        ],
                        "repairs": [
                            {
                                "action": "optional_null_omitted",
                                "path": "$.encoding",
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, read_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/read",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            diagnostics = read_body["settings"]["diagnostics"]["tool_diagnostics"]
            assert diagnostics == {"enabled": True, "record_clean": False}
            assert read_body["settings"]["diagnostics"]["llm_trace"] == {
                "enabled": False,
                "raw_chunks": False,
            }

            _, update_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/update",
                {
                    "settings": {
                        "diagnostics": {
                            "tool_diagnostics": {"enabled": False},
                            "llm_trace": {"enabled": True, "raw_chunks": True},
                        }
                    }
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert update_body["ok"] is True
            assert (
                update_body["settings"]["diagnostics"]["tool_diagnostics"][
                    "enabled"
                ]
                is False
            )
            assert update_body["settings"]["diagnostics"]["llm_trace"] == {
                "enabled": True,
                "raw_chunks": True,
            }
            assert load_yaml_config(config_path)["diagnostics"][
                "tool_diagnostics"
            ] == {"enabled": False, "record_clean": False}
            assert load_yaml_config(config_path)["diagnostics"]["llm_trace"] == {
                "enabled": True,
                "raw_chunks": True,
            }

            _, stats_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/diagnostics/tool-diagnostics/stats",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            stats = stats_body["tool_diagnostics"]
            assert stats["totals"] == {
                "events": 1,
                "diagnostics": 2,
                "errors": 1,
                "warnings": 1,
                "repaired": 1,
            }
            assert stats["by_model"][0]["name"] == "deepseek-v4"
            assert stats["by_stage"][0]["name"] == "argument_validation"
            assert stats["issues"][0]["path"] == "$.patch"
            assert stats["repairs"][0]["action"] == "optional_null_omitted"
        finally:
            service.stop()
            relay.stop()

    def test_admin_server_settings_manage_runtime_policy_groups(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "persistence:\n"
            "  backend: postgres\n"
            "  database_url: postgresql://example\n"
            "github:\n"
            "  enabled: false\n"
            "  webhook_secret: super-secret-value\n"
            "memory:\n"
            "  enabled: false\n"
            "  providers:\n"
            "    old:\n"
            "      adapter: missing_old_adapter\n"
            "  sources:\n"
            "    old_source:\n"
            "      adapter: missing_old_source_adapter\n"
            "      target_provider: old\n",
            encoding="utf-8",
        )
        reloads: list[str] = []
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: reloads.append("reload"),
        )
        service.start()
        try:
            _, read_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/read",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            settings = read_body["settings"]
            assert settings["tool_output"]["max_chars"] == 12000
            assert settings["context"]["snip_threshold_chars"] == 1500
            assert settings["memory"]["enabled"] is False
            assert settings["memory"]["default_provider"] == ""
            assert "old" in settings["memory"]["providers"]
            assert "old_source" in settings["memory"]["sources"]
            assert settings["approval"]["default_mode"] == "require_approval"
            assert "coder" in settings["modes"]["profiles"]
            assert settings["skills"]["enabled"] is True
            assert settings["prompt"]["system_append"] == ""
            assert settings["persistence"]["backend"] == "postgres"
            assert settings["github"]["webhook_secret_hint"] == "supe...alue"
            assert "webhook_secret" not in settings["github"]

            _, update_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/update",
                {
                    "settings": {
                        "tool_output": {
                            "max_chars": 24000,
                            "max_lines": 240,
                            "store_full_output": False,
                            "store_dir": ".rcoder/full-tools",
                        },
                        "context": {
                            "snip_keep_recent_tools": 3,
                            "snip_threshold_chars": 3000,
                            "snip_min_lines": 8,
                            "summarize_keep_recent_turns": 7,
                            "token_fudge_factor": 1.3,
                        },
                        "memory": {
                            "enabled": False,
                            "default_provider": "",
                            "default_agent_id": "core",
                            "default_namespace": "tests",
                            "runtime": {
                                "inject_default": True,
                                "capture_default": False,
                                "token_budget_default": 256,
                                "fail_mode": "open",
                            },
                            "providers": {},
                            "sources": {},
                            "tools": {
                                "enabled": False,
                                "provider": "",
                            },
                        },
                        "approval": {
                            "default_mode": "warn",
                            "rules": [{"tool_name": "shell", "action": "deny"}],
                        },
                        "modes": {
                            "active": "review",
                            "profiles": {
                                "review": {
                                    "description": "Review only",
                                    "tools": ["read_file"],
                                    "prompt_append": "Review carefully.",
                                }
                            },
                        },
                        "skills": {
                            "enabled": True,
                            "scan_project": False,
                            "scan_user": True,
                            "disabled": ["legacy-skill"],
                        },
                        "prompt": {"system_append": "Global note."},
                        "persistence": {
                            "runtime_enabled": False,
                            "sessions_enabled": True,
                            "retention_days": 14,
                            "event_payload_compress_threshold_bytes": 1024,
                            "maintenance_interval_sec": 120,
                        },
                        "sandbox_provider": {
                            "type": "docker",
                            "host_base_url": "http://sandbox:8765",
                            "worker_image": "worker:test",
                            "workspace_volume_root": "workspaces",
                            "idle_ttl_seconds": 600,
                        },
                        "model_capabilities": {
                            "enabled": False,
                            "interval_sec": 3600,
                        },
                        "diagnostics": {
                            "tool_diagnostics": {
                                "enabled": True,
                                "record_clean": True,
                            }
                        },
                        "github": {
                            "enabled": False,
                            "app_id": "1",
                            "installation_id": "2",
                            "private_key_path": "/tmp/key.pem",
                            "api_base_url": "https://api.github.com",
                            "web_base_url": "https://github.com",
                            "reconcile_interval_sec": 30,
                        },
                    }
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert update_body["ok"] is True
            updated = update_body["settings"]
            assert updated["tool_output"]["max_chars"] == 24000
            assert updated["context"]["token_fudge_factor"] == 1.3
            assert updated["memory"]["enabled"] is False
            assert updated["memory"]["runtime"]["token_budget_default"] == 256
            assert updated["memory"]["providers"] == {}
            assert updated["memory"]["sources"] == {}
            assert updated["approval"]["rules"][0]["action"] == "deny"
            assert updated["modes"]["active"] == "review"
            assert updated["modes"]["profiles"]["review"]["tools"] == ["read_file"]
            assert updated["skills"]["disabled"] == ["legacy-skill"]
            assert updated["prompt"]["system_append"] == "Global note."
            assert updated["persistence"]["retention_days"] == 14
            assert updated["sandbox_provider"]["worker_image"] == "worker:test"
            assert updated["model_capabilities"]["enabled"] is False
            assert updated["diagnostics"]["tool_diagnostics"]["record_clean"] is True
            assert updated["github"]["webhook_secret_hint"] == "supe...alue"

            raw = load_yaml_config(config_path)
            assert raw["github"]["webhook_secret"] == "super-secret-value"
            assert raw["persistence"]["backend"] == "postgres"
            assert raw["persistence"]["database_url"] == "postgresql://example"
            assert raw["memory"]["providers"] == {}
            assert raw["memory"]["sources"] == {}
            assert len(reloads) == 1
        finally:
            service.stop()
            relay.stop()

    def test_admin_server_settings_reject_read_only_persistence_fields(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: None,
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/server-settings/update",
                    {"settings": {"persistence": {"database_url": "postgres://x"}}},
                    headers=TEST_ADMIN_HEADERS,
                )
                raise AssertionError("read-only persistence field should fail")
            except HTTPError as exc:
                assert exc.code == 400
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "read_only_persistence_field"
                assert body["details"]["fields"] == ["database_url"]
        finally:
            service.stop()
            relay.stop()

    def test_auth_bootstrap_token_and_artifact_endpoint(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda os_name, arch, name: (
                (
                    b"peer-binary",
                    "application/octet-stream",
                )
                if (os_name, arch, name) == ("linux", "amd64", "rcoder-peer")
                else None
            ),
            bootstrap_token_ttl_sec=60,
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/auth/bootstrap-token",
                    {},
                )
                raise AssertionError("bootstrap token should require auth")
            except HTTPError as exc:
                assert exc.code == 401
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "unauthorized"

            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/auth/bootstrap-token",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            assert status == 200
            assert body["bootstrap_token"].startswith("bt_")

            with _URLOPEN(
                f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                timeout=5,
            ) as resp:
                assert resp.status == 200
                etag = resp.headers["ETag"]
                assert etag.startswith('"sha256-')
                assert resp.read() == b"peer-binary"
            req = request.Request(
                f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                headers={"If-None-Match": etag},
                method="GET",
            )
            with pytest.raises(HTTPError) as excinfo:
                _URLOPEN(req, timeout=5)
            assert excinfo.value.code == 304
            assert excinfo.value.headers["ETag"] == etag
        finally:
            service.stop()
            relay.stop()

    def test_peer_mcp_manifest_artifact_and_tools_report(self, tmp_path: Path) -> None:
        artifact_root = tmp_path / "artifacts"
        artifact_path = artifact_root / "local-filesystem" / "1.0.0" / "linux-amd64.tar.gz"
        artifact_path.parent.mkdir(parents=True)
        artifact_content = b"fake-archive"
        artifact_path.write_bytes(artifact_content)
        artifact_sha = hashlib.sha256(artifact_content).hexdigest()

        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            mcp_artifact_root=artifact_root,
            mcp_servers=[
                MCPServerConfig(
                    name="github",
                    command="github-mcp",
                    placement="server",
                ),
                MCPServerConfig(
                    name="local-filesystem",
                    command="",
                    placement="peer",
                    distribution="artifact",
                    version="1.0.0",
                    launch=MCPLaunchConfig(
                        command="{{bundle}}/filesystem-mcp",
                        args=["--root", "{{workspace}}"],
                    ),
                    artifacts={
                        "linux-amd64": MCPArtifactConfig(
                            path="local-filesystem/1.0.0/linux-amd64.tar.gz",
                            sha256=artifact_sha,
                            launch=MCPLaunchConfig(
                                command="{{bundle}}/run.sh",
                                args=["--root", "{{workspace}}"],
                            ),
                        )
                    },
                    environment_requirement_refs=[
                        "envreq:runtime:node",
                        "envreq:executable:npm",
                    ],
                    permissions={"tools": {"apply_patch": "require_approval"}},
                ),
                MCPServerConfig(
                    name="missing-platform",
                    command="missing",
                    placement="peer",
                    distribution="artifact",
                    version="1.0.0",
                    launch=MCPLaunchConfig(command="{{bundle}}/missing"),
                    artifacts={},
                ),
                MCPServerConfig(
                    name="shared-browser",
                    command="npx",
                    args=["-y", "@demo/browser@1.0.0"],
                    placement="both",
                    distribution="artifact",
                    version="1.0.0",
                    launch=MCPLaunchConfig(command="{{bundle}}/browser"),
                    artifacts={
                        "linux-amd64": MCPArtifactConfig(
                            path="shared-browser/1.0.0/linux-amd64.tar.gz",
                            sha256=artifact_sha,
                        )
                    },
                ),
            ],
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/mcp/manifest",
                    {"peer_token": "bad", "os": "linux", "arch": "amd64"},
                )
                raise AssertionError("manifest should require valid peer token")
            except HTTPError as exc:
                assert exc.code == 401

            status, manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/mcp/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )
            assert status == 200
            assert [server["name"] for server in manifest["servers"]] == [
                "local-filesystem",
                "shared-browser",
            ]
            server_manifest = manifest["servers"][0]
            assert server_manifest["artifact"]["sha256"] == artifact_sha
            assert server_manifest["distribution"] == "artifact"
            assert server_manifest["launch"]["command"] == "{{bundle}}/run.sh"
            assert server_manifest["launch"]["args"] == ["--root", "{{workspace}}"]
            assert server_manifest["environment_requirement_refs"] == [
                "envreq:runtime:node",
                "envreq:executable:npm",
            ]
            assert server_manifest["permissions"]["tools"]["apply_patch"] == "require_approval"
            assert manifest["diagnostics"][0]["server"] == "missing-platform"

            try:
                _bytes_request(
                    f"{service.base_url}{server_manifest['artifact']['url']}"
                )
                raise AssertionError("artifact should require peer token")
            except HTTPError as exc:
                assert exc.code == 401

            status, body = _bytes_request(
                f"{service.base_url}{server_manifest['artifact']['url']}",
                headers={"X-RC-Peer-Token": peer_token},
            )
            assert status == 200
            assert body == artifact_content
            req = request.Request(
                f"{service.base_url}{server_manifest['artifact']['url']}",
                headers={
                    "X-RC-Peer-Token": peer_token,
                    "If-None-Match": f'"sha256-{artifact_sha}"',
                },
                method="GET",
            )
            with pytest.raises(HTTPError) as excinfo:
                _URLOPEN(req, timeout=5)
            assert excinfo.value.code == 304
            assert excinfo.value.headers["ETag"] == f'"sha256-{artifact_sha}"'

            status, report = _json_request(
                "POST",
                f"{service.base_url}/remote/mcp/tools",
                {
                    "peer_token": peer_token,
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read a local file",
                            "input_schema": {"type": "object"},
                            "server_name": "local-filesystem",
                        }
                    ],
                },
            )
            assert status == 200
            assert report["ok"] is True
            assert relay.get_peer_mcp_tools(peer_id)[0].server_name == "local-filesystem"
        finally:
            service.stop()
            relay.stop()

    def test_peer_mcp_manifest_command_distribution_without_artifact(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            mcp_servers=[
                MCPServerConfig(
                    name="gitnexus",
                    command="gitnexus",
                    args=["mcp"],
                    placement="peer",
                    distribution="command",
                    version="1.6.3",
                    check="gitnexus --version",
                    install="npm install -g gitnexus@1.6.3",
                    environment_requirement_refs=[
                        "envreq:runtime:node",
                        "envreq:executable:npm",
                    ],
                    artifacts={
                        "linux-amd64": MCPArtifactConfig(
                            path="gitnexus/1.6.3/linux-amd64.tar.gz",
                            sha256="legacy",
                        )
                    },
                )
            ],
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/mcp/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )

            assert status == 200
            assert manifest["diagnostics"] == []
            server = manifest["servers"][0]
            assert server["name"] == "gitnexus"
            assert server["distribution"] == "command"
            assert server["artifact"] is None
            assert server["launch"]["command"] == "gitnexus"
            assert server["launch"]["args"] == ["mcp"]
            assert server["environment_requirement_refs"] == [
                "envreq:runtime:node",
                "envreq:executable:npm",
            ]
        finally:
            service.stop()
            relay.stop()

    def test_environment_manifest_endpoint_returns_structured_manifest(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            mcp_servers=[
                MCPServerConfig(
                    name="gitnexus-mcp",
                    command="gitnexus",
                    args=["mcp"],
                    placement="peer",
                    distribution="command",
                    check="gitnexus --version",
                    install="npm install -g gitnexus@1.6.3",
                    environment_requirement_refs=["envreq:runtime:node"],
                    docs=[{"title": "GitNexus MCP", "url": "https://example.test/mcp"}],
                    install_prompt="Install MCP through npm only.",
                    verify_prompt="Verify MCP starts with the mcp argument.",
                    notes=["Do not install node automatically."],
                ),
                MCPServerConfig(
                    name="disabled-mcp",
                    command="disabled-mcp",
                    enabled=False,
                    placement="peer",
                )
            ],
            capability_packages={
                "disabled-package": {
                    "enabled": False,
                    "components": ["envreq:executable:package-disabled"],
                },
                "inactive-package": {
                    "enabled": True,
                    "state": {"activation_state": "inactive"},
                    "components": ["envreq:executable:package-inactive"],
                }
            },
            environment_requirements={
                "envreq:executable:beads": {
                    "id": "envreq:executable:beads",
                    "kind": "executable",
                    "name": "beads",
                    "command": "beads",
                    "tags": ["issue_tracking"],
                    "check": "beads --version",
                    "install": "npm install -g beads",
                    "source": "npm",
                    "docs": [{"title": "Beads", "url": "https://example.test/beads"}],
                    "install_prompt": "Use npm global install for beads.",
                    "verify_prompt": "Run beads --version after install.",
                    "notes": ["Do not install node automatically."],
                },
                "envreq:executable:gitnexus": EnvironmentRequirementConfig(
                    id="envreq:executable:gitnexus",
                    kind="executable",
                    name="gitnexus",
                    command="gitnexus",
                    tags=["repo_index"],
                    check="gitnexus --version",
                    install="npm install -g gitnexus",
                    source="npm",
                    docs=[{"title": "GitNexus", "url": "https://example.test/gitnexus"}],
                    install_prompt="Use the configured npm command.",
                    verify_prompt="Check gitnexus version output.",
                    notes=["PATH changes require explicit approval."],
                ),
                "envreq:executable:disabled": EnvironmentRequirementConfig(
                    id="envreq:executable:disabled",
                    kind="executable",
                    name="disabled",
                    command="disabled",
                    enabled=False,
                    check="disabled --version",
                ),
                "envreq:path:collaborating-with-claude-skill": {
                    "id": "envreq:path:collaborating-with-claude-skill",
                    "kind": "path",
                    "name": "collaborating-with-claude-skill",
                    "scope": "user",
                    "check": "Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                    "install": "python install-skill.py",
                    "version": "1.0.0",
                    "source": "github",
                    "description": "Claude bridge skill",
                    "path": "~/.agents/skills/collaborating-with-claude/SKILL.md",
                    "docs": [{"title": "Claude skill", "url": "https://example.test/skill"}],
                    "install_prompt": "Install from the curated skill source.",
                    "verify_prompt": "Verify SKILL.md exists.",
                    "notes": ["Use user scope."],
                },
                "envreq:path:disabled-skill": {
                    "id": "envreq:path:disabled-skill",
                    "kind": "path",
                    "name": "disabled-skill",
                    "enabled": False,
                    "check": "Test-Path disabled",
                },
                "envreq:credential:github-token": {
                    "id": "envreq:credential:github-token",
                    "kind": "credential",
                    "name": "github-token",
                    "placement": "peer",
                    "description": "GitHub token supplied by the peer environment.",
                },
                "envreq:executable:package-disabled": {
                    "id": "envreq:executable:package-disabled",
                    "kind": "executable",
                    "name": "package-disabled",
                    "command": "package-disabled",
                    "managed_by": "capability_package",
                    "package_ids": ["disabled-package"],
                    "component_id": "envreq:executable:package-disabled",
                },
                "envreq:executable:package-inactive": {
                    "id": "envreq:executable:package-inactive",
                    "kind": "executable",
                    "name": "package-inactive",
                    "command": "package-inactive",
                    "managed_by": "capability_package",
                    "package_ids": ["inactive-package"],
                    "component_id": "envreq:executable:package-inactive",
                }
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/environment/manifest",
                    {"peer_token": "bad", "os": "linux", "arch": "amd64"},
                )
                raise AssertionError("environment manifest should require valid token")
            except HTTPError as exc:
                assert exc.code == 401

            status, manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/environment/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )

            assert status == 200
            requirements = {
                requirement["id"]: requirement
                for requirement in manifest["environment_requirements"]
            }
            assert "envreq:executable:disabled" not in requirements
            assert "envreq:path:disabled-skill" not in requirements
            assert "envreq:executable:package-disabled" not in requirements
            assert "envreq:executable:package-inactive" not in requirements
            assert "mcp_servers" not in manifest
            assert requirements["envreq:executable:gitnexus"]["check"] == "gitnexus --version"
            assert requirements["envreq:executable:gitnexus"]["docs"][0]["title"] == "GitNexus"
            assert (
                requirements["envreq:executable:gitnexus"]["install_prompt"]
                == "Use the configured npm command."
            )
            assert (
                requirements["envreq:executable:gitnexus"]["verify_prompt"]
                == "Check gitnexus version output."
            )
            assert requirements["envreq:executable:gitnexus"]["notes"] == [
                "PATH changes require explicit approval."
            ]
            assert requirements["envreq:executable:beads"]["tags"] == ["issue_tracking"]
            assert (
                requirements["envreq:executable:beads"]["install_prompt"]
                == "Use npm global install for beads."
            )
            assert requirements["envreq:credential:github-token"]["kind"] == "credential"
            assert requirements["envreq:credential:github-token"]["check"] == ""
            skill_requirement = requirements[
                "envreq:path:collaborating-with-claude-skill"
            ]
            assert skill_requirement["scope"] == "user"
            assert (
                skill_requirement["path"]
                == "~/.agents/skills/collaborating-with-claude/SKILL.md"
            )
            assert skill_requirement["docs"][0]["title"] == "Claude skill"
            assert skill_requirement["verify_prompt"] == "Verify SKILL.md exists."
            assert "prompt" not in manifest
        finally:
            service.stop()
            relay.stop()

    def test_environment_manifest_can_be_scoped_to_agent_capabilities(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            capability_packages={
                "review-tools": {"enabled": True},
                "deploy-tools": {"enabled": True},
            },
            environment_requirement_scope_ids={
                "reviewer": {"envreq:executable:gh"},
            },
            environment_requirements={
                "envreq:executable:gh": {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "managed_by": "capability_package",
                    "package_ids": ["review-tools"],
                    "component_id": "envreq:executable:gh",
                },
                "envreq:executable:deploy": {
                    "id": "envreq:executable:deploy",
                    "kind": "executable",
                    "name": "deploy",
                    "command": "deploy",
                    "managed_by": "capability_package",
                    "package_ids": ["deploy-tools"],
                    "component_id": "envreq:executable:deploy",
                },
                "envreq:runtime:node": {
                    "id": "envreq:runtime:node",
                    "kind": "runtime",
                    "name": "node",
                    "check": "node --version",
                },
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, scoped_manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/environment/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                    "agent_id": "reviewer",
                },
            )
            assert [
                requirement["id"]
                for requirement in scoped_manifest["environment_requirements"]
            ] == ["envreq:executable:gh"]

            _, unknown_agent_manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/environment/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                    "agent_id": "unknown-agent",
                },
            )
            assert unknown_agent_manifest["environment_requirements"] == []

            _, global_manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/environment/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )
            assert {
                requirement["id"]
                for requirement in global_manifest["environment_requirements"]
            } == {
                "envreq:executable:gh",
                "envreq:executable:deploy",
                "envreq:runtime:node",
            }
        finally:
            service.stop()
            relay.stop()

    def test_environment_run_endpoint_submits_agent_run(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "environment_local": {
                        "executor": "fake",
                        "execution_location": "local_workspace",
                    }
                },
                "agents": {
                    "environment_configurator": {
                        "runtime_profile": "environment_local",
                        "capability_refs": ["environment"],
                        "resolved_capabilities": {},
                    }
                },
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            environment_requirements={
                "envreq:executable:gitnexus": EnvironmentRequirementConfig(
                    id="envreq:executable:gitnexus",
                    kind="executable",
                    name="gitnexus",
                    command="gitnexus",
                    check="gitnexus --version",
                    install="npm install -g gitnexus",
                )
            },
        )
        service.start()
        try:
            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment/run",
                {
                    "mode": "configure",
                    "workspace_root": "/tmp/peer",
                    "entry_ids": ["envreq:executable:gitnexus"],
                },
                headers=TEST_ADMIN_HEADERS,
            )

            assert status == 200
            agent_run = body["agent_run"]
            assert body["ok"] is True
            assert body["agent_id"] == "environment_configurator"
            assert agent_run["trigger_mode"] == "environment_config"
            assert agent_run["metadata"]["workflow"] == "environment_config"
            assert agent_run["metadata"]["environment_mode"] == "configure"
            assert {
                "entry_id": "envreq:executable:gitnexus",
                "kind": "environment_requirement",
                "name": "gitnexus",
                "phase": "install",
                "command": "npm install -g gitnexus",
            } in agent_run["metadata"]["allowed_commands"]
        finally:
            service.stop()
            relay.stop()

    def test_agent_run_model_request_endpoint_uses_server_model_bridge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created_configs = []
        seen_requests = []

        class FakeProvider:
            def chat(self, provider_request):
                seen_requests.append(provider_request)
                return ProviderResponse(
                    content="hi",
                    prompt_tokens=1,
                    completion_tokens=2,
                )

        def create_provider(_self, config):
            created_configs.append(config)
            return FakeProvider()

        monkeypatch.setattr(
            "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
            create_provider,
        )
        monkeypatch.setattr(
            "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
            lambda _self, **kwargs: json.dumps(
                {
                    "ok": True,
                    "url": kwargs["url"],
                    "title": "Example Tool",
                    "sections": [],
                    "links": [],
                    "evidence": [],
                    "errors": [],
                }
            ),
        )
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "packager": {
                        "executor": "reuleauxcoder",
                        "execution_location": "remote_server",
                        "worker_kind": "sandbox_worker",
                        "model_request_origin": "server",
                    }
                },
                "agents": {
                    "capability_packager": {
                        "visibility": "system",
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "packager",
                        "model": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                            "parameters": {"max_tokens": 384000, "temperature": 0.2},
                        },
                    }
                },
            }
        )
        persisted_trace_events = []

        def trace_sink(
            session_id,
            event_type,
            payload,
            session_run_id,
            session_run_seq,
            source,
            replayable,
        ):
            seq = len(persisted_trace_events) + 1
            persisted_trace_events.append(
                {
                    "session_id": session_id,
                    "type": event_type,
                    "payload": payload,
                    "session_run_id": session_run_id,
                    "session_run_seq": session_run_seq,
                    "source": source,
                    "replayable": replayable,
                }
            )
            return seq

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.set_session_trace_event_sink(trace_sink)
        service.start()
        try:
            bootstrap_token = relay.issue_bootstrap_token(ttl_sec=60)
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": bootstrap_token,
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            ingest_payload = {
                "peer_token": peer_token,
                "session_id": "session-capability-1",
                "client_request_id": "capability-ingest-req-1",
                "locale": "zh-CN",
                "source": {
                    "type": "github_repo",
                    "url": "https://github.com/acme/example-tool",
                },
            }
            ingest_status, ingest_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                ingest_payload,
                headers=TEST_ADMIN_HEADERS,
            )
            assert ingest_status == 200
            assert ingest_body["session_run_id"]
            assert ingest_body["session_id"] == "session-capability-1"
            assert ingest_body["agent_run_id"]
            assert ingest_body["branch_binding_id"] == "main"
            assert ingest_body["scope_id"] == f"{ingest_body['session_run_id']}:main"
            assert ingest_body["selected"] is True
            assert ingest_body["runtime_state"]["agent_id"] == "capability_packager"
            assert ingest_body["runtime_state"]["agent_run_id"] == ingest_body["agent_run_id"]
            assert ingest_body["activation_id"] == ingest_body["runtime_state"]["activation_id"]
            assert ingest_body["activation_id"] == f"{ingest_body['agent_run_id']}:activation:1"
            assert ingest_body["runtime_state"]["scope_id"] == ingest_body["scope_id"]
            assert ingest_body["runtime_state"]["active_model_provider"] == "deepseek"
            assert any(
                event["type"] == "session_run_start"
                and event["session_id"] == "session-capability-1"
                and event["session_run_id"] == ingest_body["session_run_id"]
                and event["payload"]["workflow_mode"] == "capability_package_ingest"
                for event in persisted_trace_events
            )
            persisted_session_event_types = [
                event["type"]
                for event in persisted_trace_events
                if event["session_run_id"] == ingest_body["session_run_id"]
            ]
            assert persisted_session_event_types[0] == "session_run_start"
            assert persisted_session_event_types.count("session_run_start") == 1
            first_workflow_index = persisted_session_event_types.index("workflow_step")
            assert persisted_session_event_types.index("session_run_start") < first_workflow_index
            retry_status, retry_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                ingest_payload,
                headers=TEST_ADMIN_HEADERS,
            )
            assert retry_status == 200
            assert retry_body["session_run_id"] == ingest_body["session_run_id"]
            assert retry_body["session_id"] == ingest_body["session_id"]
            assert retry_body["agent_run_id"] == ingest_body["agent_run_id"]
            assert retry_body["activation_id"] == ingest_body["activation_id"]
            assert retry_body["runtime_state"]["activation_id"] == ingest_body["activation_id"]
            assert retry_body["branch_binding_id"] == "main"
            assert retry_body["scope_id"] == ingest_body["scope_id"]
            session_run_id = ingest_body["session_run_id"]
            session = service._get_session_run(session_run_id)
            assert session is not None
            session_event_types = [event.get("type") for event in session.events]
            assert session_event_types[0] == "session_run_start"
            assert session_event_types.count("session_run_start") == 1
            assert session_event_types.index("session_run_start") < session_event_types.index(
                "workflow_step"
            )
            assert session.locale == "zh-CN"
            assert any(
                event.get("type") == "session_run_start"
                and event.get("payload", {}).get("locale") == "zh-CN"
                for event in session.events
            )
            agent_run_id = ""
            deadline = time.time() + 3
            while time.time() < deadline and not agent_run_id:
                session = service._get_session_run(session_run_id)
                assert session is not None
                for event in session.events:
                    payload = event.get("payload")
                    if isinstance(payload, dict) and payload.get("agent_run_id"):
                        agent_run_id = payload["agent_run_id"]
                        break
                if not agent_run_id:
                    time.sleep(0.02)
            assert agent_run_id
            agent_run = control.agent_run_to_dict(agent_run_id)
            assert agent_run["status"] == "queued"
            assert agent_run["agent_id"] == "capability_packager"
            assert agent_run["metadata"]["repo_url"] == "https://github.com/acme/example-tool"
            assert agent_run["metadata"]["session_id"] == "session-capability-1"
            assert agent_run["metadata"]["session_run_id"] == session_run_id
            assert agent_run["metadata"]["client_request_id"] == "capability-ingest-req-1"
            assert agent_run["metadata"]["workflow"] == "capability_package_ingest"
            assert agent_run["metadata"]["locale"] == "zh-CN"
            assert "source_bundle" in agent_run["metadata"]
            assert agent_run["metadata"]["model_binding"]["provider"] == "deepseek"
            assert agent_run["metadata"]["model_binding"]["model"] == "deepseek-v4-pro"
            assert len(control.list_agent_runs(agent_id="capability_packager")) == 1
            status, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "worker_kind": "sandbox_worker",
                    "executors": ["reuleauxcoder"],
                },
            )
            assert status == 200
            claim = claim_body["claim"]
            assert claim["agent_run"]["id"] == agent_run_id
            assert claim["executor_request"]["metadata"]["repo_url"] == "https://github.com/acme/example-tool"
            assert claim["executor_request"]["metadata"]["model_binding"]["provider"] == "deepseek"

            status, model_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": agent_run_id,
                    "request_id": claim["request_id"],
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "parameters": {"max_tokens": 1, "temperature": 0.9},
                    "metadata": {"sandbox_request_id": "sandbox-1"},
                    "stream": False,
                },
            )

            assert status == 200
            assert model_body["ok"] is True
            assert model_body["response"]["content"] == "hi"
            assert model_body["response"]["prompt_tokens"] == 1
            assert model_body["response"]["completion_tokens"] == 2
            assert created_configs[0].id == "deepseek"
            request = seen_requests[0]
            assert request.model == "deepseek-v4-pro"
            assert request.messages[0]["role"] == "system"
            assert "所有用户可见的生成内容都必须使用简体中文" in request.messages[0]["content"]
            assert request.messages[1:] == [{"role": "user", "content": "hello"}]
            assert request.max_tokens == 384000
            assert request.temperature == 0.2
            assert request.metadata["agent_run_id"] == agent_run_id
            assert request.metadata["worker_id"] == "worker-1"
            assert request.metadata["sandbox_request_id"] == "sandbox-1"
            assert request.metadata["locale"] == "zh-CN"
            assert any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "model_request_started"
                for event in control.list_events(agent_run_id)
            )
        finally:
            service.stop()
            relay.stop()

    def test_capability_package_session_start_reports_ingest_error_not_scope_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from http import HTTPStatus

        from labrastro_server.services.capability_packages import (
            CapabilityPackageIngestError,
        )

        def fail_start(_self, _payload, *, agent_run_metadata=None):
            raise CapabilityPackageIngestError(
                "capability_source_invalid",
                "source could not be ingested",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        monkeypatch.setattr(
            "labrastro_server.services.capability_packages.CapabilityPackageIngestService.start",
            fail_start,
        )
        config_path = tmp_path / "config.yaml"
        save_yaml_config(config_path, {"providers": {"items": {}}})
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "packager": {
                        "executor": "reuleauxcoder",
                        "execution_location": "remote_server",
                        "worker_kind": "sandbox_worker",
                        "model_request_origin": "server",
                    }
                },
                "agents": {
                    "capability_packager": {
                        "visibility": "system",
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "packager",
                    }
                },
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.start()
        try:
            bootstrap_token = relay.issue_bootstrap_token(ttl_sec=60)
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": bootstrap_token,
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                    {
                        "peer_token": register_body["payload"]["peer_token"],
                        "session_id": "session-capability-fail",
                        "client_request_id": "capability-ingest-fail",
                        "source": {
                            "type": "github_repo",
                            "url": "https://github.com/acme/broken-tool",
                        },
                    },
                    headers=TEST_ADMIN_HEADERS,
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 422
            assert body["error"] == "capability_source_invalid"
            assert body["message"] == "source could not be ingested"
            failed_session = service._get_session_run_by_request(
                register_body["payload"]["peer_id"],
                "session-capability-fail",
                "capability-ingest-fail",
            )
            assert failed_session is not None
            assert "session_run_start_failure" not in failed_session.runtime_state
            failed_events = [
                event
                for event in failed_session.events
                if event["type"] == "session_run_failed"
            ]
            assert failed_events
            failed_payload = failed_events[-1]["payload"]
            assert failed_payload["operation"] == "start"
            assert failed_payload["branch_binding_id"] == "main"
            assert failed_payload["http_status"] == 422
            assert failed_payload["code"] == "capability_source_invalid"
            assert failed_payload["message"] == "source could not be ingested"

            with pytest.raises(HTTPError) as retry_excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                    {
                        "peer_token": register_body["payload"]["peer_token"],
                        "session_id": "session-capability-fail",
                        "client_request_id": "capability-ingest-fail",
                        "source": {
                            "type": "github_repo",
                            "url": "https://github.com/acme/broken-tool",
                        },
                    },
                    headers=TEST_ADMIN_HEADERS,
                )

            retry_body = json.loads(retry_excinfo.value.read().decode("utf-8"))
            assert retry_excinfo.value.code == 422
            assert retry_body["error"] == "capability_source_invalid"
            assert retry_body["message"] == "source could not be ingested"
        finally:
            service.stop()
            relay.stop()

    def test_capability_package_session_installs_through_http_approval_reply(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(config_path, {})
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "packager": {
                        "executor": "reuleauxcoder",
                        "execution_location": "remote_server",
                        "worker_kind": "sandbox_worker",
                    }
                },
                "agents": {
                    "capability_packager": {
                        "visibility": "system",
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "packager",
                    }
                },
            }
        )
        persisted_trace_events = []

        def trace_sink(
            session_id,
            event_type,
            payload,
            session_run_id,
            session_run_seq,
            source,
            replayable,
        ):
            seq = len(persisted_trace_events) + 1
            persisted_trace_events.append(
                {
                    "session_id": session_id,
                    "type": event_type,
                    "payload": payload,
                    "session_run_id": session_run_id,
                    "session_run_seq": session_run_seq,
                    "source": source,
                    "replayable": replayable,
                }
            )
            return seq

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.set_session_trace_event_sink(trace_sink)
        service.start()

        def wait_for(predicate, *, timeout_sec: float = 3.0):
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(0.02)
            raise AssertionError("timed out waiting for condition")

        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _, ingest_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                {
                    "peer_token": peer_token,
                    "session_id": "session-capability-install",
                    "client_request_id": "capability-install-req",
                    "source": {
                        "type": "project_notes",
                        "notes": "Install gh, then use gh pr view for review.",
                    },
                },
                headers=TEST_ADMIN_HEADERS,
            )
            session_run_id = ingest_body["session_run_id"]
            agent_run_id = wait_for(
                lambda: next(
                    (
                        event["payload"].get("agent_run_id")
                        for event in service._get_session_run(session_run_id).events
                        if event["type"] == "workflow_step"
                        and isinstance(event.get("payload"), dict)
                        and event["payload"].get("agent_run_id")
                    ),
                    "",
                )
            )
            draft = {
                "id": "review",
                "name": "Review",
                "source": {"type": "project_notes"},
                "contributions": {
                    "environment_requirements": [
                        {
                            "id": "envreq:executable:gh",
                            "kind": "executable",
                            "name": "gh",
                            "command": "gh",
                            "check": "gh --version",
                        }
                    ]
                },
                "install_plan": ["Install gh."],
                "usage": ["Use gh pr view."],
                "evidence": [
                    {
                        "title": "Project notes",
                        "excerpt": "Install gh and run gh --version",
                    }
                ],
                "credentials": ["GITHUB_TOKEN"],
                "risk_level": "low",
            }
            control.complete_agent_run_activation(
                str(agent_run_id),
                ExecutorRunResult(
                    task_id=str(agent_run_id),
                    status="completed",
                    output=f"```json\n{json.dumps(draft)}\n```",
                ),
                activation_id=_current_activation_id(control, str(agent_run_id)),
            )
            approval = wait_for(
                lambda: next(
                    (
                        item
                        for item in service._get_session_run(session_run_id)
                        .status_payload(branch_binding_id=ingest_body["branch_binding_id"])
                        .get("approvals", [])
                        if item.get("decision_type") == "capability_package_install"
                    ),
                    None,
                )
            )
            assert approval["tool_name"] == "install_capability_package"
            assert approval["review"]["package_id"] == "review"

            status, reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": approval.get("branch_binding_id") or "main",
                    "approval_id": approval["approval_id"],
                    "decision": "allow_once",
                    "reason": "ok",
                },
            )
            assert status == 200
            assert reply_body["ok"] is True

            session = wait_for(
                lambda: service._get_session_run(session_run_id)
                if service._get_session_run(session_run_id).done
                else None
            )
            event_types = [event["type"] for event in session.events]
            assert "approval_resolved" in event_types
            assert any(
                event["type"] == "workflow_result"
                and event["payload"].get("result_type") == "capability_package_install"
                and event["payload"].get("status") == "done"
                for event in session.events
            )
            assert any(event["type"] == "session_run_end" for event in session.events)

            config = load_yaml_config(config_path)
            assert config["capability_packages"]["review"]["status"] == "installed"
            assert "envreq:executable:gh" in config["capability_components"]
            assert any(event["type"] == "workflow_decision" for event in persisted_trace_events)
            assert any(event["type"] == "approval_resolved" for event in persisted_trace_events)
            assert any(
                event["type"] == "workflow_result"
                and event["payload"].get("status") == "done"
                for event in persisted_trace_events
            )
            assert any(event["type"] == "session_run_end" for event in persisted_trace_events)
        finally:
            service.stop()
            relay.stop()

    def test_agent_run_model_request_non_stream_reports_provider_interruption_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeProvider:
            def chat(self, provider_request):
                partial = ProviderResponse(content="hel")
                interruption = {
                    "phase": "stream_iterate",
                    "classification": "text_interrupted",
                    "recoverable": True,
                    "partial_kind": "text",
                    "retry_action": "continue",
                    "error_type": "RemoteProtocolError",
                    "message": "incomplete chunked read",
                }
                partial.stream_status = "interrupted"
                partial.interruption = interruption
                raise ProviderStreamInterruptedError(
                    "incomplete chunked read",
                    original_error=RuntimeError("incomplete chunked read"),
                    partial_response=partial,
                    interruption=interruption,
                )

        monkeypatch.setattr(
            "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
            lambda _self, _config: FakeProvider(),
        )
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            control.submit_agent_run(
                AgentRunRequest(
                    agent_id="capability_packager",
                    source=AgentRunSource.CAPABILITY_INGEST,
                    prompt="package repo",
                    executor="reuleauxcoder",
                    execution_location="remote_server",
                    worker_kind="sandbox_worker",
                    model_request_origin="server",
                    metadata={
                        "model_binding": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        }
                    },
                ),
                task_id="task-model-interrupted",
            )
            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "worker_kind": "sandbox_worker",
                    "executors": ["reuleauxcoder"],
                },
            )
            claim = claim_body["claim"]

            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-interrupted",
                    "request_id": claim["request_id"],
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                },
            )

            assert status == 200
            assert body["ok"] is False
            assert body["error"] == "provider_stream_interrupted"
            assert body["message"] == "Provider stream interrupted."
            assert body["message_key"] == "provider_stream_interrupted.recovering"
            assert body["stream_status"] == "interrupted"
            assert body["content"] == "hel"
            assert body["response"]["content"] == "hel"
            assert body["interruption"]["classification"] == "text_interrupted"
            assert body["diagnostic_message"] == "incomplete chunked read"
        finally:
            service.stop()
            relay.stop()

    def test_agent_run_model_request_stream_sends_heartbeat_while_provider_is_idle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeProvider:
            def chat(self, provider_request):
                time.sleep(0.12)
                if provider_request.on_token is not None:
                    provider_request.on_token("hel")
                return ProviderResponse(
                    content="hello",
                    prompt_tokens=1,
                    completion_tokens=2,
                )

        monkeypatch.setattr(
            "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
            lambda _self, _config: FakeProvider(),
        )
        monkeypatch.setattr(
            "labrastro_server.interfaces.http.remote.routes.agent_runs.MODEL_REQUEST_SSE_HEARTBEAT_SEC",
            0.03,
        )
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            control.submit_agent_run(
                AgentRunRequest(
                    agent_id="capability_packager",
                    source=AgentRunSource.CAPABILITY_INGEST,
                    prompt="package repo",
                    executor="reuleauxcoder",
                    execution_location="remote_server",
                    worker_kind="sandbox_worker",
                    model_request_origin="server",
                    metadata={
                        "model_binding": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                            "parameters": {
                                "max_tokens": 384000,
                                "temperature": 0.2,
                            },
                        }
                    },
                ),
                task_id="task-model-stream",
            )
            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "worker_kind": "sandbox_worker",
                    "executors": ["reuleauxcoder"],
                },
            )
            claim = claim_body["claim"]

            status, content_type, raw_body, frames = _sse_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-stream",
                    "request_id": claim["request_id"],
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )

            assert status == 200
            assert content_type.startswith("text/event-stream")
            assert "event: heartbeat" in raw_body
            assert "heartbeat" in [frame["event"] for frame in frames]
            assert [frame["event"] for frame in frames][-2:] == ["token", "done"]
            assert frames[-2]["data"] == {"text": "hel"}
            assert frames[-1]["data"]["content"] == "hello"
            assert any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "model_request_started"
                for event in control.list_events("task-model-stream")
            )
        finally:
            service.stop()
            relay.stop()

    def test_agent_run_model_request_stream_reports_provider_interruption_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeProvider:
            def chat(self, provider_request):
                assert provider_request.on_token is not None
                provider_request.on_token("hel")
                partial = ProviderResponse(content="hel")
                interruption = {
                    "phase": "stream_iterate",
                    "classification": "text_interrupted",
                    "recoverable": True,
                    "partial_kind": "text",
                    "retry_action": "continue",
                    "error_type": "RemoteProtocolError",
                    "message": "incomplete chunked read",
                }
                partial.stream_status = "interrupted"
                partial.interruption = interruption
                raise ProviderStreamInterruptedError(
                    "incomplete chunked read",
                    original_error=RuntimeError("incomplete chunked read"),
                    partial_response=partial,
                    interruption=interruption,
                )

        monkeypatch.setattr(
            "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
            lambda _self, _config: FakeProvider(),
        )
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            control.submit_agent_run(
                AgentRunRequest(
                    agent_id="capability_packager",
                    source=AgentRunSource.CAPABILITY_INGEST,
                    prompt="package repo",
                    executor="reuleauxcoder",
                    execution_location="remote_server",
                    worker_kind="sandbox_worker",
                    model_request_origin="server",
                    metadata={
                        "model_binding": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        }
                    },
                ),
                task_id="task-model-interrupted",
            )
            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "worker_kind": "sandbox_worker",
                    "executors": ["reuleauxcoder"],
                },
            )
            claim = claim_body["claim"]

            status, content_type, _raw_body, frames = _sse_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-interrupted",
                    "request_id": claim["request_id"],
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )

            assert status == 200
            assert content_type.startswith("text/event-stream")
            assert [frame["event"] for frame in frames] == ["token", "interrupted"]
            assert frames[0]["data"] == {"text": "hel"}
            interrupted = frames[1]["data"]
            assert interrupted["content"] == "hel"
            assert interrupted["stream_status"] == "interrupted"
            assert interrupted["message"] == "Provider stream interrupted."
            assert interrupted["message_key"] == "provider_stream_interrupted.recovering"
            assert interrupted["error"] == "provider_stream_interrupted"
            assert interrupted["interruption"]["classification"] == "text_interrupted"
            assert interrupted["diagnostic_message"] == "incomplete chunked read"
        finally:
            service.stop()
            relay.stop()

    def test_agent_run_model_request_stream_stops_callbacks_after_client_disconnect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        callback_stopped = threading.Event()

        class FakeProvider:
            def chat(self, provider_request):
                assert provider_request.on_token is not None
                provider_request.on_token("first")
                deadline = time.time() + 2
                while time.time() < deadline:
                    try:
                        provider_request.on_token("late")
                    except (BrokenPipeError, ConnectionResetError):
                        callback_stopped.set()
                        raise
                    time.sleep(0.02)
                return ProviderResponse(
                    content="late",
                    prompt_tokens=1,
                    completion_tokens=2,
                )

        monkeypatch.setattr(
            "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
            lambda _self, _config: FakeProvider(),
        )
        monkeypatch.setattr(
            "labrastro_server.interfaces.http.remote.routes.agent_runs.MODEL_REQUEST_SSE_HEARTBEAT_SEC",
            0.03,
        )
        monkeypatch.setattr(
            "labrastro_server.interfaces.http.remote.routes.agent_runs.MODEL_REQUEST_SSE_QUEUE_MAX_SIZE",
            2,
        )
        config_path = tmp_path / "config.yaml"
        save_yaml_config(
            config_path,
            {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-test",
                        }
                    }
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.start()
        sock: socket.socket | None = None
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            control.submit_agent_run(
                AgentRunRequest(
                    agent_id="capability_packager",
                    source=AgentRunSource.CAPABILITY_INGEST,
                    prompt="package repo",
                    executor="reuleauxcoder",
                    execution_location="remote_server",
                    worker_kind="sandbox_worker",
                    model_request_origin="server",
                    metadata={
                        "model_binding": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        }
                    },
                ),
                task_id="task-model-disconnect",
            )
            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "worker_kind": "sandbox_worker",
                    "executors": ["reuleauxcoder"],
                },
            )
            claim = claim_body["claim"]
            payload = json.dumps(
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-disconnect",
                    "request_id": claim["request_id"],
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            ).encode("utf-8")
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.settimeout(5)
            sock.sendall(
                b"POST /remote/agent-run-activations/model-request HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Accept: text/event-stream\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            body = b""
            while b"event: token" not in body:
                chunk = sock.recv(1024)
                assert chunk
                body += chunk
            sock.close()
            sock = None

            assert callback_stopped.wait(3)
        finally:
            if sock is not None:
                sock.close()
            service.stop()
            relay.stop()

    def test_capability_package_session_survives_peer_shutdown_and_recovers_by_status(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        save_yaml_config(config_path, {})
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "packager": {
                        "executor": "fake",
                        "execution_location": "remote_server",
                        "worker_kind": "sandbox_worker",
                    }
                },
                "agents": {
                    "capability_packager": {
                        "visibility": "system",
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "packager",
                    }
                },
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            old_peer_token = register_body["payload"]["peer_token"]
            _, ingest_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                {
                    "peer_token": old_peer_token,
                    "session_id": "session-capability-recover",
                    "client_request_id": "capability-recover-req",
                    "source": {
                        "type": "project_notes",
                        "notes": "Install gh, then use gh pr view.",
                    },
                },
                headers=TEST_ADMIN_HEADERS,
            )
            session_run_id = ingest_body["session_run_id"]
            branch_binding_id = ingest_body["branch_binding_id"]
            agent_run_id = ""
            deadline = time.time() + 3
            while time.time() < deadline and not agent_run_id:
                session = service._get_session_run(session_run_id)
                assert session is not None
                for event in session.events:
                    payload = event.get("payload")
                    if isinstance(payload, dict) and payload.get("agent_run_id"):
                        agent_run_id = payload["agent_run_id"]
                        break
                if not agent_run_id:
                    time.sleep(0.02)
            assert agent_run_id

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": old_peer_token, "reason": "peer_shutdown"},
            )
            assert status == 200
            assert control.agent_run_to_dict(agent_run_id)["status"] == "queued"

            _, reconnect_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp/peer",
                    "features": ["agent_runs", "worker_kind:sandbox_worker"],
                },
            )
            new_peer_token = reconnect_body["payload"]["peer_token"]
            status, status_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": new_peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": branch_binding_id,
                    "cursor": 0,
                },
            )
            assert status == 200
            assert status_body["session_run_id"] == session_run_id
            assert status_body["session_id"] == "session-capability-recover"
            assert status_body["mode"] == "capability_package"
            assert status_body["workflow_mode"] == "capability_package_ingest"
            assert status_body["runtime_state"]["agent_id"] == "capability_packager"
            assert status_body["running"] is True
            assert status_body["done"] is False

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/cancel",
                {
                    "peer_token": new_peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": status_body["branch_binding_id"],
                    "reason": "user_cancelled",
                },
            )
            assert status == 200
            deadline = time.time() + 3
            while time.time() < deadline:
                if control.agent_run_to_dict(agent_run_id)["status"] == "cancelled":
                    break
                time.sleep(0.02)
            assert control.agent_run_to_dict(agent_run_id)["status"] == "cancelled"
        finally:
            service.stop()
            relay.stop()

    def test_register_rejected_over_http(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            req = request.Request(
                f"{service.base_url}/remote/register",
                data=json.dumps(
                    {"bootstrap_token": "bt_invalid", "cwd": "/tmp"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 403
                body = json.loads(exc.read().decode("utf-8"))
                assert body["ok"] is False
                assert body["error"] == "register_rejected"
                assert body["details"]["reason"]
        finally:
            service.stop()
            relay.stop()

    def test_chat_command_endpoint_routes_to_host_command_handler(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        seen: dict[str, object] = {}

        def chat_command_handler(
            peer_id: str, req: ChatCommandDispatchRequest
        ) -> ChatCommandDispatchResponse:
            seen["peer_id"] = peer_id
            seen["command_text"] = req.command_text
            seen["command_id"] = req.command_id
            seen["session_hint"] = req.session_hint
            seen["mentions"] = req.mentions
            return ChatCommandDispatchResponse(
                ok=True,
                action="continue",
                session_id="session-1",
                events=[
                    {
                        "type": "output",
                        "payload": {
                            "format": "plain",
                            "content": f"{peer_id}:{req.command_text}",
                        },
                    }
                ],
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_command_handler=chat_command_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(
                    relay,
                    cwd="/tmp/peer-chat",
                    workspace_root="/tmp/peer-chat",
                    features=[
                        "shell",
                        "agent_runs.local_workspace",
                        "worker_kind:local_peer",
                    ],
                ),
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            status, command_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/command",
                {
                    "peer_token": peer_token,
                    "text": "/help",
                    "command_id": "system.help",
                    "trigger": "/help",
                    "session_hint": "session-1",
                    "client_request_id": "req-1",
                    "mentions": [{"kind": "file", "path": "README.md"}],
                },
            )

            assert status == 200
            assert command_body == {
                "ok": True,
                "action": "continue",
                "session_id": "session-1",
                "events": [
                    {
                        "type": "output",
                        "payload": {
                            "format": "plain",
                            "content": f"{peer_id}:/help",
                        },
                    }
                ],
            }
            assert seen == {
                "peer_id": peer_id,
                "command_text": "/help",
                "command_id": "system.help",
                "session_hint": "session-1",
                "mentions": [{"kind": "file", "path": "README.md"}],
            }
        finally:
            service.stop()
            relay.stop()

    def test_chat_command_endpoint_rejects_non_slash_text(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_command_handler=lambda _peer_id, _req: ChatCommandDispatchResponse(
                ok=True
            ),
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(
                    relay,
                    cwd="/tmp/peer-chat",
                    workspace_root="/tmp/peer-chat",
                    features=[
                        "shell",
                        "agent_runs.local_workspace",
                        "worker_kind:local_peer",
                    ],
                ),
            )

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/chat/command",
                    {
                        "peer_token": register_body["payload"]["peer_token"],
                        "text": "hello",
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "invalid_chat_command"
            assert "slash command" in body["message"]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_creates_bound_agent_run_mainline(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(
                    relay,
                    cwd="/tmp/peer-chat",
                    workspace_root="/tmp/peer-chat",
                    features=[
                        "shell",
                        "agent_runs.local_workspace",
                        "worker_kind:local_peer",
                    ],
                ),
            )
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]

            status, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {
                    "peer_token": peer_token,
                    "prompt": "first",
                    "session_hint": "chat-session-1",
                    "client_request_id": "start-1",
                },
            )

            assert status == 200
            session_run_id = start_body["session_run_id"]
            agent_run_id = start_body["agent_run_id"]
            assert start_body["branch_binding_id"] == "main"
            assert start_body["scope_id"] == f"{session_run_id}:main"
            assert start_body["selected"] is True
            assert start_body["activation_id"] == f"{agent_run_id}:activation:1"
            binding = control.find_session_run_binding(session_run_id=session_run_id)
            assert binding is not None
            assert binding.agent_run_id == agent_run_id
            assert binding.peer_id == peer_id
            run = control.get_agent_run(agent_run_id)
            assert run.owner_session_run_id == session_run_id
            assert run.source.value == "chat"
            assert run.trigger_mode.value == "interactive_chat"
            assert run.execution_location == ExecutionLocation.REMOTE_SERVER
            assert run.metadata.get("worker_kind") in (
                None,
                WorkerKind.SERVER_WORKER.value,
                "server_worker",
            )
            assert run.metadata["model_request_origin"] == ModelRequestOrigin.SERVER.value
            local_claim_status, local_claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "peer-chat-worker",
                    "worker_kind": "local_peer",
                    "executors": ["reuleauxcoder"],
                },
            )
            assert local_claim_status == 200
            assert local_claim_body["claim"] is None
            assert "workspace_root" not in run.metadata
            assert "cwd" not in run.metadata
            server_claim = control.claim_agent_run_activation(
                worker_id="server-chat-worker",
                worker_kind=WorkerKind.SERVER_WORKER,
                executors=["reuleauxcoder"],
                peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
            )
            assert server_claim is not None
            assert server_claim.task.id == agent_run_id
            assert server_claim.executor_request.execution_location == (
                ExecutionLocation.REMOTE_SERVER
            )
            assert server_claim.executor_request.worker_kind == WorkerKind.SERVER_WORKER
            assert server_claim.executor_request.model_request_origin == (
                ModelRequestOrigin.SERVER
            )
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_idempotent_response_fails_closed_when_agent_run_missing(
        self,
        monkeypatch,
    ) -> None:
        relay, service, control, register_body, _start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]

            def missing_agent_run(_agent_run_id: str):
                raise KeyError(_agent_run_id)

            monkeypatch.setattr(control, "get_agent_run", missing_agent_run)

            with pytest.raises(HTTPError) as raised:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/start",
                    {
                        "peer_token": peer_token,
                        "prompt": "first",
                        "session_hint": "chat-session-steer",
                        "client_request_id": "start-steer-1",
                    },
                )

            assert raised.value.code == 404
            body = json.loads(raised.value.read().decode("utf-8"))
            assert body["error"] == "agent_run_not_found"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_idempotent_response_keeps_main_after_branch_select(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            main_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch first",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-start-idempotent-branch",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-a",
                selected=False,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=main_agent_run_id,
                target_agent_run_id=branch.id,
            )

            select_status, select_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/branches/select",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                },
            )
            assert select_status == 200
            assert select_body["branch_binding_id"] == "branch-a"

            replay_status, replay_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {
                    "peer_token": peer_token,
                    "prompt": "first",
                    "session_hint": "chat-session-steer",
                    "client_request_id": "start-steer-1",
                },
            )

            assert replay_status == 200
            assert replay_body["session_run_id"] == session_run_id
            assert replay_body["agent_run_id"] == main_agent_run_id
            assert replay_body["branch_binding_id"] == "main"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_continue_uses_bound_agent_run_mainline(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {
                    "peer_token": peer_token,
                    "prompt": "first",
                    "session_hint": "chat-session-1",
                    "client_request_id": "start-1",
                },
            )
            session_run_id = start_body["session_run_id"]
            agent_run_id = start_body["agent_run_id"]
            control.complete_agent_run_activation(
                agent_run_id,
                ExecutorRunResult(
                    task_id=agent_run_id,
                    status="completed",
                    output="first done",
                ),
                activation_id=start_body["activation_id"],
            )

            status, continue_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/continue",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "main",
                    "prompt": "second",
                    "client_request_id": "continue-1",
                },
            )

            assert status == 200
            assert continue_body["ok"] is True
            assert continue_body["session_run_id"] == session_run_id
            assert continue_body["agent_run_id"] == agent_run_id
            assert continue_body["branch_binding_id"] == "main"
            assert continue_body["scope_id"] == f"{session_run_id}:main"
            assert continue_body["selected"] is True
            assert continue_body["activation_id"] == f"{agent_run_id}:activation:2"
            assert [item["id"] for item in control.list_agent_runs()] == [agent_run_id]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_routes_report_unavailable_projection_when_binding_persists(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            session_run_id = start_body["session_run_id"]
            assert control.find_session_run_binding(session_run_id=session_run_id) is not None
            with service._session_runs_lock:
                service._session_runs.pop(session_run_id, None)

            requests = [
                (
                    "/remote/session-runs/continue",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "prompt": "second",
                    },
                ),
                (
                    "/remote/session-runs/events",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "cursor": 0,
                        "timeout_sec": 0.05,
                    },
                ),
                (
                    "/remote/session-runs/status",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                    },
                ),
                (
                    "/remote/session-runs/recover",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "action": "continue",
                    },
                ),
                (
                    "/remote/session-runs/cancel",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "reason": "user_cancelled",
                    },
                ),
                (
                    "/remote/session-runs/branches/select",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "cursor": 0,
                    },
                ),
                (
                    "/remote/session-runs/user-input/reply",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "input_id": "input-1",
                        "action": "submit",
                        "content": {"text": "answer"},
                    },
                ),
                (
                    "/remote/approval/reply",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "approval_id": "approval-1",
                        "decision": "deny_once",
                    },
                ),
            ]

            for path, payload in requests:
                with pytest.raises(HTTPError) as excinfo:
                    _json_request(
                        "POST",
                        f"{service.base_url}{path}",
                        payload,
                    )

                body = json.loads(excinfo.value.read().decode("utf-8"))
                assert excinfo.value.code == 409, path
                assert body["error"] == "session_run_projection_unavailable", path
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_routes_report_binding_store_unavailable_when_binding_lookup_fails(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            session_run_id = start_body["session_run_id"]
            with service._session_runs_lock:
                service._session_runs.pop(session_run_id, None)

            def fail_binding_lookup(*_args, **_kwargs):
                raise RuntimeError("binding store unavailable")

            control.list_session_run_bindings = fail_binding_lookup  # type: ignore[method-assign]
            control.find_session_run_binding = fail_binding_lookup  # type: ignore[method-assign]

            requests = [
                (
                    "/remote/session-runs/continue",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "prompt": "second",
                    },
                ),
                (
                    "/remote/session-runs/events",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "cursor": 0,
                        "timeout_sec": 0.05,
                    },
                ),
                (
                    "/remote/session-runs/status",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                    },
                ),
                (
                    "/remote/session-runs/recover",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "action": "continue",
                    },
                ),
                (
                    "/remote/session-runs/cancel",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "reason": "user_cancelled",
                    },
                ),
                (
                    "/remote/session-runs/branches/select",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "cursor": 0,
                    },
                ),
                (
                    "/remote/session-runs/user-input/reply",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "input_id": "input-1",
                        "action": "submit",
                        "content": {"text": "answer"},
                    },
                ),
                (
                    "/remote/approval/reply",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                        "approval_id": "approval-1",
                        "decision": "deny_once",
                    },
                ),
            ]

            for path, payload in requests:
                with pytest.raises(HTTPError) as excinfo:
                    _json_request(
                        "POST",
                        f"{service.base_url}{path}",
                        payload,
                    )

                body = json.loads(excinfo.value.read().decode("utf-8"))
                assert excinfo.value.code == 503, path
                assert body["error"] == "session_run_binding_store_unavailable", path
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_missing_projection_reports_requested_branch_not_found(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            session_run_id = start_body["session_run_id"]
            assert control.find_session_run_binding(session_run_id=session_run_id) is not None
            with service._session_runs_lock:
                service._session_runs.pop(session_run_id, None)

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/status",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "branch-missing",
                    },
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 404
            assert body["error"] == "session_run_branch_binding_not_found"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_missing_projection_reports_binding_api_unavailable(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            session_run_id = start_body["session_run_id"]
            assert control.find_session_run_binding(session_run_id=session_run_id) is not None
            with service._session_runs_lock:
                service._session_runs.pop(session_run_id, None)
            control.find_session_run_binding = None  # type: ignore[method-assign]

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/status",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "main",
                    },
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 503
            assert body["error"] == "session_run_bindings_unavailable"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_missing_projection_reports_requested_branch_peer_mismatch(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            session_run_id = start_body["session_run_id"]
            _, other_peer = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(
                    relay,
                    features=["agent_runs", "agent_runs.local_workspace"],
                ),
            )
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="other peer branch",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-other-peer-branch",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=other_peer["payload"]["peer_id"],
                agent_run_id=branch.id,
                branch_binding_id="branch-other-peer",
                selected=False,
                parent_branch_binding_id="main",
                source_agent_run_id=start_body["agent_run_id"],
                target_agent_run_id=branch.id,
            )
            with service._session_runs_lock:
                service._session_runs.pop(session_run_id, None)

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/status",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "branch_binding_id": "branch-other-peer",
                    },
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 403
            assert body["error"] == "session_run_binding_peer_mismatch"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_continue_requires_branch_binding_id(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]
            session = service._create_session_run(peer_id, "chat-session-1")

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/continue",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session.session_run_id,
                        "prompt": "second",
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "branch_binding_id_required"
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.parametrize("path", ["/remote/session-runs/events", "/remote/session-runs/status"])
    def test_session_run_ui_read_routes_require_branch_scope_proof(self, path: str) -> None:
        relay, service, _control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}{path}",
                    {
                        "peer_token": peer_token,
                        "session_run_id": start_body["session_run_id"],
                        "cursor": 0,
                        "timeout_sec": 0.01,
                    },
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "branch_binding_id_required"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_status_unknown_branch_requires_binding_error(self) -> None:
        relay, service, _control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/status",
                    {
                        "peer_token": peer_token,
                        "session_run_id": start_body["session_run_id"],
                        "branch_binding_id": "branch-missing",
                    },
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 404
            assert body["error"] == "session_run_branch_binding_not_found"
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.parametrize(
        "path",
        [
            "/remote/session-runs/events",
            "/remote/session-runs/status",
            "/remote/session-runs/cancel",
            "/remote/session-runs/recover",
            "/remote/approval/reply",
            "/remote/session-runs/user-input/reply",
        ],
    )
    def test_session_run_scoped_routes_require_session_run_id(
        self,
        path: str,
    ) -> None:
        relay, service, _control, register_body, _start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            payload: dict[str, object] = {
                "peer_token": peer_token,
                "branch_binding_id": "main",
                "cursor": 0,
                "timeout_sec": 0.01,
                "reason": "user_cancelled",
                "action": "continue",
                "approval_id": "approval-1",
                "decision": "deny_once",
                "input_id": "input-1",
            }

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}{path}",
                    payload,
                )

            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "session_run_id_required"
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.parametrize(
        ("path", "extra_payload"),
        [
            ("/remote/session-runs/cancel", {"reason": "user_cancelled"}),
            ("/remote/session-runs/recover", {"action": "continue"}),
            (
                "/remote/approval/reply",
                {
                    "approval_id": "approval-1",
                    "decision": "deny_once",
                },
            ),
            (
                "/remote/session-runs/user-input/reply",
                {
                    "input_id": "input-1",
                    "action": "decline",
                },
            ),
        ],
    )
    def test_session_run_control_requests_require_branch_binding_id(
        self,
        path: str,
        extra_payload: dict[str, object],
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=AgentRunControlPlane(),
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]
            session = service._create_session_run(peer_id, "chat-session-1")

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}{path}",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session.session_run_id,
                        **extra_payload,
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "branch_binding_id_required"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_continue_targets_explicit_non_selected_branch(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            main_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch first",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-explicit-branch",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-a",
                selected=False,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=main_agent_run_id,
                target_agent_run_id=branch.id,
            )
            control.complete_agent_run_activation(
                branch.id,
                ExecutorRunResult(
                    task_id=branch.id,
                    status="completed",
                    output="branch done",
                ),
                activation_id=_current_activation_id(control, branch.id),
            )

            status, continue_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/continue",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                    "prompt": "branch second",
                    "client_request_id": "continue-branch-a",
                },
            )

            assert status == 200
            assert continue_body["ok"] is True
            assert continue_body["agent_run_id"] == branch.id
            assert continue_body["activation_id"] == f"{branch.id}:activation:2"
            selected = control.find_session_run_binding(session_run_id=session_run_id)
            assert selected is not None
            assert selected.branch_binding_id == "main"

            _, branch_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                },
            )
            assert branch_status["agent_run_id"] == branch.id
            assert branch_status["branch_binding_id"] == "branch-a"
            assert branch_status["scope_id"] == f"{session_run_id}:branch-a"
            assert branch_status["selected"] is False
            selected_status_code, selected_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "main",
                },
            )
            active_session = service._get_session_run(session_run_id)
            assert selected_status_code == 200
            assert selected_status["agent_run_id"] == main_agent_run_id
            assert selected_status["branch_binding_id"] == "main"
            assert selected_status["scope_id"] == f"{session_run_id}:main"
            assert selected_status["selected"] is True
            assert active_session is not None
            assert active_session.agent_run_id == main_agent_run_id
            assert active_session.branch_binding_id == "main"
            assert active_session.runtime_state.get("agent_run_id") != branch.id
            assert active_session.runtime_state.get("branch_binding_id") != "branch-a"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_recover_targets_explicit_non_selected_branch(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            main_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch first",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-recover-branch",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-a",
                selected=False,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=main_agent_run_id,
                target_agent_run_id=branch.id,
            )
            control.complete_agent_run_activation(
                branch.id,
                ExecutorRunResult(
                    task_id=branch.id,
                    status="completed",
                    output="branch done",
                ),
                activation_id=_current_activation_id(control, branch.id),
            )
            active_session = service._get_session_run(session_run_id)
            assert active_session is not None
            active_session.sync_branch_bindings(
                control.list_session_run_bindings(session_run_id=session_run_id)
            )
            active_session.register_recovery(
                {
                    "branch_binding_id": "branch-a",
                    "response": "partial branch response",
                    "recovery_actions": ["continue"],
                }
            )

            status, recover_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/recover",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                    "action": "continue",
                },
            )

            assert status == 200
            assert recover_body["ok"] is True
            selected = control.find_session_run_binding(session_run_id=session_run_id)
            assert selected is not None
            assert selected.branch_binding_id == "main"
            assert active_session.agent_run_id == main_agent_run_id
            assert active_session.branch_binding_id == "main"
            assert active_session.runtime_state.get("agent_run_id") != branch.id
            assert active_session.runtime_state.get("branch_binding_id") != "branch-a"

            _, branch_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                },
            )
            assert branch_status["agent_run_id"] == branch.id
            assert branch_status["branch_binding_id"] == "branch-a"
            assert branch_status["scope_id"] == f"{session_run_id}:branch-a"
            assert branch_status["selected"] is False
        finally:
            service.stop()
            relay.stop()

    def test_session_run_recover_after_selecting_finished_branch_uses_selected_scope_status(
        self,
    ) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            main_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch first",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-recover-selected-finished-branch",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-a",
                selected=False,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=main_agent_run_id,
                target_agent_run_id=branch.id,
            )
            control.complete_agent_run_activation(
                branch.id,
                ExecutorRunResult(
                    task_id=branch.id,
                    status="completed",
                    output="branch done",
                ),
                activation_id=_current_activation_id(control, branch.id),
            )
            active_session = service._get_session_run(session_run_id)
            assert active_session is not None
            active_session.sync_branch_bindings(
                control.list_session_run_bindings(session_run_id=session_run_id)
            )
            active_session.register_recovery(
                {
                    "branch_binding_id": "branch-a",
                    "response": "partial branch response",
                    "recovery_actions": ["continue"],
                }
            )

            select_status, _select_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/branches/select",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                },
            )
            assert select_status == 200

            status, recover_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/recover",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                    "action": "continue",
                },
            )

            assert status == 200
            assert recover_body["ok"] is True
            assert recover_body["branch_binding_id"] == "branch-a"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_recover_uses_bound_agent_run_status_for_selected_binding(
        self,
    ) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            main_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch first",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-recover-selected-binding-runtime",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-a",
                selected=True,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=main_agent_run_id,
                target_agent_run_id=branch.id,
            )
            control.complete_agent_run_activation(
                branch.id,
                ExecutorRunResult(
                    task_id=branch.id,
                    status="completed",
                    output="branch done",
                ),
                activation_id=_current_activation_id(control, branch.id),
            )
            active_session = service._get_session_run(session_run_id)
            assert active_session is not None
            assert active_session.running is True
            active_session.sync_branch_bindings(
                control.list_session_run_bindings(session_run_id=session_run_id)
            )
            active_session.register_recovery(
                {
                    "branch_binding_id": "branch-a",
                    "response": "partial branch response",
                    "recovery_actions": ["continue"],
                }
            )

            status, recover_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/recover",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                    "action": "continue",
                },
            )

            assert status == 200
            assert recover_body["ok"] is True
            assert recover_body["branch_binding_id"] == "branch-a"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_cancel_targets_explicit_non_selected_branch_without_polluting_selected_state(
        self,
    ) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            session_run_id = start_body["session_run_id"]
            main_agent_run_id = start_body["agent_run_id"]
            branch = control.submit_agent_run(
                AgentRunRequest(
                    agent_id="chat",
                    prompt="branch first",
                    owner_session_run_id=session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                ),
                task_id="agent-run-cancel-branch",
            )
            control.create_session_run_binding(
                session_run_id=session_run_id,
                session_id=start_body.get("session_id") or "chat-session-steer",
                peer_id=peer_id,
                agent_run_id=branch.id,
                branch_binding_id="branch-a",
                selected=False,
                parent_branch_binding_id="main",
                base_session_item_id="msg-1",
                source_agent_run_id=main_agent_run_id,
                target_agent_run_id=branch.id,
            )
            active_session = service._get_session_run(session_run_id)
            assert active_session is not None

            status, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/cancel",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "branch_binding_id": "branch-a",
                    "reason": "cancel-branch-a",
                },
            )

            assert status == 200
            assert cancel_body["ok"] is True
            assert control.get_agent_run(branch.id).status.value == "cancelled"
            selected = control.find_session_run_binding(session_run_id=session_run_id)
            assert selected is not None
            assert selected.branch_binding_id == "main"
            assert active_session.agent_run_id == main_agent_run_id
            assert active_session.branch_binding_id == "main"
            assert active_session.status != "cancelled"
            assert active_session.last_error is None
            branches = {
                item["branch_binding_id"]: item
                for item in active_session.branch_summaries(0)
            }
            assert branches["branch-a"]["status"] == "cancelled"
            assert branches["main"]["status"] != "cancelled"
            assert any(
                event["type"] == "session_run_cancelled"
                and event["payload"].get("branch_binding_id") == "branch-a"
                for event in active_session.events
            )
        finally:
            service.stop()
            relay.stop()

    def test_session_run_status_reads_bound_agent_run_status(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]
            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "first"},
            )
            agent_run_id = start_body["agent_run_id"]

            status, status_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": peer_token,
                    "session_run_id": start_body["session_run_id"],
                    "branch_binding_id": "main",
                },
            )

            assert status == 200
            assert status_body["status"] == "queued"
            assert status_body["running"] is True
            assert status_body["agent_run_id"] == agent_run_id
            assert status_body["branch_binding_id"] == "main"
            assert status_body["scope_id"] == f"{start_body['session_run_id']}:main"
            assert status_body["selected"] is True

            control.complete_agent_run_activation(
                agent_run_id,
                ExecutorRunResult(
                    task_id=agent_run_id,
                    status="completed",
                    output="done",
                ),
                activation_id=start_body["activation_id"],
            )
            _, completed_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": peer_token,
                    "session_run_id": start_body["session_run_id"],
                    "branch_binding_id": "main",
                },
            )
            assert completed_status["status"] == "done"
            assert completed_status["running"] is False
            assert completed_status["done"] is True
            assert completed_status["runtime_state"]["agent_run_status"] == "completed"
            completed_branches = {
                branch["branch_binding_id"]: branch
                for branch in completed_status["branches"]
            }
            assert completed_branches["main"]["status"] == "done"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_events_project_bound_agent_terminal_status_to_branch_summary(
        self,
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]
            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "first"},
            )
            agent_run_id = start_body["agent_run_id"]

            control.complete_agent_run_activation(
                agent_run_id,
                ExecutorRunResult(
                    task_id=agent_run_id,
                    status="failed",
                    output="",
                    error="failed branch",
                ),
                activation_id=start_body["activation_id"],
            )
            status, content_type, frame = _sse_first_frame_request(
                "POST",
                f"{service.base_url}/remote/session-runs/events",
                {
                    "peer_token": peer_token,
                    "session_run_id": start_body["session_run_id"],
                    "branch_binding_id": "main",
                    "cursor": 999,
                    "timeout_sec": 0.05,
                },
            )

            assert status == 200
            assert content_type.startswith("text/event-stream")
            assert frame["data"]["done"] is True
            branches = {
                branch["branch_binding_id"]: branch
                for branch in frame["data"]["branches"]
            }
            assert branches["main"]["status"] == "error"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_cancel_targets_bound_agent_run(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]
            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "first"},
            )
            agent_run_id = start_body["agent_run_id"]

            status, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/cancel",
                {
                    "peer_token": peer_token,
                    "session_run_id": start_body["session_run_id"],
                    "branch_binding_id": "main",
                    "reason": "user_cancelled",
                },
            )

            assert status == 200
            assert cancel_body["ok"] is True
            assert control.get_agent_run(agent_run_id).status.value == "cancelled"
        finally:
            service.stop()
            relay.stop()

    def test_user_agent_run_steer_accepts_and_deduplicates_bound_activation(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            agent_run_id = start_body["agent_run_id"]
            claim = self._claim_bound_agent_run_server_worker(control)
            self._mark_claim_running(control, peer_id, claim)

            steer_payload = {
                "peer_token": peer_token,
                "session_run_id": start_body["session_run_id"],
                "branch_binding_id": "main",
                "activation_id": start_body["activation_id"],
                "idempotency_key": "user-steer-1",
                "payload": {
                    "items": [{"type": "text", "text": "add context"}],
                },
            }
            status, steer_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-runs/{agent_run_id}/steer",
                steer_payload,
            )

            assert status == 200
            assert steer_body["ok"] is True
            assert steer_body["status"] == "accepted"
            steer = steer_body["activation_steer"]
            assert steer["activation_id"] == start_body["activation_id"]
            assert steer["source"] == "user"
            assert steer["status"] == "queued"
            assert steer["metadata"]["idempotency_key"] == "user-steer-1"
            assert steer["metadata"]["sender"] == f"peer:{peer_id}"
            assert steer["metadata"]["peer_id"] == peer_id
            assert steer["metadata"]["session_run_id"] == start_body["session_run_id"]
            assert steer["metadata"]["branch_binding_id"] == "main"
            assert (
                steer["metadata"]["expected_activation_id"]
                == start_body["activation_id"]
            )

            _, duplicate_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-runs/{agent_run_id}/steer",
                steer_payload,
            )

            assert duplicate_body["ok"] is True
            assert duplicate_body["status"] == "duplicate"
            assert duplicate_body["activation_steer"]["id"] == steer["id"]
            assert len(control.load_agent_run_detail(agent_run_id)["activation_steers"]) == 1
        finally:
            service.stop()
            relay.stop()

    def test_user_agent_run_steer_requires_branch_binding_id(self) -> None:
        relay, service, _control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/{start_body['agent_run_id']}/steer",
                    {
                        "peer_token": peer_token,
                        "session_run_id": start_body["session_run_id"],
                        "activation_id": start_body["activation_id"],
                        "idempotency_key": "user-steer-missing-branch",
                        "payload": {"items": [{"type": "text", "text": "missing branch"}]},
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "branch_binding_id_required"
        finally:
            service.stop()
            relay.stop()

    def test_user_agent_run_steer_requires_session_run_id(self) -> None:
        relay, service, _control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/{start_body['agent_run_id']}/steer",
                    {
                        "peer_token": peer_token,
                        "branch_binding_id": "main",
                        "activation_id": start_body["activation_id"],
                        "idempotency_key": "user-steer-missing-session",
                        "payload": {"items": [{"type": "text", "text": "missing session"}]},
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 400
            assert body["error"] == "session_run_id_required"
        finally:
            service.stop()
            relay.stop()

    def test_user_agent_run_steer_requires_active_claim_and_matching_activation(self) -> None:
        relay, service, control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            peer_id = register_body["payload"]["peer_id"]
            agent_run_id = start_body["agent_run_id"]

            with pytest.raises(HTTPError) as no_claim:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/{agent_run_id}/steer",
                    {
                        "peer_token": peer_token,
                        "session_run_id": start_body["session_run_id"],
                        "branch_binding_id": "main",
                        "activation_id": start_body["activation_id"],
                        "idempotency_key": "user-steer-too-early",
                        "payload": {"items": [{"type": "text", "text": "too early"}]},
                    },
                )
            no_claim_body = json.loads(no_claim.value.read().decode("utf-8"))
            assert no_claim.value.code == 409
            assert no_claim_body["error"] == "agent_run_not_steerable"

            claim = self._claim_bound_agent_run_server_worker(control)
            self._mark_claim_running(control, peer_id, claim)
            with pytest.raises(HTTPError) as mismatch:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/{agent_run_id}/steer",
                    {
                        "peer_token": peer_token,
                        "session_run_id": start_body["session_run_id"],
                        "branch_binding_id": "main",
                        "activation_id": "stale-activation",
                        "idempotency_key": "user-steer-mismatch",
                        "payload": {"items": [{"type": "text", "text": "wrong"}]},
                    },
                )
            mismatch_body = json.loads(mismatch.value.read().decode("utf-8"))
            assert mismatch.value.code == 409
            assert mismatch_body["error"] == "activation_mismatch"
        finally:
            service.stop()
            relay.stop()

    def test_user_agent_run_steer_forbids_other_peer_session_binding(self) -> None:
        relay, service, _control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            _, other_register = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(
                    relay,
                    cwd="/tmp/other-peer",
                    features=["agent_runs", "agent_runs.local_workspace"],
                ),
            )

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/{start_body['agent_run_id']}/steer",
                    {
                        "peer_token": other_register["payload"]["peer_token"],
                        "session_run_id": start_body["session_run_id"],
                        "branch_binding_id": "main",
                        "activation_id": start_body["activation_id"],
                        "idempotency_key": "user-steer-other-peer",
                        "payload": {"items": [{"type": "text", "text": "cross"}]},
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 403
            assert body["error"] == "forbidden"
        finally:
            service.stop()
            relay.stop()

    def test_user_agent_run_steer_requires_session_run_binding(self) -> None:
        relay, service, _control, register_body, start_body = (
            self._start_bound_agent_run_session()
        )
        try:
            peer_token = register_body["payload"]["peer_token"]
            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/{start_body['agent_run_id']}/steer",
                    {
                        "peer_token": peer_token,
                        "session_run_id": "missing-session-run",
                        "branch_binding_id": "main",
                        "activation_id": start_body["activation_id"],
                        "idempotency_key": "user-steer-missing-binding",
                        "payload": {"items": [{"type": "text", "text": "missing"}]},
                    },
                )
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert excinfo.value.code == 403
            assert body["error"] == "forbidden"
        finally:
            service.stop()
            relay.stop()

    def test_session_protocol_models_roundtrip(self) -> None:
        assert SessionRunStartRequest.from_dict(
            {
                "peer_token": "peer-token",
                "prompt": "hello",
                "session_hint": "session-1",
                "mode": "planner",
                "client_request_id": "req-1",
                "locale": "zh-CN",
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "prompt": "hello",
            "session_hint": "session-1",
            "mode": "planner",
            "client_request_id": "req-1",
            "locale": "zh-CN",
        }
        assert SessionRunStartRequest.from_dict(
            {
                "peer_token": "peer-token",
                "prompt": "hello",
                "mode": "taskflow",
                "workflow_mode": "taskflow",
                "taskflow_id": "taskflow-1",
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "prompt": "hello",
            "session_hint": None,
            "mode": "taskflow",
            "workflow_mode": "taskflow",
            "taskflow_id": "taskflow-1",
        }
        assert SessionListRequest.from_dict(
            {"peer_token": "peer-token", "limit": 5, "if_list_etag": "etag-1"}
        ).to_dict() == {
            "peer_token": "peer-token",
            "limit": 5,
            "if_list_etag": "etag-1",
        }
        assert SessionLoadRequest.from_dict(
            {"peer_token": "peer-token", "session_id": "session-1"}
        ).to_dict() == {"peer_token": "peer-token", "session_id": "session-1"}
        assert SessionNewRequest.from_dict(
            {"peer_token": "peer-token"}
        ).to_dict() == {"peer_token": "peer-token"}
        assert SessionDeleteRequest.from_dict(
            {"peer_token": "peer-token", "session_id": "session-1"}
        ).to_dict() == {"peer_token": "peer-token", "session_id": "session-1"}
    def test_sessions_routes_verify_peer_token_and_dispatch(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        calls: list[tuple[str, str, dict]] = []

        def session_handler(action: str, peer_id: str, payload: dict) -> dict:
            calls.append((action, peer_id, payload))
            if action == "load" and payload.get("session_id") == "missing":
                return {"ok": False, "error": "session_not_found", "_status": 404}
            if action == "new":
                raise RuntimeError("secret session failure")
            return {"ok": True, "action": action}

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, list_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert list_body["ok"] is True
            assert list_body["action"] == "list"
            assert calls[-1][0] == "list"

            status, tagged_list_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token, "if_list_etag": "etag-1"},
            )
            assert status == 200
            assert tagged_list_body["ok"] is True
            assert calls[-1][0] == "list"
            assert calls[-1][2]["if_list_etag"] == "etag-1"

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/list",
                    {"peer_token": "bad-token"},
                )
                raise AssertionError("expected invalid token to fail")
            except HTTPError as exc:
                assert exc.code == 401

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/load",
                    {"peer_token": peer_token, "session_id": "missing"},
                )
                raise AssertionError("expected missing session to fail")
            except HTTPError as exc:
                assert exc.code == 404

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/new",
                    {"peer_token": peer_token},
                )
                raise AssertionError("expected session handler error to fail")
            except HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 500
                assert body["error"] == "session_request_failed"
                assert "secret session failure" not in body["message"]

            status, delete_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/delete",
                {"peer_token": peer_token, "session_id": "session-ok"},
            )
            assert status == 200
            assert delete_body["ok"] is True
            assert delete_body["action"] == "delete"
            assert calls[-1][0] == "delete"

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/fork",
                    {
                        "peer_token": peer_token,
                        "source_session_id": "session-ok",
                    },
                )
                raise AssertionError("expected removed session fork route to fail")
            except HTTPError as exc:
                assert exc.code == 404
            assert calls[-1][0] == "delete"
        finally:
            service.stop()
            relay.stop()

    def test_json_responses_support_gzip_when_requested(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_handler(action: str, peer_id: str, payload: dict) -> dict:
            del peer_id, payload
            return {"ok": True, "action": action, "blob": "x" * 2048}

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, headers, body = _raw_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
                headers={"Accept-Encoding": "gzip"},
            )
            assert status == 200
            assert headers["Content-Encoding"] == "gzip"
            decoded = json.loads(gzip.decompress(body).decode("utf-8"))
            assert decoded["blob"] == "x" * 2048

            status, headers, body = _raw_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert "Content-Encoding" not in headers
            assert json.loads(body.decode("utf-8"))["blob"] == "x" * 2048
        finally:
            service.stop()
            relay.stop()

    def test_features_report_available_backend_surfaces(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_handler(action: str, peer_id: str, payload: dict) -> dict:
            return {"ok": True, "action": action, "peer_id": peer_id}

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
            runtime_control_plane=AgentRunControlPlane(),
        )
        service.start()
        try:
            status, body = _json_request(
                "GET", f"{service.base_url}/remote/features"
            )
            assert status == 200
            assert body["ok"] is True
            assert body["api_version"] == 1
            assert isinstance(body["server_version"], str)
            assert body["features"]["sessions"] is True
            assert body["features"]["session_auto_save"] is True
            assert body["features"]["session_history_writable"] is True
            assert body["features"]["session_runs"] is True
            assert body["features"]["taskflow"] is True
            assert body["features"]["issue_assignment"] is True
            assert body["features"]["fresh_session_without_session_hint"] is True
            assert body["features"]["peer_token_heartbeat_refresh"] is True
            assert body["features"]["agent_runs"] == {
                "executor_features": {}
            }
        finally:
            service.stop()
            relay.stop()

    def test_features_report_missing_optional_handlers(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, body = _json_request("GET", f"{service.base_url}/remote/features")
            assert body["features"]["sessions"] is False
            assert body["features"]["session_auto_save"] is True
            assert body["features"]["session_history_writable"] is False
            assert body["features"]["session_runs"] is False
            assert body["features"]["fresh_session_without_session_hint"] is False
            assert body["features"]["peer_token_heartbeat_refresh"] is True
            assert body["features"]["agent_runs"]["executor_features"] == {}
        finally:
            service.stop()
            relay.stop()

    def test_features_include_peer_executor_features(self) -> None:
        relay = RelayServer()
        relay.registry.register(
            meta={
                "host_info_min": {
                    "agent_runs": {
                        "executor_features": {
                            "claude": {
                                "installed": True,
                                "version": "2.0.0",
                                "stream_json": True,
                                "resume_by_id": True,
                                "limitations": [],
                            },
                            "gemini": {
                                "installed": False,
                                "limitations": ["executable not found on PATH"],
                            },
                        }
                    }
                }
            }
        )
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            _, body = _json_request("GET", f"{service.base_url}/remote/features")
            executor_features = body["features"]["agent_runs"][
                "executor_features"
            ]
            assert executor_features["claude"]["resume_by_id"] is True
            assert executor_features["gemini"]["installed"] is False
        finally:
            service.stop()
            relay.stop()

    def test_http_heartbeat_refreshes_peer_token(self) -> None:
        relay = RelayServer(peer_token_ttl_sec=300)
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            before = relay.token_manager._peers[peer_token].expires_at
            time.sleep(0.01)

            status, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/heartbeat",
                {"peer_token": peer_token},
            )

            assert status == 200
            assert heartbeat_body["ok"] is True
            assert relay.token_manager._peers[peer_token].expires_at > before
        finally:
            service.stop()
            relay.stop()

    def test_runtime_heartbeat_and_admin_cancel_roundtrip(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "features": [
                        "agent_runs",
                        "agent_runs.local_workspace",
                    ],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            admin_headers = TEST_ADMIN_HEADERS

            _, submit_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/submit",
                {
                    "agent_run_id": "task-http-runtime",
                    "issue_id": "issue-1",
                    "agent_id": "coder",
                    "prompt": "run fake",
                    "executor": "fake",
                    "execution_location": "local_workspace",
                    "workspace_root": "G:/repo/main",
                },
                headers=admin_headers,
            )
            assert submit_body["ok"] is True

            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "executors": ["fake"],
                },
            )
            claim = claim_body["claim"]
            assert claim is not None
            assert claim["agent_run"]["id"] == "task-http-runtime"
            assert claim["activation_id"] == "task-http-runtime:activation:1"
            assert claim["activation"]["agent_run_id"] == "task-http-runtime"
            assert (
                claim["executor_request"]["metadata"]["activation_id"]
                == claim["activation_id"]
            )

            _, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                },
            )
            assert heartbeat_body["ok"] is True
            assert heartbeat_body["activation_id"] == claim["activation_id"]
            assert heartbeat_body["cancel_requested"] is False

            _, session_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/session",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "workdir": "G:/repo/main/.rcoder/agent-runtime/ws/task/workdir/repo",
                    "branch": "agent/coder/task-http",
                    "repo_url": "file:///repo/main",
                    "cache_path": "G:/repo/main/.rcoder/agent-runtime/repos/ws/repo.git",
                },
            )
            assert session_body["ok"] is True

            _, event_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/event",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "type": "text",
                    "text": "hello",
                },
            )
            assert event_body["ok"] is True
            text_event = [
                event
                for event in control.list_events("task-http-runtime")
                if event.type == "text"
            ][0]
            assert text_event.payload["activation_id"] == claim["activation_id"]

            _, admin_events = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/events",
                {"agent_run_id": "task-http-runtime", "after_seq": 0, "limit": 2},
                headers=admin_headers,
            )
            assert len(admin_events["events"]) == 2
            assert admin_events["next_seq"] == admin_events["events"][-1]["seq"]
            assert admin_events["has_more"] is True

            _, peer_events = _json_request(
                "GET",
                f"{service.base_url}/remote/agent-runs/task-http-runtime/events?peer_token={peer_token}&after_seq=0&limit=2",
            )
            assert len(peer_events["events"]) == 2
            assert peer_events["next_seq"] == peer_events["events"][-1]["seq"]
            assert peer_events["has_more"] is True

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-run-activations/event",
                    {
                        "peer_token": peer_token,
                        "request_id": claim["request_id"],
                        "agent_run_id": "task-http-runtime",
                        "activation_id": claim["activation_id"],
                        "worker_id": "other-worker",
                        "type": "text",
                        "text": "bad",
                    },
                )
                raise AssertionError("non-owner runtime event should be rejected")
            except HTTPError as exc:
                assert exc.code == 403

            _, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/cancel",
                {"agent_run_id": "task-http-runtime", "reason": "user_stop"},
                headers=admin_headers,
            )
            assert cancel_body == {"ok": True, "agent_run_id": "task-http-runtime"}

            _, cancelled_heartbeat = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                },
            )
            assert cancelled_heartbeat["ok"] is True
            assert cancelled_heartbeat["cancel_requested"] is True
            assert cancelled_heartbeat["reason"] == "user_stop"

            _, complete_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/complete",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "status": "cancelled",
                    "output": "",
                    "error": "execution cancelled",
                    "events": [
                        {
                            "request_id": claim["request_id"],
                            "agent_run_id": "task-http-runtime",
                            "activation_id": claim["activation_id"],
                            "worker_id": "worker-1",
                            "type": "status",
                            "data": {"status": "cancelled"},
                        }
                    ],
                },
            )
            assert complete_body["ok"] is True
            activation_completed = [
                event
                for event in control.list_events("task-http-runtime")
                if event.type == "activation_completed"
            ][0]
            assert activation_completed.payload["activation_id"] == claim["activation_id"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/agent-runs/retry",
                    {
                        "agent_run_id": "task-http-runtime",
                        "new_agent_run_id": "task-http-runtime-retry",
                    },
                    headers=admin_headers,
                )
                raise AssertionError("retry must not accept a new AgentRun id")
            except HTTPError as exc:
                assert exc.code == 400

            _, retry_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/retry",
                {
                    "agent_run_id": "task-http-runtime",
                },
                headers=admin_headers,
            )
            assert retry_body["ok"] is True
            assert retry_body["agent_run"]["id"] == "task-http-runtime"
            assert retry_body["agent_run"]["status"] == "queued"
            assert (
                retry_body["agent_run"]["current_activation_id"]
                == "task-http-runtime:activation:2"
            )
            assert "current_activation_input_kind" not in retry_body["agent_run"]["metadata"]
        finally:
            service.stop()
            relay.stop()

    def test_admin_agent_run_steer_endpoint_queues_and_delivers_mailbox_item(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "features": [
                        "agent_runs",
                        "agent_runs.local_workspace",
                    ],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            admin_headers = TEST_ADMIN_HEADERS

            _, submit_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/submit",
                {
                    "agent_run_id": "task-http-steer",
                    "agent_id": "coder",
                    "prompt": "run fake",
                    "executor": "fake",
                    "execution_location": "local_workspace",
                    "workspace_root": "G:/repo/main",
                },
                headers=admin_headers,
            )
            assert submit_body["ok"] is True

            with pytest.raises(HTTPError) as missing_key:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/agent-runs/steer",
                    {
                        "agent_run_id": "task-http-steer",
                        "payload": {"items": [{"type": "text", "text": "missing key"}]},
                    },
                    headers=admin_headers,
                )
            assert missing_key.value.code == 400

            with pytest.raises(HTTPError) as no_claim:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/agent-runs/steer",
                    {
                        "agent_run_id": "task-http-steer",
                        "payload": {"items": [{"type": "text", "text": "too early"}]},
                        "idempotency_key": "too-early",
                        "sender": "admin",
                    },
                    headers=admin_headers,
                )
            assert no_claim.value.code == 400

            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "executors": ["fake"],
                },
            )
            claim = claim_body["claim"]
            assert claim is not None
            _, first_heartbeat = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-steer",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                },
            )
            assert first_heartbeat["ok"] is True

            _, steer_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/steer",
                {
                    "agent_run_id": "task-http-steer",
                    "payload": {"items": [{"type": "text", "text": "add context"}]},
                    "idempotency_key": "admin-steer-1",
                    "sender": "admin",
                    "source": "admin",
                },
                headers=admin_headers,
            )
            assert steer_body["ok"] is True
            assert steer_body["activation_steer"]["status"] == "queued"
            assert steer_body["activation_steer"]["metadata"]["idempotency_key"] == (
                "admin-steer-1"
            )

            _, delivery = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-steer",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                },
            )
            assert [item["id"] for item in delivery["activation_steers"]] == [
                steer_body["activation_steer"]["id"]
            ]

            _, ack = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-steer",
                    "activation_id": claim["activation_id"],
                    "worker_id": "worker-1",
                    "delivered_steer_ids": [steer_body["activation_steer"]["id"]],
                },
            )
            assert ack["activation_steers"] == []
            detail = control.load_agent_run_detail("task-http-steer")
            assert detail["activation_steers"][0]["status"] == "delivered"
        finally:
            service.stop()
            relay.stop()

    def test_taskflow_http_api_uses_taskflow_and_work_item_resources(self) -> None:
        route_source = Path("labrastro_server/interfaces/http/remote/routes/taskflow.py").read_text(encoding="utf-8")
        assert "/remote/taskflow/goals" not in route_source
        assert "task-drafts" not in route_source
        assert "taskflows" in route_source
        assert "work-items" in route_source

    def test_taskflow_http_api_records_discovery_and_confirms_brief(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "features": ["agent_runs"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _, create_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows",
                {
                    "peer_token": peer_token,
                    "project_id": "project-1",
                    "raw_goal": "Build taskflow discovery API.",
                    "taskflow_id": "taskflow-http",
                    "goal_id": "goal-http",
                },
            )
            assert create_body["ok"] is True
            with pytest.raises(HTTPError) as old_cards:
                _json_request(
                    "GET",
                    f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/review-cards?peer_token={peer_token}",
                )
            assert old_cards.value.code == 404
            with pytest.raises(HTTPError) as old_question_answer:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/questions/question-1/answer",
                    {"peer_token": peer_token, "answer": "unused"},
                )
            assert old_question_answer.value.code == 404

            _, discovery_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/discovery-turn",
                {
                    "peer_token": peer_token,
                    "rules": [
                        {
                            "id": "rule-confirm-before-dispatch",
                            "statement": "Dispatch requires confirmed brief.",
                        }
                    ],
                    "examples": [
                        {
                            "id": "example-confirmed-brief",
                            "rule_id": "rule-confirm-before-dispatch",
                            "title": "Confirmed brief allows compile",
                        }
                    ],
                    "decisions": [
                        {
                            "id": "decision-boundary",
                            "question": "What boundary is confirmed?",
                            "options": [{"id": "brief", "label": "Brief"}],
                            "recommended": "brief",
                            "linked_rule_ids": ["rule-confirm-before-dispatch"],
                        }
                    ],
                    "work_item_candidates": [
                        {
                            "id": "candidate-1",
                            "title": "Implement API write path",
                            "description": "Add discovery and brief actions.",
                            "acceptance_refs": ["example-confirmed-brief"],
                            "decision_refs": ["decision-boundary"],
                            "scenario_refs": ["example-confirmed-brief"],
                        }
                    ],
                },
            )
            assert discovery_body["taskflow"]["outputs"]["current_brief_version"] == 1

            _, answer_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/review-cards-v1/taskflow-http:decision:decision-boundary/actions",
                {
                    "peer_token": peer_token,
                    "action": "accept",
                },
            )
            assert answer_body["taskflow"]["outputs"]["current_brief_version"] == 2

            _, ready_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/brief/ready",
                {"peer_token": peer_token, "version": 2},
            )
            assert ready_body["taskflow"]["outputs"]["brief_versions"][-1]["status"] == "ready"

            _, confirm_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/brief/confirm",
                {"peer_token": peer_token, "version": 2},
            )
            assert confirm_body["taskflow"]["outputs"]["confirmed_brief_version"] == 2

            _, compile_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/compile",
                {"peer_token": peer_token},
            )
            assert compile_body["ok"] is True
            compiled = compile_body["plan"]["work_item_candidates"][0]
            assert compiled["metadata"]["acceptance"]["source_brief_version"] == 2

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/work-items/{compiled['work_item_id']}/dispatch",
                    {"peer_token": peer_token},
                )
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 400

            _, request_dispatch_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/dispatch-decisions",
                {
                    "peer_token": peer_token,
                    "work_item_ids": [compiled["work_item_id"]],
                    "actor": "user",
                },
            )
            dispatch_decision = request_dispatch_body["dispatch_decision"]
            assert dispatch_decision["status"] == "requested"

            _, confirm_dispatch_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/dispatch-decisions/{dispatch_decision['id']}/confirm",
                {"peer_token": peer_token, "actor": "user"},
            )
            assert (
                confirm_dispatch_body["taskflow"]["outputs"]["dispatch_decisions"][-1]["status"]
                == "confirmed"
            )

            _, dispatch_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/work-items/{compiled['work_item_id']}/dispatch",
                {
                    "peer_token": peer_token,
                    "dispatch_decision_id": dispatch_decision["id"],
                },
            )
            assert dispatch_body["task_run"]["dispatch_ref_id"] == dispatch_decision["id"]

            _, runtime_body = _json_request(
                "GET",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-http/runtime?peer_token={peer_token}",
            )
            assert runtime_body["ok"] is True
            assert runtime_body["taskflow_id"] == "taskflow-http"
            assert runtime_body["task_runs"][0]["task_run"]["id"] == dispatch_body["task_run"]["id"]
            assert runtime_body["task_runs"][0]["work_item"]["id"] == compiled["work_item_id"]
            assert (
                runtime_body["task_runs"][0]["dispatch_decision"]["id"]
                == dispatch_decision["id"]
            )
            assert runtime_body["task_runs"][0]["liveness"]["state"] == "agent_selection_required"
        finally:
            service.stop()
            relay.stop()

    def test_taskflow_http_api_records_and_overrides_complexity(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "features": ["agent_runs"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows",
                {
                    "peer_token": peer_token,
                    "project_id": "project-1",
                    "raw_goal": "Build public plugin API with migration risk.",
                    "taskflow_id": "taskflow-complexity-http",
                    "goal_id": "goal-complexity-http",
                },
            )

            _, evidence_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-complexity-http/complexity/evidence",
                {
                    "peer_token": peer_token,
                    "evidence": [
                        {
                            "id": "evidence-public-api",
                            "dimension": "interface_impact",
                            "source_type": "goal",
                            "source_id": "goal-complexity-http",
                            "score_delta": 2,
                            "rationale": "Public API contract affects consumers.",
                        }
                    ],
                },
            )
            estimate = evidence_body["taskflow"]["compiler"]["complexity_estimate"]
            assert estimate["level"] == "L2"
            assert "public-interface-floor" in estimate["hard_escalations"]
            assert "api_contract" in estimate["required_artifacts"]

            _, override_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-complexity-http/complexity/override",
                {
                    "peer_token": peer_token,
                    "level": "L3",
                    "reason": "Architectural governance required.",
                    "actor": "architect",
                },
            )
            override_estimate = override_body["taskflow"]["compiler"]["complexity_estimate"]
            assert override_estimate["level"] == "L3"
            assert override_estimate["overridden_by"] == "architect"
            assert (
                override_body["taskflow"]["outputs"]["brief_versions"][-1]["complexity_estimate"]["level"]
                == "L3"
            )

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/taskflow/taskflows/taskflow-complexity-http/complexity/evidence",
                    {
                        "peer_token": peer_token,
                        "evidence": [
                            {
                                "id": "bad-evidence",
                                "dimension": "not_a_dimension",
                                "source_type": "goal",
                                "score_delta": 1,
                            }
                        ],
                    },
                )
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 400
        finally:
            service.stop()
            relay.stop()

    def test_taskflow_http_api_returns_and_scans_complexity(self, tmp_path: Path) -> None:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        (workspace / "package.json").write_text(
            '{"dependencies":{"express":"^4.0.0"}}',
            encoding="utf-8",
        )
        routes = workspace / "src" / "routes"
        routes.mkdir(parents=True)
        (routes / "users.ts").write_text(
            "export async function GET() { return Response.json({}) }",
            encoding="utf-8",
        )

        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": str(workspace),
                    "workspace_root": str(workspace),
                    "features": ["agent_runs"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows",
                {
                    "peer_token": peer_token,
                    "project_id": "project-1",
                    "raw_goal": "Expose a public users API.",
                    "taskflow_id": "taskflow-complexity-scan-http",
                    "goal_id": "goal-complexity-scan-http",
                },
            )

            _, scan_body = _json_request(
                "POST",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-complexity-scan-http/complexity/scan-repo",
                {
                    "peer_token": peer_token,
                    "workspace_path": str(workspace),
                    "repository_id": "repo-http",
                },
            )
            estimate = scan_body["complexity"]["estimate"]
            assert "interface_impact" in estimate["dominant_dimensions"]
            assert estimate["scan_refs"]
            assert any(
                item["source_type"] == "repo_static_analysis"
                for item in estimate["evidence"]
            )

            _, get_body = _json_request(
                "GET",
                f"{service.base_url}/remote/taskflow/taskflows/taskflow-complexity-scan-http/complexity?peer_token={peer_token}",
            )
            assert get_body["complexity"]["estimate"]["scan_refs"] == estimate["scan_refs"]
        finally:
            service.stop()
            relay.stop()

    def test_issue_assignment_and_mention_http_api_reuses_taskflow_dispatch(
        self,
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "docs_profile": {
                        "executor": "fake",
                        "execution_location": "remote_server",
                    }
                },
                "agents": {
                    "docs": {
                        "name": "Docs Agent",
                        "aliases": ["writer"],
                        "runtime_profile": "docs_profile",
                        "dispatch": {"profile": "Best for docs and research tasks."},
                    }
                },
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "features": ["agent_runs"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _, issue_body = _json_request(
                "POST",
                f"{service.base_url}/remote/issues",
                {
                    "peer_token": peer_token,
                    "title": "Docs issue",
                    "description": "Write docs.",
                },
            )
            issue_id = issue_body["issue"]["id"]

            _, assignment_body = _json_request(
                "POST",
                f"{service.base_url}/remote/issues/{issue_id}/assignments",
                {
                    "peer_token": peer_token,
                    "target_agent_id": "docs",
                    "task_type": "docs",
                },
            )
            assignment = assignment_body["assignment"]
            assert assignment["status"] == "ready"
            assert control.list_agent_runs() == []

            _, reassigned_body = _json_request(
                "POST",
                f"{service.base_url}/remote/assignments/{assignment['id']}/assign",
                {
                    "peer_token": peer_token,
                    "agent_id": "docs",
                    "reason": "manual confirmation",
                },
            )
            assert reassigned_body["assignment"]["target_agent_id"] == "docs"

            _, parse_body = _json_request(
                "POST",
                f"{service.base_url}/remote/mentions/parse",
                {"peer_token": peer_token, "raw_text": "@writer please help"},
            )
            assert parse_body["mention"]["resolved_agent_id"] == "docs"

            _, mention_body = _json_request(
                "POST",
                f"{service.base_url}/remote/mentions",
                {
                    "peer_token": peer_token,
                    "issue_id": issue_id,
                    "raw_text": "@writer please draft this.",
                    "prompt": "Draft this.",
                },
            )
            assert mention_body["mention"]["status"] == "ready"
            assert mention_body["mention"]["assignment_id"]
            assert control.list_agent_runs() == []

            _, dispatch_body = _json_request(
                "POST",
                f"{service.base_url}/remote/assignments/{assignment['id']}/dispatch",
                {"peer_token": peer_token},
            )
            dispatched = dispatch_body["assignment"]
            assert dispatched["status"] == "dispatched"
            assert dispatched["task_run_id"]
            agent_runs = control.list_agent_runs()
            assert len(agent_runs) == 1
            task = control.get_agent_run(agent_runs[0]["id"])
            assert task.metadata["dispatch_source"] == "assignment"
            assert "issue_id" not in task.metadata
            assert "assignment_id" not in task.metadata

            _, issue_detail = _json_request(
                "GET",
                f"{service.base_url}/remote/issues/{issue_id}?peer_token={peer_token}",
            )
            assert issue_detail["assignments"][0]["id"] == assignment["id"]
            assert issue_detail["taskflow"]["outputs"]["task_run_refs"]
            state = service.taskflow_service.get_taskflow_state(
                issue_detail["issue"]["taskflow_id"]
            )
            project = service.taskflow_service.project_service.get_project_state(
                state.meta.project_id
            )
            assert project is not None
            assert any(
                link.source_id == dispatched["task_run_id"]
                and link.target_id == task.id
                and link.relation_type.value == "dispatches"
                for link in project.traceability.task_run_links
            )

            _, events_body = _json_request(
                "GET",
                f"{service.base_url}/remote/issues/{issue_id}/events?peer_token={peer_token}&after_seq=0",
            )
            assert {event["type"] for event in events_body["events"]} >= {
                "issue_created",
                "assignment_dispatched",
                "mention_created",
            }
        finally:
            service.stop()
            relay.stop()

    def test_agent_run_claim_rejects_invalid_or_unregistered_worker_kind(
        self,
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "server_fake": {
                        "executor": "fake",
                        "execution_location": "remote_server",
                        "worker_kind": "server_worker",
                    }
                },
                "agents": {"reviewer": {"runtime_profile": "server_fake"}},
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "features": [
                        "agent_runs",
                        "worker_kind:local_peer",
                        "agent_runs.local_workspace",
                    ],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            control.submit_agent_run(
                AgentRunRequest(
                    agent_id="reviewer",
                    prompt="run remote",
                ),
                task_id="task-remote-server",
            )

            with pytest.raises(HTTPError) as invalid_worker_kind:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-run-activations/claim",
                    {
                        "peer_token": peer_token,
                        "worker_id": "worker-local",
                        "worker_kind": "bad",
                        "executors": ["fake"],
                    },
                )
            assert invalid_worker_kind.value.code == 400
            invalid_body = json.loads(
                invalid_worker_kind.value.read().decode("utf-8")
            )
            assert invalid_body["error"] == "invalid_worker_kind"

            with pytest.raises(HTTPError) as unregistered_worker_kind:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-run-activations/claim",
                    {
                        "peer_token": peer_token,
                        "worker_id": "worker-local",
                        "worker_kind": "server_worker",
                        "executors": ["fake"],
                    },
                )
            assert unregistered_worker_kind.value.code == 400
            unregistered_body = json.loads(
                unregistered_worker_kind.value.read().decode("utf-8")
            )
            assert unregistered_body["error"] == "worker_kind_not_registered"
            assert control.get_agent_run("task-remote-server").status.value == "queued"
        finally:
            service.stop()
            relay.stop()

    def test_admin_server_settings_update_rejects_inconsistent_model_request_origin(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(config_path, {})
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/server-settings/update",
                    {
                        "runtime_profiles": {
                            "bad_profile": {
                                "executor": "codex",
                                "execution_location": "remote_server",
                                "worker_kind": "server_worker",
                                "model_request_origin": "local_cli",
                            }
                        },
                        "agent_registry": {
                            "agents": {
                                "reviewer": {"runtime_profile": "bad_profile"}
                            }
                        },
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
            assert excinfo.value.code == 400
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert body["error"] == "invalid_agent_runtime_profile"
            assert "server_worker_cli" in body["message"]
        finally:
            service.stop()
            relay.stop()

    def test_admin_capability_package_routes_protect_builtin_packages(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(config_path, {})
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            for package_id in (
                "environment",
                "core_builtin_tools",
                "capability_packager_builtin_tools",
            ):
                with pytest.raises(HTTPError) as disable_exc:
                    _json_request(
                        "POST",
                        f"{service.base_url}/remote/admin/capability-packages/enable",
                        {"package_id": package_id, "enabled": False},
                        headers=TEST_ADMIN_HEADERS,
                    )
                assert disable_exc.value.code == 400
                disable_body = json.loads(disable_exc.value.read().decode("utf-8"))
                assert disable_body["error"] == "builtin_capability_package"

                with pytest.raises(HTTPError) as delete_exc:
                    _json_request(
                        "POST",
                        f"{service.base_url}/remote/admin/capability-packages/delete",
                        {"package_id": package_id},
                        headers=TEST_ADMIN_HEADERS,
                    )
                assert delete_exc.value.code == 400
                delete_body = json.loads(delete_exc.value.read().decode("utf-8"))
                assert delete_body["error"] == "builtin_capability_package"
        finally:
            service.stop()
            relay.stop()

    def test_admin_capability_package_accept_returns_separate_state_axes(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(config_path, {})
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            status, built = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/candidates/build",
                {
                    "package_id": "review",
                    "draft": {
                        "id": "review",
                        "name": "Review",
                        "components": [
                            {
                                "kind": "skill",
                                "name": "code-review",
                                "skill_content": (
                                    "---\n"
                                    "name: code-review\n"
                                    "description: Review changes.\n"
                                    "---\n"
                                    "Review.\n"
                                ),
                            }
                        ],
                        "evidence": [{"title": "fixture", "excerpt": "review"}],
                        "risk_level": "low",
                    },
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert status == 200
            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/candidates/apply",
                {
                    "candidate_id": built["candidate_id"],
                    "candidate_hash": built["candidate_hash"],
                },
                headers=TEST_ADMIN_HEADERS,
            )

            assert status == 200
            state = body["capability_package"]["state"]
            assert state["install_state"] in {"materialized", "installed"}
            assert state["activation_state"] == "inactive"
            settings_state = body["settings"]["capability_packages"]["review"]["state"]
            assert settings_state["install_state"] in {"materialized", "installed"}
            assert settings_state["activation_state"] == "inactive"
        finally:
            service.stop()
            relay.stop()

    def test_admin_capability_package_update_transition_routes(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(
            config_path,
            {
                "capability_packages": {
                    "waza": {
                        "enabled": True,
                        "status": "installed",
                        "source_snapshot": {
                            "snapshot_id": "snap-old",
                            "source_ref": "main",
                            "commit_sha": "1111111",
                        },
                        "manifest": {
                            "components": [
                                {
                                    "id": "skill:waza/read",
                                    "kind": "skill",
                                    "name": "waza-read",
                                    "config": {
                                        "skill_content": (
                                            "---\n"
                                            "name: waza-read\n"
                                            "description: Read Waza.\n"
                                            "---\n"
                                            "Read.\n"
                                        )
                                    },
                                }
                            ]
                        },
                        "components": ["skill:waza/read"],
                    }
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            update_payload = {
                "package_id": "waza",
                "next_source_snapshot": {
                    "snapshot_id": "snap-new",
                    "source_ref": "main",
                    "commit_sha": "2222222",
                },
                "next_manifest": {
                    "components": [
                        {
                            "id": "skill:waza/read",
                            "kind": "skill",
                            "name": "waza-read",
                            "config": {
                                "skill_content": (
                                    "---\n"
                                    "name: waza-read\n"
                                    "description: Read Waza.\n"
                                    "---\n"
                                    "Read.\n"
                                )
                            },
                        },
                        {
                            "id": "skill:waza/write",
                            "kind": "skill",
                            "name": "waza-write",
                            "config": {
                                "skill_content": (
                                    "---\n"
                                    "name: waza-write\n"
                                    "description: Write Waza.\n"
                                    "---\n"
                                    "Write.\n"
                                )
                            },
                        },
                    ]
                },
            }
            check_status, check_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/updates/check",
                update_payload,
                headers=TEST_ADMIN_HEADERS,
            )
            assert check_status == 200
            assert check_body["transition_preview"]["upstream_version"] == "main@2222222"
            assert check_body["transition_preview"]["manifest_diff"]["added_components"] == [
                "skill:waza/write"
            ]

            prepare_status, prepare_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/updates/prepare",
                update_payload,
                headers=TEST_ADMIN_HEADERS,
            )
            assert prepare_status == 200
            prepared = prepare_body["settings"]["capability_packages"]["waza"]
            assert prepared["enabled"] is True
            assert prepared["state"]["update_state"] == "candidate_ready"

            apply_status, apply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/updates/apply",
                {
                    "package_id": "waza",
                    "candidate_id": prepare_body["candidate_id"],
                    "candidate_hash": prepare_body["candidate_hash"],
                    "activation_approved": False,
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert apply_status == 200
            applied = apply_body["settings"]["capability_packages"]["waza"]
            assert applied["enabled"] is False
            assert applied["state"]["activation_state"] == "inactive"
            assert applied["state"]["update_state"] == "rollback_available"
            assert applied["rollback"]["snapshot_id"] == "snap-old"

            rollback_prepare_status, rollback_prepare_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/updates/rollback/prepare",
                {"package_id": "waza"},
                headers=TEST_ADMIN_HEADERS,
            )
            assert rollback_prepare_status == 200
            rollback_status, rollback_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/updates/rollback",
                {
                    "package_id": "waza",
                    "candidate_id": rollback_prepare_body["candidate_id"],
                    "candidate_hash": rollback_prepare_body["candidate_hash"],
                    "activation_approved": True,
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert rollback_status == 200
            rolled_back = rollback_body["settings"]["capability_packages"]["waza"]
            assert rolled_back["state"]["update_state"] == "current"
        finally:
            service.stop()
            relay.stop()

    def test_capability_package_peer_install_plan_waits_for_peer_result(
        self, tmp_path: Path
    ) -> None:
        del tmp_path
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            capability_packages={
                "waza": {
                    "enabled": False,
                    "status": "installed",
                    "install_plans": [
                        {
                            "plan_id": "plan-waza",
                            "actions": [
                                {
                                    "id": "install-waza-python",
                                    "type": "install_python_packages",
                                    "target": "local_peer",
                                    "package_id": "waza",
                                    "component_id": "skill:waza/read",
                                    "params": {"packages": ["readability-lxml"]},
                                }
                            ],
                        }
                    ],
                }
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]

            status, plan_body = _json_request(
                "POST",
                f"{service.base_url}/remote/capability-packages/install/plan",
                {"peer_token": peer_token},
            )

            assert status == 200
            assert plan_body["type"] == "capabilityPackage.installPlan"
            action = plan_body["plan"]["actions"][0]
            assert action["target"] == "local_peer"
            action_key = "waza|plan-waza|install-waza-python|skill:waza/read"
            peer_status = plan_body["peer_status"]["actions"][action_key]
            assert peer_status["target"] == "local_peer"
            assert peer_status["check_state"] in {"unknown", "pending"}
            assert peer_status["install_state"] != "installed"
            assert peer_status["peer_result"] is None

            _, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/capability-packages/install/result",
                {
                    "peer_token": peer_token,
                    "result": {
                        "plan_id": "plan-waza",
                        "action_id": "install-waza-python",
                        "package_id": "waza",
                        "component_id": "skill:waza/read",
                        "target": "local_peer",
                        "status": "passed",
                        "version": "1.0.0",
                        "content_hash": "sha256:abc",
                        "message": "installed",
                        "timestamp": "2026-06-11T00:00:00Z",
                    },
                },
            )

            assert result_body["type"] == "capabilityPackage.installResult"
            updated = result_body["peer_status"]["actions"][action_key]
            assert updated["check_state"] == "passed"
            assert updated["install_state"] == "installed"
            assert updated["peer_result"]["content_hash"] == "sha256:abc"
        finally:
            service.stop()
            relay.stop()

    def test_capability_package_peer_results_do_not_collide_on_action_id(
        self, tmp_path: Path
    ) -> None:
        del tmp_path
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            capability_packages={
                "waza": {
                    "install_plans": [
                        {
                            "plan_id": "plan-python",
                            "actions": [
                                {
                                    "id": "install-python",
                                    "type": "install_python_packages",
                                    "target": "local_peer",
                                    "package_id": "waza",
                                    "component_id": "skill:waza/read",
                                }
                            ],
                        }
                    ],
                },
                "docs": {
                    "install_plans": [
                        {
                            "plan_id": "plan-python",
                            "actions": [
                                {
                                    "id": "install-python",
                                    "type": "install_python_packages",
                                    "target": "local_peer",
                                    "package_id": "docs",
                                    "component_id": "skill:docs/read",
                                }
                            ],
                        }
                    ],
                },
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]

            _, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/capability-packages/install/result",
                {
                    "peer_token": peer_token,
                    "result": {
                        "plan_id": "plan-python",
                        "action_id": "install-python",
                        "package_id": "waza",
                        "component_id": "skill:waza/read",
                        "target": "local_peer",
                        "status": "passed",
                        "content_hash": "sha256:waza",
                    },
                },
            )

            actions = result_body["peer_status"]["actions"]
            waza_key = "waza|plan-python|install-python|skill:waza/read"
            docs_key = "docs|plan-python|install-python|skill:docs/read"
            assert actions[waza_key]["check_state"] == "passed"
            assert actions[waza_key]["install_state"] == "installed"
            assert actions[docs_key]["check_state"] == "pending"
            assert actions[docs_key]["install_state"] == "registered"
            assert actions[docs_key]["peer_result"] is None
        finally:
            service.stop()
            relay.stop()

    def test_capability_package_peer_result_matches_params_component_id(
        self, tmp_path: Path
    ) -> None:
        del tmp_path
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            capability_packages={
                "waza": {
                    "install_plans": [
                        {
                            "plan_id": "plan-python",
                            "actions": [
                                {
                                    "id": "install-python",
                                    "type": "install_python_packages",
                                    "target": "local_peer",
                                    "params": {
                                        "package_id": "waza",
                                        "component_id": "skill:waza/read",
                                        "packages": ["readability-lxml"],
                                    },
                                }
                            ],
                        }
                    ],
                }
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]

            _, plan_body = _json_request(
                "POST",
                f"{service.base_url}/remote/capability-packages/install/plan",
                {"peer_token": peer_token},
            )
            action_key = "waza|plan-python|install-python|skill:waza/read"
            assert action_key in plan_body["peer_status"]["actions"]

            _, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/capability-packages/install/result",
                {
                    "peer_token": peer_token,
                    "result": {
                        "plan_id": "plan-python",
                        "action_id": "install-python",
                        "package_id": "waza",
                        "component_id": "skill:waza/read",
                        "target": "local_peer",
                        "status": "passed",
                    },
                },
            )

            updated = result_body["peer_status"]["actions"][action_key]
            assert updated["check_state"] == "passed"
            assert updated["install_state"] == "installed"
        finally:
            service.stop()
            relay.stop()

    def test_capability_package_peer_result_with_stale_hash_requires_retry(
        self, tmp_path: Path
    ) -> None:
        del tmp_path
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            capability_packages={
                "waza": {
                    "install_plans": [
                        {
                            "plan_id": "plan-python",
                            "actions": [
                                {
                                    "id": "install-python",
                                    "type": "install_python_packages",
                                    "target": "local_peer",
                                    "package_id": "waza",
                                    "component_id": "skill:waza/read",
                                    "params": {
                                        "packages": ["readability-lxml"],
                                        "expected_content_hash": "sha256:new",
                                    },
                                }
                            ],
                        }
                    ],
                }
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]
            _, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/capability-packages/install/result",
                {
                    "peer_token": peer_token,
                    "result": {
                        "plan_id": "plan-python",
                        "action_id": "install-python",
                        "package_id": "waza",
                        "component_id": "skill:waza/read",
                        "target": "local_peer",
                        "status": "passed",
                        "content_hash": "sha256:old",
                    },
                },
            )

            action_key = "waza|plan-python|install-python|skill:waza/read"
            updated = result_body["peer_status"]["actions"][action_key]
            assert updated["check_state"] == "stale"
            assert updated["install_state"] == "registered"
        finally:
            service.stop()
            relay.stop()

    def test_server_settings_update_refreshes_runtime_snapshot_for_agent_submit(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(
            config_path,
            {"run_limits": {"max_running_agents": 1, "max_shells_per_agent": 1}},
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            runtime_control_plane=control,
        )

        def reload_runtime_config() -> None:
            data = load_yaml_config(config_path)
            run_limits, runtime_snapshot = _agent_run_settings_from_config(data)
            control.configure(
                max_running_tasks=run_limits.max_running_agents,
                runtime_snapshot=runtime_snapshot,
            )

        service.admin_manager.reload_handler = reload_runtime_config
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/repo",
                    "workspace_root": "/tmp/repo",
                    "features": [
                        "agent_runs",
                        "agent_runs.daemon_worktree",
                    ],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            admin_headers = TEST_ADMIN_HEADERS

            _, update_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/update",
                {
                    "run_limits": {
                        "max_running_agents": 4,
                        "max_shells_per_agent": 1,
                    },
                    "runtime_profiles": {
                            "smoke_fake_profile": {
                                "executor": "fake",
                                "execution_location": "daemon_worktree",
                                "credential_refs": {"model": "smoke_model_ref"},
                            }
                    },
                    "agent_registry": {
                        "agents": {
                            "smoke_reviewer": {
                                "name": "Smoke Reviewer",
                                "runtime_profile": "smoke_fake_profile",
                                "dispatch": {
                                    "profile": "Best for smoke review tasks."
                                },
                                "prompt": {
                                    "system_append": (
                                        "You are the smoke reviewer agent."
                                    )
                                },
                            }
                        },
                    }
                },
                headers=admin_headers,
            )
            assert update_body["ok"] is True
            assert control.max_running_tasks == 4

            _, submit_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/submit",
                {
                    "agent_run_id": "task-agent-only",
                    "issue_id": "issue-1",
                    "agent_id": "smoke_reviewer",
                    "prompt": "run smoke",
                },
                headers=admin_headers,
            )
            assert submit_body["ok"] is True
            assert submit_body["agent_run"]["executor"] == "fake"
            assert submit_body["agent_run"]["execution_location"] == "daemon_worktree"
            assert submit_body["agent_run"]["runtime_profile_id"] == "smoke_fake_profile"

            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-run-activations/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "executors": ["fake"],
                },
            )
            claim = claim_body["claim"]
            assert claim is not None
            assert claim["executor_request"]["executor"] == "fake"
            assert (
                claim["executor_request"]["runtime_profile_id"]
                == "smoke_fake_profile"
            )
            prompt_files = claim["executor_request"]["metadata"]["prompt_files"]
            assert "AGENT_RUNTIME.md" in prompt_files
            assert "Smoke Reviewer" in prompt_files["AGENT_RUNTIME.md"]
            assert (
                "You are the smoke reviewer agent."
                in prompt_files["AGENT_RUNTIME.md"]
            )
            assert (
                claim["runtime_snapshot"]["runtime_profiles"]["smoke_fake_profile"][
                    "credential_refs"
                ]["model"]
                == "smoke_model_ref"
            )
        finally:
            service.stop()
            relay.stop()

    def test_server_settings_update_replace_removes_runtime_profiles_and_agents(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(
            config_path,
            {
                "run_limits": {
                    "max_running_agents": 2,
                    "max_shells_per_agent": 1,
                },
                "runtime_profiles": {
                    "old_profile": {
                        "executor": "fake",
                        "execution_location": "daemon_worktree",
                    }
                },
                "agent_registry": {
                    "agents": {"old_agent": {"runtime_profile": "old_profile"}}
                },
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        run_limits, runtime_snapshot = _agent_run_settings_from_config(
            load_yaml_config(config_path)
        )
        control = AgentRunControlPlane(
            max_running_tasks=run_limits.max_running_agents,
            runtime_snapshot=runtime_snapshot,
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            runtime_control_plane=control,
        )

        def reload_runtime_config() -> None:
            data = load_yaml_config(config_path)
            run_limits, runtime_snapshot = _agent_run_settings_from_config(data)
            control.configure(
                max_running_tasks=run_limits.max_running_agents,
                runtime_snapshot=runtime_snapshot,
            )

        service.admin_manager.reload_handler = reload_runtime_config
        service.start()
        try:
            _, update_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/update",
                {
                    "run_limits": {
                        "max_running_agents": 3,
                    },
                    "runtime_profiles": {},
                    "agent_registry": {"agents": {}},
                },
                headers=TEST_ADMIN_HEADERS,
            )

            assert update_body["ok"] is True
            assert update_body["settings"]["run_limits"]["max_running_agents"] == 3
            assert update_body["settings"]["run_limits"]["max_shells_per_agent"] == 1
            assert set(update_body["settings"]["runtime_profiles"]) == {
                "environment_local",
                "agent_remote",
                "capability_packager_remote",
            }
            assert set(update_body["settings"]["agent_registry"]["agents"]) == {
                "environment_configurator",
                "capability_packager",
                "main_chat",
            }
            assert control.max_running_tasks == 3
            assert set(control.runtime_snapshot["runtime_profiles"]) == {
                "environment_local",
                "agent_remote",
                "capability_packager_remote",
            }
            assert set(control.runtime_snapshot["agents"]) == {
                "environment_configurator",
                "capability_packager",
                "main_chat",
            }
        finally:
            service.stop()
            relay.stop()

    def test_admin_runtime_submit_rejects_missing_agent_profile(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane(
            runtime_snapshot={
                "agents": {"smoke_reviewer": {"runtime_profile": "missing_profile"}}
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/agent-runs/submit",
                    {
                        "agent_run_id": "task-missing-profile",
                        "issue_id": "issue-1",
                        "agent_id": "smoke_reviewer",
                        "prompt": "run smoke",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
            except HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 400
                assert body["error"] == "invalid_agent_run"
                assert "missing_profile" not in body["message"]
            else:
                raise AssertionError("submit should reject missing runtime profile")
        finally:
            service.stop()
            relay.stop()

    def test_default_artifact_provider_prefers_prebuilt_binary(
        self, tmp_path: Path
    ) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        artifact_root = getattr(provider, "_artifact_root")
        prebuilt_path = artifact_root / "linux" / "amd64" / "rcoder-peer"
        prebuilt_path.parent.mkdir(parents=True, exist_ok=True)
        prebuilt_path.write_bytes(b"prebuilt-peer")
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.subprocess.run"
            ) as mock_run:
                content, content_type = provider("linux", "amd64", "rcoder-peer") or (
                    None,
                    None,
                )
            assert content == b"prebuilt-peer"
            assert content_type == "application/octet-stream"
            mock_run.assert_not_called()
        finally:
            _cleanup_provider_build_dir(provider)
            prebuilt_path.unlink(missing_ok=True)
            for parent in [
                prebuilt_path.parent,
                prebuilt_path.parent.parent,
                artifact_root,
            ]:
                try:
                    parent.rmdir()
                except OSError:
                    pass

    def test_default_artifact_provider_raises_without_prebuilt_or_go(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.shutil.which",
                return_value=None,
            ):
                with pytest.raises(RuntimeError, match="no prebuilt binary found"):
                    provider("linux", "amd64", "rcoder-peer")
        finally:
            _cleanup_provider_build_dir(provider)

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="Go SDK is not installed")
    def test_default_artifact_provider_builds_real_agent_binary(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            content, content_type = provider("linux", "amd64", "rcoder-peer") or (
                None,
                None,
            )
            assert content_type == "application/octet-stream"
            assert isinstance(content, bytes)
            assert len(content) > 0
        finally:
            _cleanup_provider_build_dir(provider)

    def test_artifact_endpoint_returns_clear_error_when_unavailable(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda _os_name, _arch, _name: (_ for _ in ()).throw(
                RuntimeError(
                    "peer artifact unavailable: no prebuilt binary found and local Go SDK is not installed"
                )
            ),
        )
        service.start()
        try:
            try:
                _URLOPEN(
                    f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                    timeout=5,
                )
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "artifact_unavailable"
                assert "no prebuilt binary found" in body["message"]
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="Go SDK is not installed")
    def test_go_agent_run_worker_fake_daemon_worktree_end_to_end(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRunControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        agent_binary = _build_go_agent_binary()
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, timeout=30)
        subprocess.run(["git", "checkout", "-B", "main"], cwd=repo, check=True, timeout=30)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repo,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo,
            check=True,
            timeout=30,
        )
        (repo / "tracked.txt").write_text("initial\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, timeout=30)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, timeout=30)
        agent_env, gh_log = _fake_gh_env(tmp_path)
        proc = subprocess.Popen(
            [
                str(agent_binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(repo),
                "--workspace-root",
                str(repo),
                "--poll-interval",
                "100ms",
                "--agent-run-worker",
                "--worker-session-id",
                "worker-session-1",
                "--agent-run-worker-kind",
                "server_worker",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=agent_env,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not relay.registry.list_online():
                time.sleep(0.1)
            assert relay.registry.list_online()

            admin_headers = TEST_ADMIN_HEADERS
            _, submit = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/submit",
                {
                    "agent_run_id": "task-go-runtime-worktree",
                    "issue_id": "issue-1",
                    "agent_id": "coder",
                    "prompt": "hello from fake runtime",
                    "executor": "fake",
                    "execution_location": "daemon_worktree",
                    "publish_policy": "branch",
                    "metadata": {
                        "repo_url": repo.resolve().as_uri(),
                        "workspace_id": "test-workspace",
                        "prompt_files": {
                            "AGENTS.md": "Use project conventions.\n",
                        },
                        "fake_files": {
                            "agent-output.txt": "created by fake executor\n",
                        },
                        "pr_body": "body",
                    },
                },
                headers=admin_headers,
            )
            assert submit["ok"] is True

            deadline = time.time() + 25
            task = control.get_agent_run("task-go-runtime-worktree")
            while time.time() < deadline and not task.is_terminal:
                time.sleep(0.2)
                task = control.get_agent_run("task-go-runtime-worktree")

            assert task.status.value == "completed"
            assert task.terminal_result["output"] == "hello from fake runtime"
            assert task.workdir is not None
            workdir = Path(task.workdir)
            assert (workdir / "tracked.txt").exists()
            assert (workdir / "agent-output.txt").read_text() == "created by fake executor\n"
            assert (workdir / "AGENTS.md").read_text() == "Use project conventions.\n"
            artifacts = control.artifacts_to_dict("task-go-runtime-worktree")
            artifact_types = {artifact["type"]: artifact for artifact in artifacts}
            assert artifact_types["branch"]["status"] == "pushed"
            branch_name = str(artifact_types["branch"]["branch_name"] or "")
            assert branch_name
            assert "pull_request" not in artifact_types
            pushed = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", branch_name],
                cwd=workdir,
                check=True,
                timeout=30,
                capture_output=True,
                text=True,
            )
            assert branch_name in pushed.stdout
            assert not gh_log.exists() or gh_log.read_text(encoding="utf-8") == ""
            events = control.list_events("task-go-runtime-worktree")
            assert any(event.type == "session_pinned" for event in events)
            assert any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "worktree_ready"
                for event in events
            )
            assert any(
                event.type == "text" and event.payload.get("text") == "hello from fake runtime"
                for event in events
            )
            assert any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "branch_pushed"
                for event in events
            )
            assert not any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "pr_created"
                for event in events
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            service.stop()
            relay.stop()
