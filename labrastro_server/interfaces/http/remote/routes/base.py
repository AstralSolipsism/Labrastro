from __future__ import annotations

import gzip
import json
import os
import time
import uuid
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
    DisconnectNotice,
    EnvironmentManifestRequest,
    EnvironmentManifestResponse,
    Heartbeat,
    MCPManifestRequest,
    MCPManifestResponse,
    PeerMCPToolsReport,
    RegisterRejected,
    RegisterRequest,
    SessionDeleteRequest,
    SessionListRequest,
    SessionLoadRequest,
    SessionModelSwitchRequest,
    SessionNewRequest,
)
from labrastro_server.relay.errors import RegisterRejectedError
from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from reuleauxcoder.interfaces.events import UIEventKind


class RemoteRouteError(Exception):
    def __init__(
        self,
        status: int | HTTPStatus,
        code: str,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.status = int(status)
        self.code = code
        self.message = message or _default_error_message(code)
        self.details = details or {}


def _default_error_message(code: str) -> str:
    return {
        "invalid_json": "request body must be valid JSON",
        "invalid_content_length": "request content length is invalid",
        "request_body_too_large": "request body is too large",
        "unauthorized": "authentication required",
        "forbidden": "permission denied",
        "not_found": "route not found",
        "internal_server_error": "internal server error",
    }.get(code, code.replace("_", " "))


class RemoteRelayBaseHandler:
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise RemoteRouteError(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
            ) from exc
        if content_length <= 0:
            return {}
        max_body = int(
            getattr(self.service, "max_request_body_bytes", 16 * 1024 * 1024)
            or 16 * 1024 * 1024
        )
        if content_length > max_body:
            raise RemoteRouteError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_body_too_large",
                details={"limit": max_body},
            )
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteRouteError(HTTPStatus.BAD_REQUEST, "invalid_json") from exc
        if not isinstance(decoded, dict):
            raise RemoteRouteError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "request body must be a JSON object",
            )
        return decoded

    def _request_id(self) -> str:
        request_id = getattr(self, "_remote_request_id", None)
        if isinstance(request_id, str) and request_id:
            return request_id
        header = self.headers.get("X-Request-ID", "")
        if isinstance(header, str) and header.strip():
            request_id = header.strip()[:128]
        else:
            request_id = uuid.uuid4().hex
        self._remote_request_id = request_id
        return request_id

    def _error_payload(
        self,
        code: str,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": code,
            "message": message or _default_error_message(code),
            "details": details or {},
            "request_id": self._request_id(),
        }

    def _send_error(
        self,
        status: int | HTTPStatus,
        code: str,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        self._send_json(status, self._error_payload(code, message, details))

    def _coerce_error_payload(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("ok") is False and "request_id" in payload:
            return payload
        raw_code = payload.get("error")
        code = raw_code if isinstance(raw_code, str) and raw_code else "request_failed"
        raw_message = payload.get("message")
        message = raw_message if isinstance(raw_message, str) and raw_message else ""
        details = {
            key: value
            for key, value in payload.items()
            if key not in {"ok", "error", "message", "request_id"}
        }
        return self._error_payload(code, message, details)

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
        response_headers.setdefault("X-Request-ID", self._request_id())
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
        if int(status) >= 400:
            payload = self._coerce_error_payload(payload)
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

    def _session_binding_peer_matches(self, binding: Any, peer_id: str | None) -> bool:
        if peer_id is None:
            return False
        binding_peer_id = str(getattr(binding, "peer_id", "") or "").strip()
        if not binding_peer_id or binding_peer_id == peer_id:
            return True
        return self._peer_can_resume_binding_owner(binding_peer_id, peer_id)

    def _peer_can_resume_binding_owner(
        self,
        owner_peer_id: str,
        candidate_peer_id: str,
    ) -> bool:
        registry = self.service.relay_server.registry
        current = registry.get(candidate_peer_id)
        get_any = getattr(registry, "get_any", None)
        owner = get_any(owner_peer_id) if callable(get_any) else None
        if current is None or owner is None:
            return False
        if str(getattr(owner, "status", "") or "") == "online":
            return False
        owner_root = self._peer_scope_root(owner)
        current_root = self._peer_scope_root(current)
        if not owner_root or owner_root != current_root:
            return False
        owner_features = {str(item) for item in getattr(owner, "features", []) or []}
        current_features = {str(item) for item in getattr(current, "features", []) or []}
        return owner_features.issubset(current_features)

    @staticmethod
    def _peer_scope_root(peer: Any) -> str:
        raw = (
            getattr(peer, "workspace_root", None)
            or getattr(peer, "cwd", None)
            or ""
        )
        value = str(raw or "").strip()
        if not value:
            return ""
        return os.path.normcase(os.path.normpath(value))

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
            self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return None
        if roles and principal.role not in roles:
            self._send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return None
        return principal

    def _require_auth_scopes(self, scopes: set[str]):
        principal = self.service.auth_service.authenticate_access_token(
            self._bearer_token()
        )
        if principal is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return None
        if any(not principal.has_scope(scope) for scope in scopes):
            self._send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return None
        return principal
