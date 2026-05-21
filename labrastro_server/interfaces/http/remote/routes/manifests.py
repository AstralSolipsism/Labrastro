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

class RemoteManifestRoutes:
    def _handle_mcp_manifest(self) -> None:
        payload = self._read_json()
        try:
            req = MCPManifestRequest.from_dict(payload)
        except Exception:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_mcp_manifest_request"}
            )
            return
        peer_id = self._verify_peer_token(req.peer_token)
        if peer_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"}
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        response = self.service._build_mcp_manifest(req.os, req.arch)
        self._send_json(HTTPStatus.OK, response.to_dict())

    def _handle_mcp_tools(self) -> None:
        payload = self._read_json()
        try:
            report = PeerMCPToolsReport.from_dict(payload)
        except Exception:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_mcp_tools_report"}
            )
            return
        peer_id = self._verify_peer_token(report.peer_token)
        if peer_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"}
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        ok = self.service.relay_server.update_peer_mcp_tools(
            peer_id, report.tools, report.diagnostics
        )
        if self.service.ui_bus is not None:
            self.service.ui_bus.info(
                f"Remote peer MCP tools reported: {len(report.tools)}",
                kind=UIEventKind.REMOTE,
                peer_id=peer_id,
            )
        self._send_json(HTTPStatus.OK, {"ok": ok, "peer_id": peer_id})

    def _handle_environment_manifest(self) -> None:
        payload = self._read_json()
        try:
            req = EnvironmentManifestRequest.from_dict(payload)
        except Exception:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_environment_manifest_request"},
            )
            return
        peer_id = self._verify_peer_token(req.peer_token)
        if peer_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"}
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        response = self.service._build_environment_manifest(
            req.os, req.arch, req.workspace
        )
        self._send_json(HTTPStatus.OK, response.to_dict())


