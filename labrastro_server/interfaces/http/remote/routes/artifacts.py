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

class RemoteArtifactRoutes:
    def _handle_artifact(self, path: str) -> None:
        if self.service.artifact_provider is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "artifact_unavailable",
                    "message": "peer artifact not uploaded yet",
                },
            )
            return
        suffix = path.removeprefix("/remote/artifacts/")
        parts = suffix.split("/")
        if len(parts) != 3:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        os_name, arch, artifact_name = parts
        try:
            artifact = self.service.artifact_provider(os_name, arch, artifact_name)
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "artifact_unavailable", "message": str(exc)},
            )
            return
        if artifact is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "artifact_unavailable"}
            )
            return
        content, content_type = artifact
        etag = strong_etag(content)
        cache_headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if self._etag_matches(etag):
            self._send_not_modified(etag, {"Cache-Control": "no-cache"})
            return
        self._send_bytes(
            HTTPStatus.OK,
            content,
            content_type,
            cache_headers,
        )

    def _handle_mcp_artifact(self, path: str) -> None:
        peer_token = self.headers.get("X-RC-Peer-Token", "")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"}
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        suffix = unquote(path.removeprefix("/remote/mcp/artifacts/"))
        artifact_path = self.service._resolve_mcp_artifact_path(suffix)
        if artifact_path is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            content = artifact_path.read_bytes()
        except OSError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "artifact_unavailable", "message": str(exc)},
            )
            return
        etag = strong_etag(content)
        cache_headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if self._etag_matches(etag):
            self._send_not_modified(etag, {"Cache-Control": "no-cache"})
            return
        self._send_bytes(
            HTTPStatus.OK,
            content,
            "application/octet-stream",
            cache_headers,
        )

