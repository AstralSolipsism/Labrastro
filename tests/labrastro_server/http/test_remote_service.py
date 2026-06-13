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
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

import pytest


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

_GO_AVAILABLE = shutil.which("go") is not None

from labrastro_server.interfaces.http.remote.service import (
    RemoteRelayHTTPService as _RemoteRelayHTTPService,
    _SessionRunEventBuffer,
    _RemoteSessionRun,
)
from labrastro_server.interfaces.http.remote.routes.chat import (
    _session_run_events_handler_error_payload,
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
from reuleauxcoder.services.providers.stream_supervisor import ProviderStreamInterruptedError
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
    ExecutorRunResult,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader
from labrastro_server.interfaces.http.remote.protocol import (
    ChatCommandDispatchRequest,
    ChatCommandDispatchResponse,
    SessionRunStartRequest,
    CleanupResult,
    ExecToolResult,
    SessionDeleteRequest,
    SessionForkRequest,
    SessionListRequest,
    SessionLoadRequest,
    SessionNewRequest,
    ToolPreviewRequest,
    ToolPreviewResult,
    RelayEnvelope,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from reuleauxcoder.interfaces.entrypoint.runner import (
    _default_create_remote_artifact_provider,
)
from reuleauxcoder.interfaces.events import UIEventBus


TEST_ADMIN_TOKEN = "test-admin-token"


def test_session_run_events_handler_error_payload_preserves_remote_protocol_code() -> None:
    class RemoteProtocolLikeError(Exception):
        code = "REMOTE_PREVIEW_EMPTY"
        message = "remote peer preview did not include diff or sections"

    assert _session_run_events_handler_error_payload(RemoteProtocolLikeError("wrapped")) == {
        "message": "remote peer preview did not include diff or sections",
        "code": "REMOTE_PREVIEW_EMPTY",
    }


def test_session_run_events_handler_error_payload_preserves_provider_diagnostic() -> None:
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

    payload = _session_run_events_handler_error_payload(RemoteChatProviderError("wrapped"))

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

    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )

    session.append_event("session_run_start", {"prompt": "hi"})
    session.append_event("assistant_delta", {"content": "live-coalesced"})
    session.append_event(
        "document_draft_progress",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content_length": 5,
            "content_sha256": "progress-sha",
            "last_chunk_seq": 1,
        },
    )
    session.append_event(
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
    session.append_event("tool_call_stream", {"tool_call_id": "tool-1", "content": "live"})
    assert persisted == []

    session.append_event("remote_peer_ready", {"session_id": "session-1"})
    assert persisted == []

    session.enable_trace_persistence("session-1")
    session.append_event("assistant_message", {"content": "stored"})
    session.append_event("session_run_end", {"response": "done"})

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
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        locale="zh-CN",
    )

    session.append_event(
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


def test_remote_session_run_adds_server_enqueue_latency_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "labrastro_server.interfaces.http.remote.service.time.time",
        lambda: 10.25,
    )
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    session.append_event("stream_observability", {"emitted_at": 10.0})

    payload = session.events[0]["payload"]
    assert payload["server_enqueued_at"] == 10.25
    assert payload["server_enqueue_latency_ms"] == 250


def test_remote_session_run_uses_english_notice_for_english_locale(tmp_path: Path) -> None:
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        locale="en",
    )

    session.append_event(
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
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    session.append_event("assistant_delta", {"content": "A"})
    events, _done, cursor = session.wait_events(0, 0)
    assert [event["payload"]["content"] for event in events] == ["A"]

    session.append_event("assistant_delta", {"content": "B"})
    session.append_event("assistant_delta", {"content": "C"})
    events, _done, cursor = session.wait_events(cursor, 0)
    assert events == []

    events, _done, _cursor = session.wait_events(cursor, 0.2)
    assert [event["type"] for event in events] == ["assistant_delta"]
    assert events[0]["payload"]["content"] == "BC"


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

    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )
    session.enable_trace_persistence("session-1")

    session.append_live_event(
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

    events, _done, _cursor = session.wait_events(0, 0)
    assert [event["type"] for event in events] == ["document_draft_preview_chunk"]
    assert events[0]["payload"]["content"] == "A"
    assert persisted == []
    assert session.events == []


def test_remote_session_run_document_draft_preview_chunk_keeps_large_body_without_artifact(
    tmp_path: Path,
) -> None:
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        max_payload_bytes=32,
    )
    content = "live preview body " * 16

    session.append_live_event(
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

    events, _done, _cursor = session.wait_events(0, 0)

    assert [event["type"] for event in events] == ["document_draft_preview_chunk"]
    payload = events[0]["payload"]
    assert payload["content"] == content
    assert "artifact_ref" not in payload
    assert session.events == []


def test_remote_session_run_hydrates_large_document_draft_snapshot_for_stream(
    tmp_path: Path,
) -> None:
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        max_payload_bytes=128,
    )
    content = "# Architecture\n\n" + ("large body\n" * 64)

    session.append_event(
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

    events, _done, _cursor = session.wait_events(0, 0)

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

    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )
    session.enable_trace_persistence("session-1")
    content = "# Architecture\n\nTrace must not store this body."

    session.append_event(
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

    events, _done, _cursor = session.wait_events(0, 0)
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

    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        trace_event_sink=sink,
    )
    content = "# Architecture\n\nPending trace must not store this body."

    session.append_event(
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
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
        max_payload_bytes=128,
    )
    content = "# Architecture\n\n" + ("large body\n" * 64)
    session.append_event(
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

    status = session.status_payload(0)

    assert status["next_cursor"] == 1
    assert status["latest_seq"] == 1


def test_remote_session_run_interleaves_live_and_durable_events_without_false_loss(
    tmp_path: Path,
) -> None:
    session = _RemoteSessionRun(
        session_run_id="run-1",
        peer_id="peer-1",
        artifact_root=tmp_path,
    )

    session.append_live_event(
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
    session.append_event(
        "document_draft_progress",
        {
            "draft_id": "draft-1",
            "target_path": "docs/a.md",
            "content_length": 1,
            "content_sha256": "sha-a",
            "last_chunk_seq": 1,
        },
    )

    events, _done, cursor = session.wait_events(0, 0)

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
    def test_relay_send_preview_request_roundtrips_result(self) -> None:
        captured: list[RelayEnvelope] = []

        def send_fn(peer_id: str, envelope: RelayEnvelope) -> None:
            captured.append(envelope)
            relay.handle_inbound(
                peer_id,
                RelayEnvelope(
                    type="tool_preview_result",
                    request_id=envelope.request_id,
                    peer_id=peer_id,
                    payload=ToolPreviewResult(
                        ok=True,
                        sections=[
                            {
                                "id": "diff",
                                "kind": "diff",
                                "content": "--- a/a.txt\n+++ b/a.txt\n",
                            }
                        ],
                        resolved_path="/repo/a.txt",
                    ).to_dict(),
                ),
            )

        relay = RelayServer(send_fn=send_fn)
        relay.start()
        try:
            peer_id = relay.registry.register(
                {"features": ["tool_preview"], "cwd": "/repo"}
            )
            result = relay.send_preview_request(
                peer_id,
                ToolPreviewRequest(
                    tool_name="apply_patch",
                    args={
                        "patch": "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch",
                    },
                    cwd="/repo",
                ),
                timeout_sec=2,
            )

            assert captured[0].type == "preview_tool"
            assert result.ok is True
            assert result.sections[0]["kind"] == "diff"
            assert result.resolved_path == "/repo/a.txt"
        finally:
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
                == "builtin:fetch_capabilities"
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
            assert ingest_body["runtime_state"]["agent_id"] == "capability_packager"
            assert ingest_body["runtime_state"]["active_model_provider"] == "deepseek"
            assert any(
                event["type"] == "session_run_start"
                and event["session_id"] == "session-capability-1"
                and event["session_run_id"] == ingest_body["session_run_id"]
                and event["payload"]["workflow_mode"] == "capability_package_ingest"
                for event in persisted_trace_events
            )
            retry_status, retry_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/ingest/session/start",
                ingest_payload,
                headers=TEST_ADMIN_HEADERS,
            )
            assert retry_status == 200
            assert retry_body["session_run_id"] == ingest_body["session_run_id"]
            assert retry_body["session_id"] == ingest_body["session_id"]
            session_run_id = ingest_body["session_run_id"]
            session = service._get_session_run(session_run_id)
            assert session is not None
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
                f"{service.base_url}/remote/agent-runs/claim",
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
                f"{service.base_url}/remote/agent-runs/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": agent_run_id,
                    "request_id": claim["request_id"],
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
            control.complete_agent_run(
                str(agent_run_id),
                ExecutorRunResult(
                    task_id=str(agent_run_id),
                    status="completed",
                    output=f"```json\n{json.dumps(draft)}\n```",
                ),
            )
            approval = wait_for(
                lambda: next(
                    (
                        item
                        for item in service._get_session_run(session_run_id).status_payload().get("approvals", [])
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
                    issue_id="issue-1",
                    agent_id="capability_packager",
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
                f"{service.base_url}/remote/agent-runs/claim",
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
                f"{service.base_url}/remote/agent-runs/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-interrupted",
                    "request_id": claim["request_id"],
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
                    issue_id="issue-1",
                    agent_id="capability_packager",
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
                f"{service.base_url}/remote/agent-runs/claim",
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
                f"{service.base_url}/remote/agent-runs/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-stream",
                    "request_id": claim["request_id"],
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
                    issue_id="issue-1",
                    agent_id="capability_packager",
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
                f"{service.base_url}/remote/agent-runs/claim",
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
                f"{service.base_url}/remote/agent-runs/model-request",
                {
                    "peer_token": peer_token,
                    "agent_run_id": "task-model-interrupted",
                    "request_id": claim["request_id"],
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
                    issue_id="issue-1",
                    agent_id="capability_packager",
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
                f"{service.base_url}/remote/agent-runs/claim",
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
                    "worker_id": "worker-1",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            ).encode("utf-8")
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.settimeout(5)
            sock.sendall(
                b"POST /remote/agent-runs/model-request HTTP/1.1\r\n"
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

    def test_register_poll_result_disconnect_and_cleanup(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            bootstrap_token = relay.issue_bootstrap_token(ttl_sec=60)
            status, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": bootstrap_token,
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp",
                    "features": ["shell", "read_file"],
                },
            )
            assert status == 200
            assert register_body["type"] == "register_ok"
            payload = register_body["payload"]
            peer_id = payload["peer_id"]
            peer_token = payload["peer_token"]

            status, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/heartbeat",
                {"peer_token": peer_token, "ts": time.time()},
            )
            assert status == 200
            assert heartbeat_body["peer_id"] == peer_id

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "noop"

            result_holder: dict[str, object] = {}

            def run_exec() -> None:
                result_holder["result"] = relay.send_exec_request(
                    peer_id,
                    request=__import__(
                        "labrastro_server.interfaces.http.remote.protocol",
                        fromlist=["ExecToolRequest"],
                    ).ExecToolRequest(tool_name="shell", args={"command": "echo hi"}),
                    timeout_sec=2,
                )

            exec_thread = threading.Thread(target=run_exec)
            exec_thread.start()
            time.sleep(0.1)

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "exec_tool"
            assert poll_body["payload"]["tool_name"] == "shell"
            req_id = poll_body["request_id"]

            status, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/result",
                {
                    "peer_token": peer_token,
                    "request_id": req_id,
                    "type": "tool_result",
                    "payload": ExecToolResult(
                        ok=True, result="hello from peer"
                    ).to_dict(),
                },
            )
            assert status == 200
            assert result_body["ok"] is True
            exec_thread.join(timeout=2)
            assert result_holder["result"].result == "hello from peer"

            cleanup_holder: dict[str, object] = {}

            def run_cleanup() -> None:
                cleanup_holder["result"] = relay.request_cleanup(peer_id, timeout_sec=2)

            cleanup_thread = threading.Thread(target=run_cleanup)
            cleanup_thread.start()
            time.sleep(0.1)

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "cleanup"
            cleanup_req_id = poll_body["request_id"]

            status, cleanup_body = _json_request(
                "POST",
                f"{service.base_url}/remote/result",
                {
                    "peer_token": peer_token,
                    "request_id": cleanup_req_id,
                    "type": "cleanup_result",
                    "payload": CleanupResult(
                        ok=True, removed_items=["/tmp/rc-peer"]
                    ).to_dict(),
                },
            )
            assert status == 200
            assert cleanup_body["ok"] is True
            cleanup_thread.join(timeout=2)
            assert cleanup_holder["result"].ok is True
            assert cleanup_holder["result"].removed_items == ["/tmp/rc-peer"]

            status, disconnect_body = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "peer_initiated"},
            )
            assert status == 200
            assert disconnect_body["ok"] is True
            assert relay.registry.get(peer_id) is None
        finally:
            service.stop()
            relay.stop()

    def test_all_remote_builtin_tools_dispatch_over_http_contract(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
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

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id
            cases = [
                (
                    ShellTool(backend=backend),
                    {
                        "command": "echo hello",
                        "intent": "输出 hello 验证远端 shell 工具调度。",
                    },
                    "shell",
                    "shell-ok",
                ),
                (
                    ReadFileTool(backend=backend),
                    {"file_path": "/tmp/demo.txt"},
                    "read_file",
                    "read-ok",
                ),
                (
                    ApplyPatchTool(backend=backend),
                    {
                        "patch": "*** Begin Patch\n*** Update File: demo.txt\n@@\n-old\n+new\n*** End Patch",
                    },
                    "apply_patch",
                    "patch-ok",
                ),
                (
                    GlobTool(backend=backend),
                    {"pattern": "*.py", "path": "/tmp"},
                    "glob",
                    "glob-ok",
                ),
                (
                    GrepTool(backend=backend),
                    {"pattern": "hello", "path": "/tmp"},
                    "grep",
                    "grep-ok",
                ),
            ]

            for tool, kwargs, expected_name, expected_result in cases:
                holder: dict[str, object] = {}

                def run_tool(current_tool=tool, current_kwargs=kwargs) -> None:
                    holder["result"] = current_tool.execute(**current_kwargs)

                t = threading.Thread(target=run_tool)
                t.start()
                time.sleep(0.1)

                status, poll_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/poll",
                    {"peer_token": peer_token},
                )
                assert status == 200
                if expected_name == "apply_patch":
                    assert poll_body["type"] == "preview_tool"
                    assert poll_body["payload"]["tool_name"] == expected_name
                    for key, value in kwargs.items():
                        assert poll_body["payload"]["args"][key] == value
                    preview_identity = {
                        "plan_id": "remote-contract-plan",
                        "candidate_hash": "remote-contract-candidate",
                        "tool_name": "apply_patch",
                        "workspace_id": "/tmp/peer",
                        "execution_target": "remote_peer",
                        "path_space": "remote_peer_workspace",
                        "args_hash": "remote-contract-args",
                    }
                    status, preview_body = _json_request(
                        "POST",
                        f"{service.base_url}/remote/result",
                        {
                            "peer_token": peer_token,
                            "request_id": poll_body["request_id"],
                            "type": "tool_preview_result",
                            "payload": ToolPreviewResult(
                                ok=True,
                                sections=[
                                    {
                                        "id": "diff",
                                        "kind": "diff",
                                        "content": "--- a/demo.txt\n+++ b/demo.txt\n-old\n+new\n",
                                        "resolved_path": "/tmp/peer/demo.txt",
                                    }
                                ],
                                meta={
                                    "preview_identity": preview_identity,
                                    "approved_save_candidate": {
                                        "tool_name": "apply_patch",
                                        "preview_identity": preview_identity,
                                        "operations": [
                                            {
                                                "kind": "update",
                                                "path": "demo.txt",
                                                "new_content": "new\n",
                                            }
                                        ],
                                    },
                                },
                            ).to_dict(),
                        },
                    )
                    assert status == 200
                    assert preview_body["ok"] is True
                    status, poll_body = _json_request(
                        "POST",
                        f"{service.base_url}/remote/poll",
                        {"peer_token": peer_token},
                    )
                    assert status == 200
                assert poll_body["type"] == "exec_tool"
                assert poll_body["payload"]["tool_name"] == expected_name
                if expected_name == "apply_patch":
                    assert poll_body["payload"]["args"] == {}
                    assert poll_body["payload"]["preview_identity"]["plan_id"] == (
                        "remote-contract-plan"
                    )
                    assert poll_body["payload"]["approved_save_candidate"]["operations"] == [
                        {
                            "kind": "update",
                            "path": "demo.txt",
                            "new_content": "new\n",
                        }
                    ]
                else:
                    for key, value in kwargs.items():
                        assert poll_body["payload"]["args"][key] == value

                status, result_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/result",
                    {
                        "peer_token": peer_token,
                        "request_id": poll_body["request_id"],
                        "type": "tool_result",
                        "payload": ExecToolResult(
                            ok=True, result=expected_result
                        ).to_dict(),
                    },
                )
                assert status == 200
                assert result_body["ok"] is True

                t.join(timeout=2)
                assert holder["result"] == expected_result
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
                _peer_register_payload(relay),
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
                _peer_register_payload(relay),
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

    def test_disconnect_aborts_active_stream_session_run(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            session.append_event("session_run_start", {"prompt": _prompt})
            # Wait long enough so test can force disconnect first.
            session.wait_approval("hold", timeout_sec=2)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {
                    "peer_token": peer_token,
                    "prompt": "long-run",
                },
            )
            session_run_id = start_body["session_run_id"]

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "test_disconnect"},
            )
            assert status == 200

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            assert stream_body["done"] is True
            event_types = [event["type"] for event in stream_body["events"]]
            assert "session_run_start" in event_types
            assert "error" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_disconnect_resolves_registered_pending_approval(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        approval_ready = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            approval_id = "approval-disconnect"
            payload = {
                "approval_id": approval_id,
                "tool_call_id": "call-disconnect",
                "tool_name": "shell",
                "tool_source": "builtin",
                "reason": "need approval",
                "tool_args": {"command": "echo hi"},
            }
            session.register_approval(approval_id, payload)
            session.append_event("approval_request", payload)
            approval_ready.set()
            session.wait_approval(approval_id, timeout_sec=2)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "needs approval"},
            )
            session_run_id = start_body["session_run_id"]
            assert approval_ready.wait(2)

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "peer_shutdown"},
            )
            assert status == 200

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            resolved_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "approval_resolved"
            ]
            assert len(resolved_events) == 1
            payload = resolved_events[0]["payload"]
            assert payload["server_enqueued_at"] > 0
            assert {
                key: payload[key]
                for key in ("approval_id", "tool_call_id", "decision", "reason")
            } == {
                "approval_id": "approval-disconnect",
                "tool_call_id": "call-disconnect",
                "decision": "deny_once",
                "reason": "peer_disconnected: peer_shutdown",
            }
            assert stream_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_preserves_requested_mode_on_stream_session(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        seen: dict[str, object] = {}

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            seen["mode"] = session.mode
            seen["locale"] = session.locale
            seen["mentions"] = session.mentions
            session.append_event(
                "session_run_start",
                {
                    "prompt": _prompt,
                    "mode": session.mode,
                    "locale": session.locale,
                    "mentions": session.mentions,
                },
            )
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {
                    "peer_token": peer_token,
                    "prompt": "use planner mode",
                    "mode": "planner",
                    "locale": "zh-CN",
                    "mentions": [
                        {
                            "kind": "file",
                            "name": "README.md",
                            "path": "README.md",
                            "source": "workspace_files",
                        }
                    ],
                },
            )
            session_run_id = start_body["session_run_id"]

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)

            assert stream_body["done"] is True
            assert seen["mode"] == "planner"
            assert seen["locale"] == "zh-CN"
            assert seen["mentions"] == [
                {
                    "kind": "file",
                    "name": "README.md",
                    "path": "README.md",
                    "source": "workspace_files",
                }
            ]
            session_run_start = next(
                event for event in stream_body["events"] if event["type"] == "session_run_start"
            )
            assert session_run_start["payload"]["mentions"] == seen["mentions"]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_is_idempotent_for_client_request_id(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        release = threading.Event()
        starts: list[str] = []

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            starts.append(session.session_run_id)
            release.wait(timeout=2)
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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
            payload = {
                "peer_token": peer_token,
                "prompt": "same request",
                "session_hint": "session-1",
                "client_request_id": "req-1",
            }

            _, first = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                payload,
            )
            _, second = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                payload,
            )

            assert second["session_run_id"] == first["session_run_id"]
            deadline = time.time() + 1
            while len(starts) < 1 and time.time() < deadline:
                time.sleep(0.01)
            assert len(starts) == 1
        finally:
            release.set()
            service.stop()
            relay.stop()

    def test_register_rejects_missing_runtime_context_when_required(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            require_peer_runtime_context=True,
        )
        service.start()
        try:
            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/register",
                    {
                        "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                        "cwd": "/tmp/peer",
                    },
                )
            assert excinfo.value.code == 400
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert body["error"] == "invalid_peer_runtime_context"
            assert "host_info_min.shell" in body["details"]["missing"]
            assert "workspace_root" in body["details"]["missing"]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_requires_model_for_new_sessions_when_required(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
            require_explicit_chat_model=True,
            require_peer_runtime_context=True,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]

            with pytest.raises(HTTPError) as excinfo:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/start",
                    {"peer_token": peer_token, "prompt": "hello"},
                )
            assert excinfo.value.code == 400
            body = json.loads(excinfo.value.read().decode("utf-8"))
            assert body["error"] == "model_selection_required"

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {
                    "peer_token": peer_token,
                    "prompt": "hello",
                    "provider_id": "deepseek",
                    "model_id": "V4FLASH",
                },
            )
            assert start_body["session_run_id"]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_allows_existing_session_runtime_model_when_required(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_handler(action: str, _peer_id: str, payload: dict) -> dict:
            if action == "load" and payload.get("session_id") == "session-1":
                return {
                    "ok": True,
                    "runtime_state": {
                        "active_model_provider": "deepseek",
                        "active_model": "V4FLASH",
                    },
                }
            return {"ok": False, "error": "session_not_found", "_status": 404}

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
            session_handler=session_handler,
            require_explicit_chat_model=True,
            require_peer_runtime_context=True,
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
                    "prompt": "continue",
                    "session_hint": "session-1",
                },
            )
            assert start_body["session_run_id"]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_status_reports_running_done_and_error(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        running_started = threading.Event()
        release_running = threading.Event()

        def session_run_events_handler(_peer_id: str, prompt: str, session) -> None:
            session.append_event("session_run_start", {"prompt": prompt})
            if prompt == "boom":
                session.append_event("error", {"message": "intentional_failure"})
                return
            running_started.set()
            release_running.wait(timeout=2)
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "hold"},
            )
            session_run_id = start_body["session_run_id"]
            assert running_started.wait(2)

            _, running_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {"peer_token": peer_token, "session_run_id": session_run_id, "cursor": 0},
            )
            assert running_status["ok"] is True
            assert running_status["status"] == "running"
            assert running_status["running"] is True
            assert running_status["done"] is False
            assert running_status["reconnectable"] is True
            assert running_status["latest_seq"] >= 1

            release_running.set()
            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id, timeout_sec=2)
            assert stream_body["done"] is True

            _, done_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {"peer_token": peer_token, "session_run_id": session_run_id, "cursor": 0},
            )
            assert done_status["status"] == "done"
            assert done_status["running"] is False
            assert done_status["done"] is True
            assert done_status["reconnectable"] is False

            _, error_start = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "boom"},
            )
            error_stream = _session_run_events_body(
                service.base_url,
                peer_token,
                error_start["session_run_id"],
                timeout_sec=2,
            )
            assert error_stream["done"] is True

            _, error_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {
                    "peer_token": peer_token,
                    "session_run_id": error_start["session_run_id"],
                    "cursor": 0,
                },
            )
            assert error_status["status"] == "error"
            assert error_status["done"] is True
            assert error_status["error"] == "intentional_failure"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_start_reports_peer_registration_setup_errors(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, _session) -> None:
            raise ValueError("remote peer registration missing host_info_min.shell")

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            stream_body = _session_run_events_body(
                service.base_url,
                peer_token,
                start_body["session_run_id"],
            )

            error_event = [
                event for event in stream_body["events"] if event["type"] == "error"
            ][-1]
            error_payload = error_event["payload"]
            assert error_payload["server_enqueued_at"] > 0
            assert {
                key: error_payload[key]
                for key in ("message", "code")
            } == {
                "message": "remote peer registration missing host_info_min.shell",
                "code": "session_run_handler_failed",
            }
            failed_event = [
                event for event in stream_body["events"] if event["type"] == "session_run_failed"
            ][-1]
            failed_payload = failed_event["payload"]
            assert failed_payload["server_enqueued_at"] > 0
            assert {
                key: failed_payload[key]
                for key in ("message", "code", "recoverable")
            } == {
                "message": "remote peer registration missing host_info_min.shell",
                "code": "session_run_handler_failed",
                "recoverable": False,
            }
        finally:
            service.stop()
            relay.stop()

    def test_session_run_recover_consumes_recovery_ticket_and_restarts_stream(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        prompts: list[str] = []

        def session_run_events_handler(_peer_id: str, prompt: str, session) -> None:
            prompts.append(prompt)
            if len(prompts) == 1:
                payload = {
                    "message": "provider stream interrupted",
                    "response": "partial answer",
                    "recoverable": True,
                    "recovery_actions": ["continue", "retry"],
                }
                session.register_recovery(payload)
                session.append_event("session_run_interrupted", payload)
                return
            session.append_event("assistant_delta", {"content": " resumed"})
            session.append_event("session_run_end", {"response": "partial answer resumed"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "initial"},
            )
            session_run_id = start_body["session_run_id"]
            first_stream = _session_run_events_first_body(
                service.base_url,
                peer_token,
                session_run_id,
            )
            assert any(event["type"] == "session_run_interrupted" for event in first_stream["events"])

            _, recover_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/recover",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "action": "continue",
                },
            )
            assert recover_body == {
                "ok": True,
                "session_run_id": session_run_id,
                "error": None,
                "state": "consumed",
            }

            second_stream = _session_run_events_body(
                service.base_url,
                peer_token,
                session_run_id,
                cursor=first_stream["next_cursor"],
            )
            assert prompts[0] == "initial"
            assert "<stream_recovery>" in prompts[1]
            assert any(event["type"] == "session_run_recovery_start" for event in second_stream["events"])
            assert any(event["type"] == "session_run_end" for event in second_stream["events"])
        finally:
            service.stop()
            relay.stop()

    def test_session_run_events_cursor_resume_reads_events_created_between_connections(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        first_delta_sent = threading.Event()
        release_second_delta = threading.Event()
        finished = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("session_run_start", {"prompt": _prompt})
                session.append_event("delta", {"text": "first"})
                first_delta_sent.set()
                release_second_delta.wait(timeout=2)
                session.append_event("delta", {"text": "second"})
                session.append_event("session_run_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "resume"},
            )
            session_run_id = start_body["session_run_id"]
            assert first_delta_sent.wait(2)

            first_batch = _session_run_events_first_body(
                service.base_url,
                peer_token,
                session_run_id,
                timeout_sec=0,
            )
            assert first_batch["done"] is False
            assert [event["type"] for event in first_batch["events"]] == [
                "session_run_start",
                "delta",
            ]
            first_cursor = first_batch["next_cursor"]

            release_second_delta.set()
            assert finished.wait(2)
            resumed_batch = _session_run_events_body(
                service.base_url,
                peer_token,
                session_run_id,
                cursor=first_cursor,
            )

            assert resumed_batch["done"] is True
            assert [event["type"] for event in resumed_batch["events"]] == [
                "delta",
                "session_run_end",
            ]
            assert resumed_batch["events"][0]["payload"]["text"] == "second"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_events_sse_streams_chat_response_frames(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        first_delta_sent = threading.Event()
        release_second_delta = threading.Event()
        finished = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("session_run_start", {"prompt": _prompt})
                session.append_event("delta", {"text": "first"})
                first_delta_sent.set()
                release_second_delta.wait(timeout=2)
                session.append_event("delta", {"text": "second"})
                session.append_event("session_run_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "sse"},
            )
            session_run_id = start_body["session_run_id"]
            assert first_delta_sent.wait(2)

            result: dict[str, object] = {}

            def read_stream() -> None:
                status, content_type, raw_body, frames = _sse_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/events",
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "cursor": 0,
                        "timeout_sec": 0.05,
                    },
                )
                result.update(
                    {
                        "status": status,
                        "content_type": content_type,
                        "raw_body": raw_body,
                        "frames": frames,
                    }
                )

            reader = threading.Thread(target=read_stream)
            reader.start()
            time.sleep(0.2)
            release_second_delta.set()
            reader.join(timeout=5)

            assert finished.wait(2)
            assert not reader.is_alive()
            assert result["status"] == 200
            assert str(result["content_type"]).startswith("text/event-stream")
            assert ": ping" in str(result["raw_body"])
            frames = result["frames"]
            assert isinstance(frames, list)
            assert [frame["event"] for frame in frames] == ["session_run", "session_run"]
            assert [event["type"] for event in frames[0]["data"]["events"]] == [
                "session_run_start",
                "delta",
            ]
            assert frames[0]["data"]["done"] is False
            assert [event["type"] for event in frames[1]["data"]["events"]] == [
                "delta",
                "session_run_end",
            ]
            assert frames[1]["data"]["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_events_sse_resumes_from_cursor(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        first_delta_sent = threading.Event()
        release_second_delta = threading.Event()
        finished = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": "first"})
                first_delta_sent.set()
                release_second_delta.wait(timeout=2)
                session.append_event("delta", {"text": "second"})
                session.append_event("session_run_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "resume-sse"},
            )
            session_run_id = start_body["session_run_id"]
            assert first_delta_sent.wait(2)

            first_batch = _session_run_events_first_body(
                service.base_url,
                peer_token,
                session_run_id,
                timeout_sec=0,
            )
            first_cursor = first_batch["next_cursor"]

            release_second_delta.set()
            assert finished.wait(2)
            status, content_type, _raw_body, frames = _sse_request(
                "POST",
                f"{service.base_url}/remote/session-runs/events",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "cursor": first_cursor,
                    "timeout_sec": 1,
                },
            )

            assert status == 200
            assert content_type.startswith("text/event-stream")
            assert len(frames) == 1
            assert [event["type"] for event in frames[0]["data"]["events"]] == [
                "delta",
                "session_run_end",
            ]
            assert frames[0]["data"]["events"][0]["payload"]["text"] == "second"
            assert frames[0]["data"]["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_allows_any_valid_peer_token_for_existing_session_run(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("session_run_start", {"prompt": _prompt})
                session.append_event("delta", {"text": "after-reconnect"})
                session.append_event("session_run_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
        )
        service.start()
        try:
            _, first_register = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer-a",
                },
            )
            _, replacement_register = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer-b",
                },
            )
            first_token = first_register["payload"]["peer_token"]
            replacement_token = replacement_register["payload"]["peer_token"]
            assert first_register["payload"]["peer_id"] != replacement_register["payload"]["peer_id"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": first_token, "prompt": "resume from replacement"},
            )
            assert finished.wait(2)

            stream_body = _session_run_events_body(
                service.base_url,
                replacement_token,
                start_body["session_run_id"],
            )

            assert stream_body["done"] is True
            assert [event["type"] for event in stream_body["events"]] == [
                "session_run_start",
                "delta",
                "session_run_end",
            ]
            assert stream_body["events"][1]["payload"]["text"] == "after-reconnect"

            status_code, status_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {"peer_token": replacement_token, "session_run_id": start_body["session_run_id"]},
            )
            assert status_code == 200
            assert status_body["session_run_id"] == start_body["session_run_id"]
            assert status_body["peer_id"] == first_register["payload"]["peer_id"]
        finally:
            service.stop()
            relay.stop()

    def test_session_run_control_rejects_invalid_token_and_missing_run(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=lambda _peer_id, _prompt, _session: None,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                _peer_register_payload(relay),
            )
            peer_token = register_body["payload"]["peer_token"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/status",
                    {"peer_token": "bad-token", "session_run_id": "missing-run"},
                )
                raise AssertionError("expected invalid peer token to fail")
            except HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 401
                assert body["error"] == "invalid_peer_token"

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/session-runs/status",
                    {"peer_token": peer_token, "session_run_id": "missing-run"},
                )
                raise AssertionError("expected missing session run to fail")
            except HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 404
                assert body["error"] == "session_run_not_found"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_cancel_adds_terminal_event_and_marks_done(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            session.wait_approval("hold", timeout_sec=2)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "cancel me"},
            )
            session_run_id = start_body["session_run_id"]

            _, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/cancel",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "reason": "user_cancelled",
                },
            )
            assert cancel_body["ok"] is True

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            assert stream_body["done"] is True
            event_types = [event["type"] for event in stream_body["events"]]
            assert "session_run_cancel_requested" in event_types
            assert "session_run_cancelled" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_session_run_cancel_resolves_registered_pending_approval(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        approval_ready = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            approval_id = "approval-cancel"
            payload = {
                "approval_id": approval_id,
                "tool_call_id": "call-cancel",
                "tool_name": "shell",
                "tool_source": "builtin",
                "reason": "need approval",
                "tool_args": {"command": "echo hi"},
            }
            session.register_approval(approval_id, payload)
            session.append_event("approval_request", payload)
            approval_ready.set()
            decision, resolution_reason = session.wait_approval(approval_id, timeout_sec=2)
            session.append_event(
                "approval_resolved",
                {
                    "approval_id": approval_id,
                    "tool_call_id": "call-cancel",
                    "decision": decision,
                    "reason": resolution_reason,
                },
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "cancel me"},
            )
            session_run_id = start_body["session_run_id"]
            assert approval_ready.wait(2)

            _, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/cancel",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "reason": "user_cancelled",
                },
            )
            assert cancel_body["ok"] is True

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            resolved_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "approval_resolved"
            ]
            assert len(resolved_events) == 1
            payload = resolved_events[0]["payload"]
            assert payload["server_enqueued_at"] > 0
            assert {
                key: payload[key]
                for key in ("approval_id", "tool_call_id", "decision", "reason")
            } == {
                "approval_id": "approval-cancel",
                "tool_call_id": "call-cancel",
                "decision": "deny_once",
                "reason": "user_cancelled",
            }
            assert any(event["type"] == "session_run_cancelled" for event in stream_body["events"])
            assert stream_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_follow_up_can_be_consumed_at_safe_boundary(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        handler_ready = threading.Event()
        consumed = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            session.set_follow_up_callback(
                lambda ticket: (
                    session.mark_follow_up_consumed(ticket["followup_id"]),
                    consumed.set(),
                )
            )
            handler_ready.set()
            consumed.wait(timeout=2)
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "guide me"},
            )
            session_run_id = start_body["session_run_id"]
            assert handler_ready.wait(2)

            _, follow_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/follow-up",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "followup_id": "follow-1",
                    "client_request_id": "pending-1",
                    "text": "prefer the shorter path",
                },
            )
            assert follow_body["ok"] is True
            assert follow_body["followup_id"] == "follow-1"
            assert follow_body["state"] in {"pending", "consumed"}
            assert consumed.wait(2)

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            event_types = [event["type"] for event in stream_body["events"]]
            assert "session_run_follow_up_accepted" in event_types
            assert "session_run_follow_up_consumed" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_session_run_follow_up_unconsumed_when_run_finishes_without_boundary(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        handler_ready = threading.Event()
        release = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            handler_ready.set()
            release.wait(timeout=2)
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "pure stream"},
            )
            session_run_id = start_body["session_run_id"]
            assert handler_ready.wait(2)

            _, follow_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/follow-up",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "followup_id": "follow-1",
                    "text": "late guidance",
                },
            )
            assert follow_body["ok"] is True
            release.set()

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            events = stream_body["events"]
            unconsumed = [
                event
                for event in events
                if event["type"] == "session_run_follow_up_unconsumed"
            ]
            assert unconsumed
            assert unconsumed[-1]["payload"]["followup_id"] == "follow-1"
        finally:
            release.set()
            service.stop()
            relay.stop()

    def test_session_run_follow_up_cancel_marks_ticket_cancelled(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        handler_ready = threading.Event()
        release = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            handler_ready.set()
            release.wait(timeout=2)
            session.append_event("session_run_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "cancel follow up"},
            )
            session_run_id = start_body["session_run_id"]
            assert handler_ready.wait(2)

            _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/follow-up",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "followup_id": "follow-1",
                    "text": "temporary guidance",
                },
            )
            _, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/follow-up/cancel",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "followup_id": "follow-1",
                    "reason": "user_changed_to_queue",
                },
            )
            assert cancel_body["ok"] is True
            assert cancel_body["state"] == "cancelled"
            release.set()

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            cancelled = [
                event
                for event in stream_body["events"]
                if event["type"] == "session_run_follow_up_cancelled"
            ]
            assert cancelled
            assert cancelled[-1]["payload"]["reason"] == "user_changed_to_queue"
        finally:
            release.set()
            service.stop()
            relay.stop()

    def test_session_run_events_reports_lost_events_when_buffer_pruned(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                for idx in range(6):
                    session.append_event("delta", {"idx": idx})
                session.append_event("session_run_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
            session_run_max_events=3,
            session_run_artifact_root=tmp_path / "session-run-events",
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "overflow"},
            )
            assert finished.wait(2)

            stream_body = _session_run_events_body(
                service.base_url,
                peer_token,
                start_body["session_run_id"],
            )

            events = stream_body["events"]
            assert events[0]["type"] == "events_lost"
            assert events[0]["payload"]["first_available_seq"] > 1
            assert events[0]["payload"]["dropped_count"] >= 1
            assert [event["type"] for event in events[1:]] == [
                "delta",
                "delta",
                "session_run_end",
            ]
            assert [event["payload"].get("idx") for event in events if event["type"] == "delta"] == [4, 5]
            assert stream_body["next_cursor"] == events[-1]["seq"]
            assert stream_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_events_spills_oversized_payload_to_gzip_artifact(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()
        large_text = "x" * 512

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": large_text})
                session.append_event("session_run_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
            session_run_max_payload_bytes=128,
            session_run_artifact_root=tmp_path / "session-run-events",
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "large"},
            )
            assert finished.wait(2)

            stream_body = _session_run_events_body(
                service.base_url,
                peer_token,
                start_body["session_run_id"],
            )

            delta_event = next(
                event for event in stream_body["events"] if event["type"] == "delta"
            )
            artifact = delta_event["payload"]["artifact_ref"]
            artifact_path = Path(artifact["path"])
            assert artifact["encoding"] == "json+gzip"
            assert artifact["bytes"] > 128
            assert artifact_path.exists()
            with gzip.open(artifact_path, "rt", encoding="utf-8") as fh:
                assert json.load(fh) == {"text": large_text}
        finally:
            service.stop()
            relay.stop()

    def test_session_run_artifact_envelope_preserves_file_change_identity(
        self, tmp_path: Path
    ) -> None:
        buffer = _SessionRunEventBuffer(
            session_run_id="run-1",
            artifact_root=tmp_path / "session-run-events",
            max_events=20,
            max_payload_bytes=128,
            max_total_bytes=4096,
        )
        large_diff = "--- a/src/a.ts\n+++ b/src/a.ts\n" + "\n".join(
            f"+line-{index}" for index in range(80)
        )

        buffer.append({
            "session_run_id": "run-1",
            "seq": 1,
            "type": "file_change_patch_updated",
            "payload": {
                "item_id": "file-change-1",
                "tool_call_id": "tool-1",
                "approval_id": "approval-1",
                "draft_id": "draft-1",
                "status": "in_progress",
                "changes": [{"path": "src/a.ts", "kind": "update", "diff": large_diff}],
                "patch_preview": large_diff,
            },
        })

        event = buffer.snapshot()[0]
        payload = event["payload"]
        assert payload["item_id"] == "file-change-1"
        assert payload["tool_call_id"] == "tool-1"
        assert payload["approval_id"] == "approval-1"
        assert payload["draft_id"] == "draft-1"
        assert payload["status"] == "in_progress"
        assert "changes" not in payload
        assert "patch_preview" not in payload
        artifact = payload["artifact_ref"]
        assert artifact["encoding"] == "json+gzip"
        assert artifact["fields"] == ["changes", "patch_preview"]
        with gzip.open(Path(artifact["path"]), "rt", encoding="utf-8") as fh:
            stored = json.load(fh)
        assert stored["changes"][0]["diff"] == large_diff
        assert stored["patch_preview"] == large_diff

    def test_session_run_gc_removes_closed_idle_sessions_and_artifacts(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": "x" * 512})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
            session_run_max_payload_bytes=128,
            session_run_artifact_root=tmp_path / "session-run-events",
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "gc"},
            )
            session_run_id = start_body["session_run_id"]
            assert finished.wait(2)

            stream_body = _session_run_events_first_body(
                service.base_url,
                peer_token,
                session_run_id,
            )
            delta_event = next(
                event for event in stream_body["events"] if event["type"] == "delta"
            )
            artifact_path = Path(delta_event["payload"]["artifact_ref"]["path"])
            assert artifact_path.exists()

            session = service._get_session_run(session_run_id)
            assert session is not None
            service._session_run_closed_ttl_sec = 0
            session.finished_at = time.time() - 1
            service._gc_session_runs()

            assert service._get_session_run(session_run_id) is None
            assert not artifact_path.exists()
        finally:
            service.stop()
            relay.stop()

    def test_approval_reply_routes_to_matching_session_run_only(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            approval_id = "approval-1"
            approval_payload = {
                "approval_id": approval_id,
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "tool_source": "builtin",
                "reason": "need approval",
                "tool_args": {"command": "echo hi"},
                "sections": [{"id": "command", "kind": "text", "content": "echo hi"}],
                "content": "approve echo hi",
            }
            session.register_approval(approval_id, approval_payload)
            session.append_event(
                "approval_request",
                approval_payload,
            )
            decision, reason = session.wait_approval(approval_id, timeout_sec=2)
            session.append_event(
                "approval_resolved",
                {"approval_id": approval_id, "decision": decision, "reason": reason},
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "approve me"},
            )
            session_run_id = start_body["session_run_id"]

            stream_body = _session_run_events_first_body(
                service.base_url,
                peer_token,
                session_run_id,
            )
            approval_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "approval_request"
            ]
            assert approval_events
            approval_id = approval_events[0]["payload"]["approval_id"]

            _, pending_status = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {"peer_token": peer_token, "session_run_id": session_run_id, "cursor": 0},
            )
            assert pending_status["approvals"] == [
                {
                    "approval_id": approval_id,
                    "tool_call_id": "call-1",
                    "tool_name": "shell",
                    "tool_source": "builtin",
                    "reason": "need approval",
                    "tool_args": {"command": "echo hi"},
                    "sections": [{"id": "command", "kind": "text", "content": "echo hi"}],
                    "content": "approve echo hi",
                    "state": "requested",
                }
            ]

            status, reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "approval_id": approval_id,
                    "decision": "allow_once",
                    "reason": "ok",
                },
            )
            assert status == 200
            assert reply_body["ok"] is True

            resolved_body = _session_run_events_body(
                service.base_url,
                peer_token,
                session_run_id,
                cursor=stream_body["next_cursor"],
            )
            resolved_events = [
                event
                for event in resolved_body["events"]
                if event["type"] == "approval_resolved"
            ]
            assert resolved_events
            assert resolved_events[0]["payload"]["decision"] == "allow_once"
            assert resolved_body["done"] is True

            status, duplicate_reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "approval_id": approval_id,
                    "decision": "allow_once",
                    "reason": "ok",
                },
            )
            assert status == 200
            assert duplicate_reply_body["ok"] is True
            assert duplicate_reply_body["state"] == "already_resolved"

            bad_session_run_req = request.Request(
                f"{service.base_url}/remote/approval/reply",
                data=json.dumps(
                    {
                        "peer_token": peer_token,
                        "session_run_id": "missing-run",
                        "approval_id": approval_id,
                        "decision": "allow_once",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(bad_session_run_req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "session_run_not_found"

            bad_approval_req = request.Request(
                f"{service.base_url}/remote/approval/reply",
                data=json.dumps(
                    {
                        "peer_token": peer_token,
                        "session_run_id": session_run_id,
                        "approval_id": "missing-approval",
                        "decision": "allow_once",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(bad_approval_req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "approval_not_found"
        finally:
            service.stop()
            relay.stop()

    def test_session_run_user_input_reply_routes_structured_mcp_elicitation_content(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            input_id = "mcp-elicitation-1"
            request_payload = {
                "input_id": input_id,
                "kind": "mcp_elicitation",
                "event_name": "Elicitation",
                "mcp_server": "docs",
                "tool_name": "lookup",
                "tool_call_id": "tool-call-1",
                "message": "Pick a format",
                "input_schema": {
                    "type": "object",
                    "properties": {"format": {"type": "string"}},
                },
            }
            session.register_user_input(input_id, request_payload)
            session.append_event("user_input_request", request_payload)
            action, content, reason = session.wait_user_input(input_id, timeout_sec=2)
            session.append_event(
                "user_input_resolved",
                {
                    "input_id": input_id,
                    "kind": "mcp_elicitation",
                    "action": action,
                    "content": content,
                    "reason": reason,
                },
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "ask mcp"},
            )
            session_run_id = start_body["session_run_id"]

            stream_body = _session_run_events_first_body(
                service.base_url,
                peer_token,
                session_run_id,
            )
            input_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "user_input_request"
            ]
            assert input_events
            input_id = input_events[0]["payload"]["input_id"]

            status, reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/user-input/reply",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "input_id": input_id,
                    "action": "accept",
                    "content": {"format": "markdown"},
                    "reason": "chosen",
                },
            )
            assert status == 200
            assert reply_body["ok"] is True

            resolved_body = _session_run_events_body(
                service.base_url,
                peer_token,
                session_run_id,
                cursor=stream_body["next_cursor"],
            )
            resolved_events = [
                event
                for event in resolved_body["events"]
                if event["type"] == "user_input_resolved"
            ]
            assert resolved_events
            payload = resolved_events[0]["payload"]
            assert payload["server_enqueued_at"] > 0
            assert {
                key: payload[key]
                for key in ("input_id", "kind", "action", "content", "reason")
            } == {
                "input_id": input_id,
                "kind": "mcp_elicitation",
                "action": "accept",
                "content": {"format": "markdown"},
                "reason": "chosen",
            }
            assert resolved_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_status_reports_pending_mcp_user_inputs(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        input_ready = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            input_id = "mcp-elicitation-status"
            request_payload = {
                "input_id": input_id,
                "kind": "mcp_elicitation",
                "event_name": "Elicitation",
                "mcp_server": "docs",
                "tool_name": "lookup",
                "tool_call_id": "tool-call-status",
                "message": "Pick a format",
                "input_schema": {
                    "type": "object",
                    "properties": {"format": {"type": "string"}},
                },
            }
            session.register_user_input(input_id, request_payload)
            session.append_event("user_input_request", request_payload)
            input_ready.set()
            action, content, reason = session.wait_user_input(input_id, timeout_sec=5)
            session.append_event(
                "user_input_resolved",
                {
                    "input_id": input_id,
                    "kind": "mcp_elicitation",
                    "action": action,
                    "content": content,
                    "reason": reason,
                },
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "ask mcp"},
            )
            session_run_id = start_body["session_run_id"]
            assert input_ready.wait(2)

            status, status_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/status",
                {"peer_token": peer_token, "session_run_id": session_run_id, "cursor": 0},
            )
            assert status == 200
            assert status_body["user_inputs"] == [
                {
                    "input_id": "mcp-elicitation-status",
                    "kind": "mcp_elicitation",
                    "event_name": "Elicitation",
                    "mcp_server": "docs",
                    "tool_name": "lookup",
                    "tool_call_id": "tool-call-status",
                    "message": "Pick a format",
                    "input_schema": {
                        "type": "object",
                        "properties": {"format": {"type": "string"}},
                    },
                    "state": "requested",
                }
            ]

            _, reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/user-input/reply",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "input_id": "mcp-elicitation-status",
                    "action": "decline",
                    "reason": "test_cleanup",
                },
            )
            assert reply_body["ok"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_cancel_resolves_registered_pending_user_input(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        input_ready = threading.Event()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            input_id = "mcp-elicitation-cancel"
            payload = {
                "input_id": input_id,
                "kind": "mcp_elicitation",
                "event_name": "Elicitation",
                "message": "Pick a format",
            }
            session.register_user_input(input_id, payload)
            session.append_event("user_input_request", payload)
            input_ready.set()
            action, content, reason = session.wait_user_input(input_id, timeout_sec=5)
            session.append_event(
                "user_input_resolved",
                {
                    "input_id": input_id,
                    "kind": "mcp_elicitation",
                    "action": action,
                    "content": content,
                    "reason": reason,
                },
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "cancel mcp"},
            )
            session_run_id = start_body["session_run_id"]
            assert input_ready.wait(2)

            status, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/cancel",
                {
                    "peer_token": peer_token,
                    "session_run_id": session_run_id,
                    "reason": "user_cancelled",
                },
            )
            assert status == 200
            assert cancel_body["ok"] is True

            stream_body = _session_run_events_body(service.base_url, peer_token, session_run_id)
            resolved_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "user_input_resolved"
            ]
            assert len(resolved_events) == 1
            payload = resolved_events[0]["payload"]
            assert payload["server_enqueued_at"] > 0
            assert {
                key: payload[key]
                for key in ("input_id", "kind", "action", "content", "reason")
            } == {
                "input_id": "mcp-elicitation-cancel",
                "kind": "mcp_elicitation",
                "action": "cancel",
                "content": {},
                "reason": "user_cancelled",
            }
            assert any(event["type"] == "session_run_cancelled" for event in stream_body["events"])
            assert stream_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_session_run_done_resolves_registered_pending_approval(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_run_events_handler(_peer_id: str, _prompt: str, session) -> None:
            approval_id = "approval-done"
            payload = {
                "approval_id": approval_id,
                "tool_call_id": "call-done",
                "tool_name": "shell",
                "tool_source": "builtin",
                "reason": "need approval",
                "tool_args": {"command": "echo hi"},
            }
            session.register_approval(approval_id, payload)
            session.append_event("approval_request", payload)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_run_events_handler=session_run_events_handler,
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

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/session-runs/start",
                {"peer_token": peer_token, "prompt": "finish with pending approval"},
            )
            stream_body = _session_run_events_body(
                service.base_url,
                peer_token,
                start_body["session_run_id"],
            )

            resolved_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "approval_resolved"
            ]
            assert len(resolved_events) == 1
            payload = resolved_events[0]["payload"]
            assert payload["server_enqueued_at"] > 0
            assert {
                key: payload[key]
                for key in ("approval_id", "tool_call_id", "decision", "reason")
            } == {
                "approval_id": "approval-done",
                "tool_call_id": "call-done",
                "decision": "deny_once",
                "reason": "session_run_closed",
            }
            assert stream_body["done"] is True
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
        assert SessionForkRequest.from_dict(
            {
                "peer_token": "peer-token",
                "source_session_id": "session-1",
                "keep_through_message_index": 3,
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "source_session_id": "session-1",
            "keep_through_message_index": 3,
        }

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

            status, fork_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/fork",
                {
                    "peer_token": peer_token,
                    "source_session_id": "session-ok",
                    "keep_through_message_index": 1,
                },
            )
            assert status == 200
            assert fork_body["ok"] is True
            assert fork_body["action"] == "fork"
            assert calls[-1][0] == "fork"
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

        def stream_handler(peer_id: str, prompt: str, session) -> None:
            del peer_id, prompt, session

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
            session_run_events_handler=stream_handler,
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
                f"{service.base_url}/remote/agent-runs/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "executors": ["fake"],
                },
            )
            claim = claim_body["claim"]
            assert claim is not None
            assert claim["agent_run"]["id"] == "task-http-runtime"

            _, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-runs/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "worker_id": "worker-1",
                },
            )
            assert heartbeat_body["ok"] is True
            assert heartbeat_body["cancel_requested"] is False

            _, session_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-runs/session",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
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
                f"{service.base_url}/remote/agent-runs/event",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "worker_id": "worker-1",
                    "type": "text",
                    "text": "hello",
                },
            )
            assert event_body["ok"] is True

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
                    f"{service.base_url}/remote/agent-runs/event",
                    {
                        "peer_token": peer_token,
                        "request_id": claim["request_id"],
                        "agent_run_id": "task-http-runtime",
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
                f"{service.base_url}/remote/agent-runs/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "worker_id": "worker-1",
                },
            )
            assert cancelled_heartbeat["ok"] is True
            assert cancelled_heartbeat["cancel_requested"] is True
            assert cancelled_heartbeat["reason"] == "user_stop"

            _, complete_body = _json_request(
                "POST",
                f"{service.base_url}/remote/agent-runs/complete",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "agent_run_id": "task-http-runtime",
                    "worker_id": "worker-1",
                    "status": "cancelled",
                    "output": "",
                    "error": "execution cancelled",
                    "events": [
                        {
                            "request_id": claim["request_id"],
                            "agent_run_id": "task-http-runtime",
                            "worker_id": "worker-1",
                            "type": "status",
                            "data": {"status": "cancelled"},
                        }
                    ],
                },
            )
            assert complete_body["ok"] is True

            _, retry_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/agent-runs/retry",
                {
                    "agent_run_id": "task-http-runtime",
                    "new_agent_run_id": "task-http-runtime-retry",
                },
                headers=admin_headers,
            )
            assert retry_body["ok"] is True
            assert retry_body["agent_run"]["id"] == "task-http-runtime-retry"
            assert retry_body["agent_run"]["status"] == "queued"
            assert retry_body["agent_run"]["metadata"]["retry_of"] == "task-http-runtime"
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
            assert task.metadata["issue_id"] == issue_id

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
                    issue_id="issue-1",
                    agent_id="reviewer",
                    prompt="run remote",
                ),
                task_id="task-remote-server",
            )

            with pytest.raises(HTTPError) as invalid_worker_kind:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/agent-runs/claim",
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
                    f"{service.base_url}/remote/agent-runs/claim",
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
            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/capability-packages/drafts/accept",
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
            state = body["capability_package"]["state"]
            assert state["install_state"] in {"materialized", "installed"}
            assert state["activation_state"] == "inactive"
            settings_state = body["settings"]["capability_packages"]["review"]["state"]
            assert settings_state["install_state"] in {"materialized", "installed"}
            assert settings_state["activation_state"] == "inactive"
        finally:
            service.stop()
            relay.stop()

    def test_admin_capability_package_update_candidate_routes(
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
                        "manifest": {"components": [{"id": "skill:waza/read"}]},
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
                "candidate_snapshot": {
                    "snapshot_id": "snap-new",
                    "source_ref": "main",
                    "commit_sha": "2222222",
                },
                "candidate_manifest": {
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
            assert check_body["update_candidate"]["upstream_version"] == "main@2222222"
            assert check_body["update_candidate"]["manifest_diff"]["added_components"] == [
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
                {"package_id": "waza", "activation_approved": False},
                headers=TEST_ADMIN_HEADERS,
            )
            assert apply_status == 200
            applied = apply_body["settings"]["capability_packages"]["waza"]
            assert applied["enabled"] is False
            assert applied["state"]["activation_state"] == "inactive"
            assert applied["state"]["update_state"] == "rollback_available"
            assert applied["rollback"]["snapshot_id"] == "snap-old"
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
                f"{service.base_url}/remote/agent-runs/claim",
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
            assert task.output == "hello from fake runtime"
            assert task.workdir is not None
            assert task.branch_name is not None
            workdir = Path(task.workdir)
            assert (workdir / "tracked.txt").exists()
            assert (workdir / "agent-output.txt").read_text() == "created by fake executor\n"
            assert (workdir / "AGENTS.md").read_text() == "Use project conventions.\n"
            artifacts = control.artifacts_to_dict("task-go-runtime-worktree")
            artifact_types = {artifact["type"]: artifact for artifact in artifacts}
            assert artifact_types["branch"]["status"] == "pushed"
            assert "pull_request" not in artifact_types
            assert task.pr_url is None
            pushed = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", task.branch_name],
                cwd=workdir,
                check=True,
                timeout=30,
                capture_output=True,
                text=True,
            )
            assert task.branch_name in pushed.stdout
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

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="Go SDK is not installed")
    def test_go_agent_end_to_end_with_http_host(self, tmp_path: Path) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        agent_binary = _build_go_agent_binary()
        work_dir = tmp_path / "peer-work"
        work_dir.mkdir()
        target_file = work_dir / "demo.txt"
        target_file.write_text("hello world\n")
        proc = subprocess.Popen(
            [
                str(agent_binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(work_dir),
                "--workspace-root",
                str(work_dir),
                "--poll-interval",
                "100ms",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            peer_id = None
            while time.time() < deadline:
                online = relay.registry.list_online()
                if online:
                    peer_id = online[0].peer_id
                    break
                time.sleep(0.1)
            assert peer_id is not None

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id

            shell_result = ShellTool(backend=backend).execute(
                command="printf 'hi-from-agent'",
                intent="输出 hi-from-agent 验证远端 shell 连通性。",
            )
            assert "hi-from-agent" in shell_result

            read_result = ReadFileTool(backend=backend).execute(
                file_path=str(target_file)
            )
            assert "1\thello world" in read_result

            write_result = ApplyPatchTool(backend=backend).execute(
                patch=(
                    "*** Begin Patch\n"
                    "*** Update File: demo.txt\n"
                    "@@\n"
                    "-hello world\n"
                    "+alpha\n"
                    "+beta\n"
                    "*** End Patch"
                ),
            )
            assert "Applied patch" in write_result
            assert target_file.read_text() == "alpha\nbeta\n"

            edit_result = ApplyPatchTool(backend=backend).execute(
                patch=(
                    "*** Begin Patch\n"
                    "*** Update File: demo.txt\n"
                    "@@\n"
                    " alpha\n"
                    "-beta\n"
                    "+gamma\n"
                    "*** End Patch"
                ),
            )
            assert "--- a/" in edit_result
            assert "+++ b/" in edit_result
            assert "-beta" in edit_result
            assert "+gamma" in edit_result
            assert target_file.read_text() == "alpha\ngamma\n"

            glob_result = GlobTool(backend=backend).execute(
                pattern="*.txt", path=str(work_dir)
            )
            assert str(target_file) in glob_result

            grep_result = GrepTool(backend=backend).execute(
                pattern="gamma", path=str(work_dir)
            )
            assert str(target_file) in grep_result
            assert "gamma" in grep_result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            service.stop()
            relay.stop()
