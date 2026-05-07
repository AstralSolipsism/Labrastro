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

class RemoteTaskflowRoutes:
    def _handle_taskflow_get(self, parsed: Any) -> None:
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
            if len(parts) == 4 and parts[:3] == ["remote", "taskflow", "goals"]:
                detail = self.service.taskflow_service.load_goal_detail(
                    parts[3], peer_id=peer_id
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **detail})
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "goals"]
                and parts[4] == "events"
            ):
                events = self.service.taskflow_service.list_events(
                    parts[3],
                    after_seq=int(self._query_value(parsed, "after_seq", "0") or 0),
                    timeout_sec=float(
                        self._query_value(parsed, "timeout_sec", "0") or 0
                    ),
                    peer_id=peer_id,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "events": events})
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "task-drafts"]
                and parts[4] == "dispatch-decisions"
            ):
                decisions = [
                    decision.to_dict()
                    for decision in self.service.taskflow_service.list_dispatch_decisions(
                        parts[3], peer_id=peer_id
                    )
                ]
                self._send_json(
                    HTTPStatus.OK, {"ok": True, "decisions": decisions}
                )
                return
        except KeyError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "taskflow_not_found", "message": str(exc)},
            )
            return
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "taskflow_forbidden", "message": str(exc)},
            )
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "taskflow_request_failed", "message": str(exc)},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_taskflow_post(self, path: str) -> None:
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
            if parts == ["remote", "taskflow", "goals"]:
                goal = self.service.taskflow_service.create_goal(
                    title=str(payload.get("title") or ""),
                    prompt=str(payload.get("prompt") or ""),
                    session_id=(
                        str(payload["session_id"])
                        if payload.get("session_id") is not None
                        else None
                    ),
                    peer_id=peer_id,
                    metadata=(
                        dict(payload.get("metadata"))
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                    goal_id=(
                        str(payload["goal_id"])
                        if payload.get("goal_id") is not None
                        else None
                    ),
                )
                self._send_json(
                    HTTPStatus.OK, {"ok": True, "goal": goal.to_dict()}
                )
                return
            if len(parts) == 5 and parts[:3] == ["remote", "taskflow", "goals"]:
                goal_id = parts[3]
                action = parts[4]
                if action == "brief":
                    brief = self.service.taskflow_service.record_brief(
                        goal_id,
                        summary=str(payload.get("summary") or ""),
                        decision_points=(
                            list(payload.get("decision_points"))
                            if isinstance(payload.get("decision_points"), list)
                            else []
                        ),
                        issue_drafts=(
                            list(payload.get("issue_drafts"))
                            if isinstance(payload.get("issue_drafts"), list)
                            else []
                        ),
                        task_drafts=(
                            list(payload.get("task_drafts"))
                            if isinstance(payload.get("task_drafts"), list)
                            else []
                        ),
                        ready=bool(payload.get("ready", False)),
                        metadata=(
                            dict(payload.get("metadata"))
                            if isinstance(payload.get("metadata"), dict)
                            else {}
                        ),
                        peer_id=peer_id,
                    )
                    detail = self.service.taskflow_service.load_goal_detail(
                        goal_id, peer_id=peer_id
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "brief": brief.to_dict(), **detail},
                    )
                    return
                if action == "confirm":
                    goal = self.service.taskflow_service.confirm_goal(
                        goal_id, peer_id=peer_id, confirmed_by="user"
                    )
                    detail = self.service.taskflow_service.load_goal_detail(
                        goal_id, peer_id=peer_id
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "goal": goal.to_dict(), **detail},
                    )
                    return
                if action == "cancel":
                    goal = self.service.taskflow_service.cancel_goal(
                        goal_id,
                        reason=str(payload.get("reason") or "user_cancelled"),
                        peer_id=peer_id,
                    )
                    self._send_json(
                        HTTPStatus.OK, {"ok": True, "goal": goal.to_dict()}
                    )
                    return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "task-drafts"]
            ):
                draft_id = parts[3]
                action = parts[4]
                if action == "dispatch":
                    decision = self.service.taskflow_service.dispatch_task_draft(
                        draft_id,
                        manual_agent_id=(
                            str(payload["manual_agent_id"])
                            if payload.get("manual_agent_id") is not None
                            else None
                        ),
                        peer_id=peer_id,
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "decision": decision.to_dict()},
                    )
                    return
                if action == "assign":
                    agent_id = str(payload.get("agent_id") or "")
                    if not agent_id:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "agent_id_required"},
                        )
                        return
                    decision = self.service.taskflow_service.assign_task_draft(
                        draft_id, agent_id=agent_id, peer_id=peer_id
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "decision": decision.to_dict()},
                    )
                    return
        except KeyError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "taskflow_not_found", "message": str(exc)},
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "taskflow_invalid_state", "message": str(exc)},
            )
            return
        except PermissionError as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "taskflow_forbidden", "message": str(exc)},
            )
            return
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "taskflow_unavailable", "message": str(exc)},
            )
            return
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "taskflow_request_failed", "message": str(exc)},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})


