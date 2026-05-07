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

class RemoteRelayBaseHandler:
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _accepts_gzip(self) -> bool:
        accepted = self.headers.get("Accept-Encoding", "")
        return any(
            part.strip().split(";", 1)[0].lower() == "gzip"
            for part in accepted.split(",")
        )

    def _send_response_body(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
        *,
        compressible: bool = False,
    ) -> None:
        response_headers = dict(headers or {})
        data = body
        if (
            compressible
            and len(body) >= GZIP_MIN_BYTES
            and self._accepts_gzip()
        ):
            data = gzip.compress(body)
            response_headers["Content-Encoding"] = "gzip"
            response_headers["Vary"] = "Accept-Encoding"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _etag_matches(self, etag: str) -> bool:
        header = self.headers.get("If-None-Match")
        if not header:
            return False
        return any(part.strip() in {etag, "*"} for part in header.split(","))

    def _send_not_modified(
        self, etag: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(HTTPStatus.NOT_MODIFIED)
        self.send_header("ETag", etag)
        for key, value in dict(headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_response_body(
            status,
            body,
            "application/json",
            headers,
            compressible=True,
        )

    def _send_text(
        self,
        status: int,
        body: str,
        content_type: str = "text/plain; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8")
        self._send_response_body(
            status,
            data,
            content_type,
            headers,
            compressible=True,
        )

    def _send_bytes(
        self,
        status: int,
        content: bytes,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._send_response_body(status, content, content_type, headers)

    def _verify_peer_token(self, peer_token: Any) -> str | None:
        if not isinstance(peer_token, str) or not peer_token:
            return None
        return self.service.relay_server.token_manager.verify_peer_token(peer_token)

    def _query_value(self, parsed: Any, key: str, default: str = "") -> str:
        values = parse_qs(parsed.query).get(key, [])
        if not values:
            return default
        return values[0]

    def _verify_query_peer(self, parsed: Any) -> str | None:
        return self._verify_peer_token(self._query_value(parsed, "peer_token"))

    def _bearer_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return header[len("Bearer ") :].strip()

    def _require_auth(self, roles: set[str] | None = None):
        principal = self.service.auth_service.authenticate_access_token(
            self._bearer_token()
        )
        if principal is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        if roles and principal.role not in roles:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return None
        return principal

    def _require_auth_scopes(self, scopes: set[str]):
        principal = self.service.auth_service.authenticate_access_token(
            self._bearer_token()
        )
        if principal is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        if any(not principal.has_scope(scope) for scope in scopes):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return None
        return principal


