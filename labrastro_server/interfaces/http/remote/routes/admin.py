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
from labrastro_server.services.environment_run import (
    EnvironmentRunError,
    EnvironmentRunService,
)
from reuleauxcoder.interfaces.events import UIEventKind

class RemoteAdminRoutes:
    def _handle_admin(self, path: str) -> None:
        payload = self._read_json()
        principal = self._require_auth_scopes({self._admin_scope_for_path(path)})
        if principal is None:
            return
        try:
            if path.startswith("/remote/admin/github/"):
                if self._handle_admin_github(path, payload):
                    return
            if path == "/remote/admin/status":
                result = {"ok": True, **self.service.admin_manager.status()}
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/remote/admin/runtime/submit":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                metadata = (
                    dict(payload.get("metadata", {}))
                    if isinstance(payload.get("metadata"), dict)
                    else {}
                )
                if payload.get("workspace_root") is not None:
                    metadata.setdefault(
                        "workspace_root", str(payload["workspace_root"])
                    )
                try:
                    task = self.service.runtime_control_plane.submit_task(
                        RuntimeTaskRequest(
                            issue_id=str(payload.get("issue_id") or "manual"),
                            agent_id=str(payload.get("agent_id") or "default"),
                            prompt=str(payload.get("prompt") or ""),
                            executor=optional_payload_str(payload, "executor"),
                            execution_location=optional_payload_str(
                                payload, "execution_location"
                            ),
                            trigger_mode=optional_payload_str(
                                payload, "trigger_mode"
                            )
                            or "issue_task",
                            runtime_profile_id=optional_payload_str(
                                payload, "runtime_profile_id"
                            ),
                            workdir=optional_payload_str(payload, "workdir"),
                            model=optional_payload_str(payload, "model"),
                            metadata=metadata,
                        ),
                        task_id=optional_payload_str(payload, "task_id"),
                    )
                except ValueError as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "invalid_runtime_task",
                            "message": str(exc),
                        },
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "task": self.service.runtime_control_plane.task_to_dict(
                            task.id
                        ),
                    },
                )
                return
            if path == "/remote/admin/environment/run":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                entry_ids = payload.get("entry_ids", [])
                if isinstance(entry_ids, list):
                    normalized_entry_ids = [
                        str(entry_id)
                        for entry_id in entry_ids
                        if str(entry_id).strip()
                    ]
                else:
                    normalized_entry_ids = []
                workspace_root = str(payload.get("workspace_root") or "")
                manifest = self.service._build_environment_manifest(
                    "",
                    "",
                    workspace_root,
                )
                try:
                    result = EnvironmentRunService(
                        self.service.runtime_control_plane
                    ).submit(
                        mode=str(payload.get("mode") or "check"),
                        manifest=manifest,
                        workspace_root=workspace_root,
                        entry_ids=normalized_entry_ids,
                        agent_id=optional_payload_str(payload, "agent_id"),
                    )
                except EnvironmentRunError as exc:
                    self._send_json(
                        exc.status,
                        {"error": exc.error, "message": exc.message},
                    )
                    return
                except ValueError as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "invalid_environment_task",
                            "message": str(exc),
                        },
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "task": self.service.runtime_control_plane.task_to_dict(
                            result.task.id
                        ),
                        "agent_id": result.agent_id,
                        "entry_ids": result.entry_ids,
                        "manifest_hash": result.manifest_hash,
                    },
                )
                return
            if path == "/remote/admin/runtime/events":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                task_id = str(payload.get("task_id") or "")
                after_seq = int(payload.get("after_seq") or 0)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "events": [
                            event.to_dict()
                            for event in self.service.runtime_control_plane.list_events(
                                task_id, after_seq=after_seq
                            )
                        ],
                    },
                )
                return
            if path == "/remote/admin/runtime/cancel":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                task_id = str(payload.get("task_id") or "")
                if not task_id:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "task_id_required"}
                    )
                    return
                ok = self.service.runtime_control_plane.cancel_task(
                    task_id,
                    reason=str(payload.get("reason") or "user_cancelled"),
                )
                self._send_json(HTTPStatus.OK, {"ok": ok, "task_id": task_id})
                return
            if path == "/remote/admin/runtime/retry":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                task_id = str(payload.get("task_id") or "")
                if not task_id:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "task_id_required"}
                    )
                    return
                try:
                    retry = self.service.runtime_control_plane.retry_task(
                        task_id,
                        new_task_id=(
                            str(payload["new_task_id"])
                            if payload.get("new_task_id") is not None
                            else None
                        ),
                        resume_session=payload.get("resume_session") is True,
                    )
                except KeyError:
                    self._send_json(
                        HTTPStatus.NOT_FOUND, {"error": "task_not_found"}
                    )
                    return
                except ValueError as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "task_not_retryable", "message": str(exc)},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "task": self.service.runtime_control_plane.task_to_dict(
                            retry.id
                        ),
                    },
                )
                return
            if path == "/remote/admin/runtime/tasks/list":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "tasks": self.service.runtime_control_plane.list_tasks(
                            status=optional_payload_str(payload, "status"),
                            agent_id=optional_payload_str(payload, "agent_id"),
                            issue_id=optional_payload_str(payload, "issue_id"),
                            limit=int(payload.get("limit") or 50),
                            after_created_at=optional_payload_str(
                                payload, "after_created_at"
                            ),
                        ),
                    },
                )
                return
            if path == "/remote/admin/runtime/tasks/load":
                if self.service.runtime_control_plane is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "agent_runtime_unavailable"},
                    )
                    return
                task_id = str(payload.get("task_id") or "")
                if not task_id:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "task_id_required"}
                    )
                    return
                try:
                    detail = self.service.runtime_control_plane.load_task_detail(
                        task_id,
                        event_limit=int(payload.get("event_limit") or 100),
                    )
                except KeyError:
                    self._send_json(
                        HTTPStatus.NOT_FOUND, {"error": "task_not_found"}
                    )
                    return
                github_pr_service = self.service.github_pr_service
                if github_pr_service is not None:
                    github_pr = github_pr_service.store.get_pull_request_for_task(task_id)
                    detail["github_pull_request"] = (
                        github_pr.to_dict() if github_pr is not None else None
                    )
                    detail["github_review_comments"] = github_pr_service.list_review_comments(
                        task_id
                    )
                else:
                    detail["github_pull_request"] = None
                    detail["github_review_comments"] = []
                self._send_json(HTTPStatus.OK, {"ok": True, **detail})
                return
            if path == "/remote/admin/server-settings/read":
                result = {
                    "ok": True,
                    **self.service.admin_manager.read_server_settings(),
                }
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/remote/admin/server-settings/update":
                result = self.service.admin_manager.update_server_settings(payload)
                self._send_json(result.status, result.payload)
                return
            if path == "/remote/admin/providers/list":
                result = {"ok": True, **self.service.admin_manager.list_providers()}
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/remote/admin/providers/record":
                result = self.service.admin_manager.record_provider(payload)
            elif path == "/remote/admin/providers/test":
                result = self.service.admin_manager.test_provider(payload)
            elif path == "/remote/admin/providers/delete":
                result = self.service.admin_manager.delete_provider(payload)
            elif path == "/remote/admin/providers/copy":
                result = self.service.admin_manager.copy_provider(payload)
            elif path == "/remote/admin/providers/enable":
                result = self.service.admin_manager.enable_provider(payload)
            elif path == "/remote/admin/providers/models":
                result = self.service.admin_manager.list_provider_models(payload)
            elif path == "/remote/admin/models/list":
                result = {
                    "ok": True,
                    **self.service.admin_manager.list_model_profiles(),
                }
                self._send_json(HTTPStatus.OK, result)
                return
            elif path == "/remote/admin/models/record":
                result = self.service.admin_manager.record_model_profile(payload)
            elif path == "/remote/admin/models/activate":
                result = self.service.admin_manager.activate_model_profile(payload)
            elif path == "/remote/admin/toolchains/list":
                result = {
                    "ok": True,
                    **self.service.admin_manager.list_toolchains(),
                }
                self._send_json(HTTPStatus.OK, result)
                return
            elif path == "/remote/admin/toolchains/dashboard":
                result = {
                    "ok": True,
                    **self.service.admin_manager.toolchain_dashboard(),
                }
                self._send_json(HTTPStatus.OK, result)
                return
            elif path == "/remote/admin/toolchains/record":
                result = self.service.admin_manager.record_toolchain(payload)
            elif path == "/remote/admin/toolchains/delete":
                result = self.service.admin_manager.delete_toolchain(payload)
            elif path == "/remote/admin/toolchains/enable":
                result = self.service.admin_manager.enable_toolchain(payload)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "admin_request_failed", "message": str(exc)},
            )
            return
        self._send_json(result.status, result.payload)

    def _admin_scope_for_path(self, path: str) -> str:
        read_paths = {
            "/remote/admin/status",
            "/remote/admin/github/status",
            "/remote/admin/runtime/events",
            "/remote/admin/runtime/tasks/list",
            "/remote/admin/runtime/tasks/load",
            "/remote/admin/server-settings/read",
            "/remote/admin/providers/list",
            "/remote/admin/providers/models",
            "/remote/admin/models/list",
            "/remote/admin/toolchains/list",
            "/remote/admin/toolchains/dashboard",
        }
        if path in read_paths:
            return "admin:read"
        return "admin:write"
