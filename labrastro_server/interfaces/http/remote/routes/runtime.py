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
from labrastro_server.services.agent_runtime.runtime_store import clamp_event_limit
from reuleauxcoder.interfaces.events import UIEventKind

class RemoteRuntimeRoutes:
    def _handle_runtime_events_get(self, parsed: Any) -> None:
        peer_id = self._verify_query_peer(parsed)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runtime_unavailable")
            return
        parts = [
            unquote(part)
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        if (
            len(parts) != 5
            or parts[:3] != ["remote", "agent-runtime", "tasks"]
            or parts[4] != "events"
        ):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        task_id = parts[3]
        after_seq = int(self._query_value(parsed, "after_seq", "0") or 0)
        timeout_sec = float(
            self._query_value(parsed, "timeout_sec", "0") or 0
        )
        limit = clamp_event_limit(
            int(self._query_value(parsed, "limit", "200") or 200)
        )
        try:
            events = self.service.runtime_control_plane.wait_events(
                task_id,
                after_seq=after_seq,
                timeout_sec=timeout_sec,
                limit=limit,
            )
        except AttributeError:
            events = self.service.runtime_control_plane.list_events(
                task_id, after_seq=after_seq, limit=limit
            )
        next_seq = max([after_seq, *[int(event.seq) for event in events]])
        has_more = bool(
            self.service.runtime_control_plane.list_events(
                task_id,
                after_seq=next_seq,
                limit=1,
            )
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "events": [event.to_dict() for event in events],
                "next_seq": next_seq,
                "has_more": has_more,
            },
        )

    def _handle_runtime_claim(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runtime_unavailable")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        raw_executors = payload.get("executors", [])
        executors = raw_executors if isinstance(raw_executors, list) else []
        worker_id = str(payload.get("worker_id") or peer_id)
        peer = self.service.relay_server.registry.get(peer_id)
        peer_features = list(peer.features) if peer is not None else []
        workspace_root = peer.workspace_root if peer is not None else None
        claim = self.service.runtime_control_plane.claim_task(
            worker_id=worker_id,
            executors=[str(executor) for executor in executors],
            peer_id=peer_id,
            peer_features=peer_features,
            workspace_root=workspace_root,
            wait_sec=float(payload.get("wait_sec") or 0),
        )
        self._send_json(
            HTTPStatus.OK,
            {"claim": claim.to_dict() if claim is not None else None},
        )

    def _handle_runtime_heartbeat(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runtime_unavailable")
            return
        request_id = str(payload.get("request_id") or "")
        task_id = str(payload.get("task_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not request_id or not task_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_task_id_and_worker_id_required",
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        response = self.service.runtime_control_plane.heartbeat_task(
            request_id=request_id,
            task_id=task_id,
            worker_id=worker_id,
            peer_id=peer_id,
            lease_sec=(
                int(payload["lease_sec"])
                if payload.get("lease_sec") is not None
                else None
            ),
        )
        self._send_json(HTTPStatus.OK, response)

    def _handle_runtime_session(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runtime_unavailable")
            return
        request_id = str(payload.get("request_id") or "")
        task_id = str(payload.get("task_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not request_id or not task_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_task_id_and_worker_id_required",
            )
            return
        metadata: dict[str, Any] = {}
        for key in ("repo_url", "cache_path"):
            if payload.get(key) is not None:
                metadata[key] = str(payload[key])
        try:
            ok, reason = self.service.runtime_control_plane.pin_claimed_session(
                request_id=request_id,
                task_id=task_id,
                worker_id=worker_id,
                peer_id=peer_id,
                workdir=(
                    str(payload["workdir"])
                    if payload.get("workdir") is not None
                    else None
                ),
                branch=(
                    str(payload["branch"])
                    if payload.get("branch") is not None
                    else None
                ),
                executor_session_id=(
                    str(payload["executor_session_id"])
                    if payload.get("executor_session_id") is not None
                    else None
                ),
                metadata=metadata,
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "task_not_found")
            return
        if not ok:
            status = (
                HTTPStatus.NOT_FOUND
                if reason == "task_not_found"
                else HTTPStatus.FORBIDDEN
            )
            self._send_error(status, reason or "claim_owner_mismatch")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_runtime_event(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runtime_unavailable")
            return
        task_id = str(payload.get("task_id") or "")
        request_id = str(payload.get("request_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        event_type = str(payload.get("type") or "")
        if not task_id or not event_type or not request_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_task_id_worker_id_and_type_required",
            )
            return
        data = payload.get("data", {})
        ok, reason = self.service.runtime_control_plane.append_executor_event(
            task_id,
            ExecutorEvent(
                type=event_type,
                text=(
                    str(payload["text"])
                    if payload.get("text") is not None
                    else None
                ),
                data=dict(data) if isinstance(data, dict) else {},
            ),
            request_id=request_id,
            worker_id=worker_id,
            peer_id=peer_id,
        )
        if not ok:
            status = (
                HTTPStatus.NOT_FOUND
                if reason == "task_not_found"
                else HTTPStatus.FORBIDDEN
            )
            self._send_error(status, reason or "claim_owner_mismatch")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_runtime_complete(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runtime_unavailable")
            return
        task_id = str(payload.get("task_id") or "")
        request_id = str(payload.get("request_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not task_id or not request_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_task_id_and_worker_id_required",
            )
            return
        raw_events = payload.get("events", [])
        events = [
            ExecutorEvent(
                type=str(event.get("type", "status")),
                text=(
                    str(event["text"])
                    if event.get("text") is not None
                    else None
                ),
                data=(
                    dict(event.get("data", {}))
                    if isinstance(event.get("data"), dict)
                    else {}
                ),
            )
            for event in raw_events
            if isinstance(event, dict)
        ]
        usage = payload.get("usage", {})
        artifacts = payload.get("artifacts", [])
        result = ExecutorRunResult(
            task_id=task_id,
            status=str(payload.get("status") or "failed"),
            output=str(payload.get("output") or ""),
            executor_session_id=(
                str(payload["executor_session_id"])
                if payload.get("executor_session_id") is not None
                else None
            ),
            events=events,
            usage=dict(usage) if isinstance(usage, dict) else {},
            error=(
                str(payload["error"])
                if payload.get("error") is not None
                else None
            ),
        )
        try:
            ok, reason, completed = self.service.runtime_control_plane.complete_claimed_task(
                task_id,
                result,
                request_id=request_id,
                worker_id=worker_id,
                peer_id=peer_id,
                artifacts=[
                    artifact
                    for artifact in artifacts
                    if isinstance(artifact, dict)
                ],
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "task_not_found")
            return
        if not ok:
            status = (
                HTTPStatus.NOT_FOUND
                if reason == "task_not_found"
                else HTTPStatus.FORBIDDEN
            )
            self._send_error(status, reason or "claim_owner_mismatch")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        github: dict[str, Any] | None = None
        github_pr_service = getattr(self.service, "github_pr_service", None)
        if (
            completed is not None
            and result.status == "completed"
            and github_pr_service is not None
        ):
            github = github_pr_service.ensure_pr_for_task(task_id).to_dict()
        response: dict[str, Any] = {"ok": True}
        if github is not None:
            response["github"] = github
        self._send_json(HTTPStatus.OK, response)


