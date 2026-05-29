from __future__ import annotations

import gzip
import json
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote

from labrastro_server.interfaces.http.remote.helpers import (
    GZIP_MIN_BYTES,
    optional_payload_str,
    package_version,
    strong_etag,
)
from labrastro_server.interfaces.http.remote.protocol import (
    ApprovalReplyRequest,
    ApprovalReplyResponse,
    SessionRunCancelRequest,
    SessionRunCancelResponse,
    SessionRunStartRequest,
    SessionRunStartResponse,
    CleanupResult,
    DisconnectNotice,
    EnvironmentManifestRequest,
    EnvironmentManifestResponse,
    ExecToolResult,
    Heartbeat,
    MCPManifestRequest,
    MCPManifestResponse,
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
from labrastro_server.relay.errors import RegisterRejectedError
from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from reuleauxcoder.interfaces.events import UIEventKind

class RemotePeerRoutes:
    def _handle_features(self) -> None:
        agent_run_features = self._agent_run_features()
        session_history_status = self._session_history_status()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "api_version": 1,
                "server_version": package_version(),
                "features": {
                    "sessions": self.service.session_handler is not None,
                    "session_auto_save": session_history_status["session_auto_save"],
                    "session_history_writable": session_history_status[
                        "session_history_writable"
                    ],
                    "session_runs": self.service.session_run_events_handler is not None,
                    "taskflow": self.service.taskflow_service is not None,
                    "issue_assignment": self.service.issue_assignment_service
                    is not None,
                    "fresh_session_without_session_hint": self.service.session_run_events_handler
                    is not None,
                    "peer_token_heartbeat_refresh": True,
            "agent_runs": agent_run_features,
                },
            },
        )

    def _session_history_status(self) -> dict[str, bool]:
        provider = getattr(self.service, "session_history_status_provider", None)
        if callable(provider):
            try:
                status = provider()
                return {
                    "session_auto_save": bool(status.get("session_auto_save", True)),
                    "session_history_writable": bool(
                        status.get("session_history_writable", False)
                    ),
                }
            except Exception:
                pass
        sessions_available = self.service.session_handler is not None
        return {
            "session_auto_save": True,
            "session_history_writable": sessions_available,
        }

    def _agent_run_features(self) -> dict[str, Any]:
        executor_features: dict[str, dict[str, Any]] = {}
        for peer in self.service.relay_server.registry.list_online():
            host_info = peer.meta.get("host_info_min")
            if not isinstance(host_info, dict):
                continue
            agent_runs = host_info.get("agent_runs")
            if not isinstance(agent_runs, dict):
                continue
            raw = agent_runs.get("executor_features")
            if not isinstance(raw, dict):
                continue
            for name, value in raw.items():
                if not isinstance(value, dict):
                    continue
                key = str(name)
                next_value = dict(value)
                current = executor_features.get(key)
                if current is None or (
                    current.get("installed") is not True
                    and next_value.get("installed") is True
                ):
                    executor_features[key] = next_value
                    continue
                current_limits = {
                    str(item)
                    for item in current.get("limitations", [])
                    if str(item).strip()
                }
                next_limits = {
                    str(item)
                    for item in next_value.get("limitations", [])
                    if str(item).strip()
                }
                current["limitations"] = sorted(current_limits | next_limits)
        return {"executor_features": executor_features}

    def _handle_register(self) -> None:
        payload = self._read_json()
        try:
            req = RegisterRequest.from_dict(payload)
            missing = _missing_peer_runtime_context_fields(req)
            if getattr(self.service, "require_peer_runtime_context", False) and missing:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_peer_runtime_context",
                    "remote peer registration is missing required runtime context",
                    {"missing": missing},
                )
                return
            resp = self.service.relay_server._on_register(req)
        except RegisterRejectedError as exc:
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "register_rejected",
                "remote peer registration rejected",
                {"reason": RegisterRejected(reason=exc.message).reason},
            )
            return
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_register_request")
            return
        self.service.ui_bus and self.service.ui_bus.success(
            f"Remote peer registered: {resp.peer_id}",
            kind=UIEventKind.REMOTE,
            peer_id=resp.peer_id,
        )
        self._send_json(
            HTTPStatus.OK, {"type": "register_ok", "payload": resp.to_dict()}
        )

    def _handle_heartbeat(self) -> None:
        payload = self._read_json()
        try:
            hb = Heartbeat.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_heartbeat_request")
            return
        peer_id = self.service.relay_server.token_manager.refresh_peer_token(
            hb.peer_token, ttl_sec=self.service.relay_server.peer_token_ttl_sec
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(HTTPStatus.OK, {"ok": True, "peer_id": peer_id})

    def _handle_poll(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        if not isinstance(peer_token, str) or not peer_token:
            self._send_error(HTTPStatus.BAD_REQUEST, "peer_token_required")
            return
        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        env = self.service._next_envelope(peer_id)
        if env is None:
            self._send_json(HTTPStatus.OK, {"type": "noop", "payload": {}})
            return
        self._send_json(HTTPStatus.OK, env.to_dict())

    def _handle_result(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        request_id = payload.get("request_id")
        result_type = payload.get("type", "tool_result")
        result_payload = payload.get("payload", {})
        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if not isinstance(request_id, str) or not request_id:
            self._send_error(HTTPStatus.BAD_REQUEST, "request_id_required")
            return
        try:
            if result_type == "cleanup_result":
                result = CleanupResult.from_dict(result_payload)
                env = RelayEnvelope(
                    type="cleanup_result",
                    request_id=request_id,
                    peer_id=peer_id,
                    payload=result.to_dict(),
                )
            elif result_type == "tool_stream":
                env = RelayEnvelope(
                    type="tool_stream",
                    request_id=request_id,
                    peer_id=peer_id,
                    payload=result_payload,
                )
            elif result_type == "tool_preview_result":
                result = ToolPreviewResult.from_dict(result_payload)
                env = RelayEnvelope(
                    type="tool_preview_result",
                    request_id=request_id,
                    peer_id=peer_id,
                    payload=result.to_dict(),
                )
            else:
                result = ExecToolResult.from_dict(result_payload)
                env = RelayEnvelope(
                    type="tool_result",
                    request_id=request_id,
                    peer_id=peer_id,
                    payload=result.to_dict(),
                )
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_result_request")
            return
        self.service.relay_server.handle_inbound(peer_id, env)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_disconnect(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        notice = DisconnectNotice(
            reason=payload.get("reason", "peer_initiated")
        )
        self.service._abort_peer_session_runs(
            peer_id, f"peer_disconnected: {notice.reason}"
        )
        self.service.relay_server.disconnect_peer(peer_id, notice.reason)
        self.service.ui_bus and self.service.ui_bus.warning(
            f"Remote peer disconnected: {peer_id}",
            kind=UIEventKind.REMOTE,
            peer_id=peer_id,
            reason=notice.reason,
        )
        self._send_json(HTTPStatus.OK, {"ok": True})


def _missing_peer_runtime_context_fields(req: RegisterRequest) -> list[str]:
    missing: list[str] = []
    host_info = req.host_info_min if isinstance(req.host_info_min, dict) else {}
    for key in ("os", "shell"):
        value = host_info.get(key)
        if not isinstance(value, str) or not value.strip():
            missing.append(f"host_info_min.{key}")
    if not isinstance(req.cwd, str) or not req.cwd.strip():
        missing.append("cwd")
    if not isinstance(req.workspace_root, str) or not req.workspace_root.strip():
        missing.append("workspace_root")
    if not isinstance(req.features, list):
        missing.append("features")
    return missing
