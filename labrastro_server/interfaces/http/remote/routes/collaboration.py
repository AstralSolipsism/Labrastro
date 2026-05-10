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

class RemoteCollaborationRoutes:
    def _handle_issue_assignment_get(self, parsed: Any) -> None:
        peer_id = self._verify_query_peer(parsed)
        if peer_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"}
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        parts = [
            unquote(part)
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        try:
            if len(parts) == 3 and parts[:2] == ["remote", "issues"]:
                detail = self.service.issue_assignment_service.load_issue_detail(
                    parts[2], peer_id=peer_id
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **detail})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "issues"]
                and parts[3] == "assignments"
            ):
                assignments = [
                    assignment.to_dict()
                    for assignment in self.service.issue_assignment_service.list_assignments(
                        parts[2], peer_id=peer_id
                    )
                ]
                self._send_json(
                    HTTPStatus.OK, {"ok": True, "assignments": assignments}
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "issues"]
                and parts[3] == "events"
            ):
                events = self.service.issue_assignment_service.list_events(
                    "issue",
                    parts[2],
                    after_seq=int(
                        self._query_value(parsed, "after_seq", "0") or 0
                    ),
                    timeout_sec=float(
                        self._query_value(parsed, "timeout_sec", "0") or 0
                    ),
                    peer_id=peer_id,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "events": events})
                return
            if len(parts) == 3 and parts[:2] == ["remote", "assignments"]:
                detail = (
                    self.service.issue_assignment_service.load_assignment_detail(
                        parts[2], peer_id=peer_id
                    )
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **detail})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "assignments"]
                and parts[3] == "events"
            ):
                events = self.service.issue_assignment_service.list_events(
                    "assignment",
                    parts[2],
                    after_seq=int(
                        self._query_value(parsed, "after_seq", "0") or 0
                    ),
                    timeout_sec=float(
                        self._query_value(parsed, "timeout_sec", "0") or 0
                    ),
                    peer_id=peer_id,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "events": events})
                return
            if len(parts) == 3 and parts[:2] == ["remote", "mentions"]:
                mention = self.service.issue_assignment_service.get_mention(
                    parts[2], peer_id=peer_id
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "mention": mention.to_dict()},
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "mentions"]
                and parts[3] == "events"
            ):
                events = self.service.issue_assignment_service.list_events(
                    "mention",
                    parts[2],
                    after_seq=int(
                        self._query_value(parsed, "after_seq", "0") or 0
                    ),
                    timeout_sec=float(
                        self._query_value(parsed, "timeout_sec", "0") or 0
                    ),
                    peer_id=peer_id,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "events": events})
                return
        except KeyError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "assignment_not_found", "message": str(exc)},
            )
            return
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "assignment_forbidden", "message": str(exc)},
            )
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "assignment_request_failed", "message": str(exc)},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_issue_assignment_post(self, path: str) -> None:
        payload = self._read_json()
        peer_id = self._verify_peer_token(payload.get("peer_token"))
        if peer_id is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"}
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        parts = [
            unquote(part)
            for part in path.strip("/").split("/")
            if part
        ]
        try:
            if parts == ["remote", "issues"]:
                issue = self.service.issue_assignment_service.create_issue(
                    title=str(payload.get("title") or ""),
                    description=str(payload.get("description") or ""),
                    peer_id=peer_id,
                    source=str(payload.get("source") or "manual"),
                    taskflow_id=optional_payload_str(
                        payload, "taskflow_id"
                    ),
                    work_item_id=optional_payload_str(
                        payload, "work_item_id"
                    ),
                    metadata=(
                        dict(payload.get("metadata"))
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                    issue_id=optional_payload_str(payload, "issue_id"),
                )
                self._send_json(
                    HTTPStatus.OK, {"ok": True, "issue": issue.to_dict()}
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "issues"]
                and parts[3] == "assignments"
            ):
                legacy_dispatch_fields = [
                    key
                    for key in ("required_dispatch_tags", "preferred_dispatch_tags")
                    if key in payload
                ]
                if legacy_dispatch_fields:
                    self._send_error(
                        HTTPStatus.BAD_REQUEST,
                        "legacy_dispatch_fields_removed",
                        "select an Agent explicitly with target_agent_id",
                    )
                    return
                assignment = (
                    self.service.issue_assignment_service.create_assignment(
                        parts[2],
                        peer_id=peer_id,
                        target_agent_id=optional_payload_str(
                            payload, "target_agent_id"
                        )
                        or optional_payload_str(payload, "agent_id"),
                        title=optional_payload_str(payload, "title"),
                        prompt=optional_payload_str(payload, "prompt"),
                        task_type=optional_payload_str(payload, "task_type"),
                        workspace_root=optional_payload_str(
                            payload, "workspace_root"
                        ),
                        repo_url=optional_payload_str(payload, "repo_url"),
                        execution_location=optional_payload_str(
                            payload, "execution_location"
                        ),
                        reason=str(payload.get("reason") or ""),
                        source=str(payload.get("source") or "manual"),
                        metadata=(
                            dict(payload.get("metadata"))
                            if isinstance(payload.get("metadata"), dict)
                            else {}
                        ),
                        assignment_id=optional_payload_str(
                            payload, "assignment_id"
                        ),
                    )
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "assignment": assignment.to_dict()},
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "assignments"]
                and parts[3] == "dispatch"
            ):
                assignment = (
                    self.service.issue_assignment_service.dispatch_assignment(
                        parts[2], peer_id=peer_id
                    )
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "assignment": assignment.to_dict()},
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "assignments"]
                and parts[3] == "cancel"
            ):
                assignment = self.service.issue_assignment_service.cancel_assignment(
                    parts[2],
                    peer_id=peer_id,
                    reason=str(payload.get("reason") or "user_cancelled"),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "assignment": assignment.to_dict()},
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["remote", "assignments"]
                and parts[3] == "assign"
            ):
                agent_id = optional_payload_str(payload, "agent_id")
                if not agent_id:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "agent_id_required"},
                    )
                    return
                assignment = self.service.issue_assignment_service.reassign_assignment(
                    parts[2],
                    agent_id=agent_id,
                    peer_id=peer_id,
                    reason=str(payload.get("reason") or "manual_reassign"),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "assignment": assignment.to_dict()},
                )
                return
            if parts == ["remote", "mentions", "parse"]:
                mention = self.service.issue_assignment_service.parse_mention(
                    raw_text=str(payload.get("raw_text") or ""),
                    agent_ref=optional_payload_str(payload, "agent_ref"),
                    peer_id=peer_id,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "mention": mention.to_dict()},
                )
                return
            if parts == ["remote", "mentions"]:
                mention = self.service.issue_assignment_service.create_mention(
                    raw_text=str(payload.get("raw_text") or ""),
                    peer_id=peer_id,
                    agent_ref=optional_payload_str(payload, "agent_ref"),
                    issue_id=optional_payload_str(payload, "issue_id"),
                    title=optional_payload_str(payload, "title"),
                    prompt=optional_payload_str(payload, "prompt"),
                    context_type=str(payload.get("context_type") or "chat"),
                    context_id=optional_payload_str(payload, "context_id"),
                    source=str(payload.get("source") or "manual"),
                    metadata=(
                        dict(payload.get("metadata"))
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "mention": mention.to_dict()},
                )
                return
        except KeyError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "assignment_not_found", "message": str(exc)},
            )
            return
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "assignment_forbidden", "message": str(exc)},
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "assignment_invalid_state", "message": str(exc)},
            )
            return
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "assignment_unavailable", "message": str(exc)},
            )
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "assignment_request_failed", "message": str(exc)},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})


