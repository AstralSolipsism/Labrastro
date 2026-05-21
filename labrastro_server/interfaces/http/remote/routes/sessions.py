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
    SessionForkRequest,
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

class RemoteSessionRoutes:
    def _handle_sessions(self, path: str) -> None:
        payload = self._read_json()
        session_payload = payload
        action = path.rsplit("/", 1)[-1]
        try:
            if action == "list":
                req = SessionListRequest.from_dict(payload)
                peer_token = req.peer_token
                session_payload = req.to_dict()
            elif action == "load":
                req = SessionLoadRequest.from_dict(payload)
                peer_token = req.peer_token
                session_payload = req.to_dict()
            elif action == "new":
                req = SessionNewRequest.from_dict(payload)
                peer_token = req.peer_token
                session_payload = req.to_dict()
            elif action == "delete":
                req = SessionDeleteRequest.from_dict(payload)
                peer_token = req.peer_token
                session_payload = req.to_dict()
            elif action == "fork":
                req = SessionForkRequest.from_dict(payload)
                peer_token = req.peer_token
                session_payload = req.to_dict()
            elif action == "model":
                req = SessionModelSwitchRequest.from_dict(payload)
                peer_token = req.peer_token
                session_payload = req.to_dict()
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found")
                return
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.session_handler is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "sessions_unavailable")
            return

        try:
            result = dict(self.service.session_handler(action, peer_id, session_payload))
            status = int(result.pop("_status", HTTPStatus.OK))
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "session_request_failed",
            )
            return
        self._send_json(status, result)


