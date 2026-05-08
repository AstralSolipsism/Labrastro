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
    SessionSnapshotRequest,
    ToolPreviewResult,
)
from labrastro_server.relay.errors import RegisterRejectedError
from labrastro_server.services.agent_runtime.control_plane import RuntimeTaskRequest
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from reuleauxcoder.interfaces.events import UIEventKind

class RemotePeerRoutes:
    def _handle_capabilities(self) -> None:
        runtime_capabilities = self._agent_runtime_capabilities()
        session_history_status = self._session_history_status()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "api_version": 1,
                "server_version": package_version(),
                "capabilities": {
                    "sessions": self.service.session_handler is not None,
                    "session_auto_save": session_history_status["session_auto_save"],
                    "session_history_writable": session_history_status[
                        "session_history_writable"
                    ],
                    "chat_stream": self.service.stream_chat_handler is not None,
                    "taskflow": self.service.taskflow_service is not None,
                    "issue_assignment": self.service.issue_assignment_service
                    is not None,
                    "fresh_session_without_session_hint": self.service.stream_chat_handler
                    is not None,
                    "peer_token_heartbeat_refresh": True,
                    "agent_runtime": runtime_capabilities,
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

    def _agent_runtime_capabilities(self) -> dict[str, Any]:
        executor_capabilities: dict[str, dict[str, Any]] = {}
        for peer in self.service.relay_server.registry.list_online():
            host_info = peer.meta.get("host_info_min")
            if not isinstance(host_info, dict):
                continue
            agent_runtime = host_info.get("agent_runtime")
            if not isinstance(agent_runtime, dict):
                continue
            raw = agent_runtime.get("executor_capabilities")
            if not isinstance(raw, dict):
                continue
            for name, value in raw.items():
                if not isinstance(value, dict):
                    continue
                key = str(name)
                next_value = dict(value)
                current = executor_capabilities.get(key)
                if current is None or (
                    current.get("installed") is not True
                    and next_value.get("installed") is True
                ):
                    executor_capabilities[key] = next_value
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
        return {"executor_capabilities": executor_capabilities}

    def _handle_register(self) -> None:
        payload = self._read_json()
        try:
            resp = self.service.relay_server._on_register(
                RegisterRequest.from_dict(payload)
            )
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
        self.service._abort_peer_chat_sessions(
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


