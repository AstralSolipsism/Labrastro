from __future__ import annotations

import gzip
import json
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
from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.runtime_store import clamp_event_limit
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
        if self._is_admin_config_mutation_path(path):
            current_etag = self.service.admin_manager.config_etag()
            if_match = self._admin_config_if_match(payload)
            if if_match and if_match != current_etag:
                operation, target = self._admin_config_operation(path, payload)
                self._record_admin_config_audit(
                    principal,
                    event_type="admin_config_conflict",
                    path=path,
                    operation=operation,
                    target=target,
                    result="conflict",
                    before={},
                    after={},
                    extra={"config_etag": current_etag, "if_match": if_match},
                )
                self._send_error(
                    HTTPStatus.CONFLICT,
                    "config_version_conflict",
                    "config version conflict",
                    {"config_etag": current_etag},
                )
                return
        try:
            if path.startswith("/remote/admin/github/"):
                if self._handle_admin_github(path, payload):
                    return
            if path == "/remote/admin/status":
                result = {"ok": True, **self.service.admin_manager.status()}
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/remote/admin/agent-runs/submit":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
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
                    agent_run = self.service.runtime_control_plane.submit_agent_run(
                        AgentRunRequest(
                            issue_id=str(payload.get("issue_id") or "manual"),
                            agent_id=str(payload.get("agent_id") or "default"),
                            prompt=str(payload.get("prompt") or ""),
                            source=optional_payload_str(payload, "source") or "manual",
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
                        task_id=optional_payload_str(payload, "agent_run_id"),
                    )
                except ValueError as exc:
                    del exc
                    self._send_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_agent_run",
                        "invalid AgentRun",
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "agent_run": self.service.runtime_control_plane.agent_run_to_dict(
                            agent_run.id
                        ),
                    },
                )
                return
            if path == "/remote/admin/environment/run":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
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
                    self._send_error(
                        exc.status,
                        exc.error,
                        exc.message,
                    )
                    return
                except ValueError as exc:
                    del exc
                    self._send_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_environment_agent_run",
                        "invalid environment AgentRun",
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "agent_run": self.service.runtime_control_plane.agent_run_to_dict(
                            result.agent_run.id
                        ),
                        "agent_id": result.agent_id,
                        "entry_ids": result.entry_ids,
                        "manifest_hash": result.manifest_hash,
                    },
                )
                return
            if path == "/remote/admin/agent-runs/events":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
                    )
                    return
                task_id = str(payload.get("agent_run_id") or "")
                after_seq = int(payload.get("after_seq") or 0)
                limit = clamp_event_limit(int(payload.get("limit") or 200))
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
                return
            if path == "/remote/admin/agent-runs/cancel":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
                    )
                    return
                task_id = str(payload.get("agent_run_id") or "")
                if not task_id:
                    self._send_error(HTTPStatus.BAD_REQUEST, "agent_run_id_required")
                    return
                ok = self.service.runtime_control_plane.cancel_agent_run(
                    task_id,
                    reason=str(payload.get("reason") or "user_cancelled"),
                )
                self._send_json(HTTPStatus.OK, {"ok": ok, "agent_run_id": task_id})
                return
            if path == "/remote/admin/agent-runs/retry":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
                    )
                    return
                task_id = str(payload.get("agent_run_id") or "")
                if not task_id:
                    self._send_error(HTTPStatus.BAD_REQUEST, "agent_run_id_required")
                    return
                try:
                    retry = self.service.runtime_control_plane.retry_agent_run(
                        task_id,
                        new_agent_run_id=(
                            str(payload["new_agent_run_id"])
                            if payload.get("new_agent_run_id") is not None
                            else None
                        ),
                        resume_session=payload.get("resume_session") is True,
                    )
                except KeyError:
                    self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
                    return
                except ValueError as exc:
                    del exc
                    self._send_error(
                        HTTPStatus.BAD_REQUEST,
                        "agent_run_not_retryable",
                        "AgentRun is not retryable",
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "agent_run": self.service.runtime_control_plane.agent_run_to_dict(
                            retry.id
                        ),
                    },
                )
                return
            if path == "/remote/admin/agent-runs/list":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "agent_runs": self.service.runtime_control_plane.list_agent_runs(
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
            if path == "/remote/admin/agent-runs/load":
                if self.service.runtime_control_plane is None:
                    self._send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "agent_runs_unavailable",
                    )
                    return
                task_id = str(payload.get("agent_run_id") or "")
                if not task_id:
                    self._send_error(HTTPStatus.BAD_REQUEST, "agent_run_id_required")
                    return
                try:
                    detail = self.service.runtime_control_plane.load_agent_run_detail(
                        task_id,
                        event_limit=int(payload.get("event_limit") or 100),
                    )
                except KeyError:
                    self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
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
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.update_server_settings(payload),
                )
                self._send_json(result.status, result.payload)
                return
            if path == "/remote/admin/providers/list":
                result = {"ok": True, **self.service.admin_manager.list_providers()}
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/remote/admin/providers/record":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.record_provider(payload),
                )
            elif path == "/remote/admin/providers/test":
                result = self.service.admin_manager.test_provider(payload)
            elif path == "/remote/admin/providers/delete":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.delete_provider(payload),
                )
            elif path == "/remote/admin/providers/copy":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.copy_provider(payload),
                )
            elif path == "/remote/admin/providers/enable":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.enable_provider(payload),
                )
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
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.record_model_profile(payload),
                )
            elif path == "/remote/admin/models/activate":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.activate_model_profile(payload),
                )
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
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.record_toolchain(payload),
                )
            elif path == "/remote/admin/toolchains/delete":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.delete_toolchain(payload),
                )
            elif path == "/remote/admin/toolchains/enable":
                result = self._run_admin_config_mutation(
                    principal,
                    path,
                    payload,
                    lambda: self.service.admin_manager.enable_toolchain(payload),
                )
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found")
                return
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "admin_request_failed",
            )
            return
        self._send_json(result.status, result.payload)

    def _run_admin_config_mutation(
        self,
        principal: Any,
        path: str,
        payload: dict[str, Any],
        callback: Any,
    ) -> Any:
        operation, target = self._admin_config_operation(path, payload)
        before = self.service.admin_manager._load_data()
        try:
            result = callback()
        except Exception:
            self._record_admin_config_audit(
                principal,
                event_type="admin_config_failed",
                path=path,
                operation=operation,
                target=target,
                result="failed",
                before=before,
                after=before,
                extra={"error": "admin_request_failed"},
            )
            raise
        after = self.service.admin_manager._load_data()
        if getattr(result, "ok", False):
            result.payload.setdefault(
                "config_etag",
                self.service.admin_manager.config_etag(after),
            )
            event_type = "admin_config_updated"
            outcome = "updated"
        else:
            event_type = "admin_config_failed"
            outcome = "failed"
        self._record_admin_config_audit(
            principal,
            event_type=event_type,
            path=path,
            operation=operation,
            target=target,
            result=outcome,
            before=before,
            after=after,
            extra={
                "status": int(getattr(result, "status", 500) or 500),
                "error": getattr(result, "payload", {}).get("error")
                if isinstance(getattr(result, "payload", None), dict)
                else None,
            },
        )
        return result

    def _admin_config_if_match(self, payload: dict[str, Any]) -> str:
        header = self.headers.get("If-Match", "")
        if isinstance(header, str) and header.strip():
            return header.strip()
        value = payload.get("if_match")
        return str(value).strip() if value is not None else ""

    def _is_admin_config_mutation_path(self, path: str) -> bool:
        return path in {
            "/remote/admin/server-settings/update",
            "/remote/admin/providers/record",
            "/remote/admin/providers/delete",
            "/remote/admin/providers/copy",
            "/remote/admin/providers/enable",
            "/remote/admin/models/record",
            "/remote/admin/models/activate",
            "/remote/admin/toolchains/record",
            "/remote/admin/toolchains/delete",
            "/remote/admin/toolchains/enable",
        }

    def _admin_config_operation(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[str, str]:
        operation = path.rsplit("/", 1)[-1].replace("-", "_")
        if path == "/remote/admin/server-settings/update":
            targets = []
            settings = payload.get("settings")
            if isinstance(settings, dict):
                targets.extend(str(key) for key in settings if str(key).strip())
            for key in ("agent_registry", "runtime_profiles", "run_limits", "sandbox_provider"):
                if isinstance(payload.get(key), dict):
                    targets.append(key)
            if isinstance(payload.get("github"), dict):
                targets.append("github")
            return operation, ",".join(sorted(set(targets))) or "server_settings"
        for key in ("provider_id", "profile_id", "name", "id", "target_id"):
            if payload.get(key) is not None:
                return operation, str(payload.get(key) or "")
        toolchain = payload.get("toolchain")
        if isinstance(toolchain, dict) and toolchain.get("name") is not None:
            return operation, str(toolchain.get("name") or "")
        return operation, ""

    def _record_admin_config_audit(
        self,
        principal: Any,
        *,
        event_type: str,
        path: str,
        operation: str,
        target: str,
        result: str,
        before: dict[str, Any],
        after: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> None:
        store = getattr(getattr(self.service, "auth_service", None), "store", None)
        append = getattr(store, "append_audit_event", None)
        if not callable(append):
            return
        payload = {
            "request_id": self._request_id(),
            "path": path,
            "operation": operation,
            "target": target,
            "result": result,
            "diff": self._redacted_config_diff(before, after),
        }
        payload.update({key: value for key, value in (extra or {}).items() if value is not None})
        append(
            {
                "id": "evt_" + uuid.uuid4().hex,
                "type": event_type,
                "created_at": time.time(),
                "user_id": str(getattr(principal, "user_id", "") or ""),
                "username": str(getattr(principal, "username", "") or ""),
                "device_id": str(getattr(principal, "device_id", "") or ""),
                "source_ip": self.client_address[0] if self.client_address else "",
                "payload": payload,
            }
        )

    def _redacted_config_diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> list[dict[str, Any]]:
        diff: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            before_value = self._redact_config_value(key, before.get(key))
            after_value = self._redact_config_value(key, after.get(key))
            if before_value == after_value:
                continue
            diff.append(
                {
                    "path": key,
                    "before": before_value,
                    "after": after_value,
                }
            )
        return diff

    def _redact_config_value(self, key: str, value: Any) -> Any:
        key_lower = str(key).lower()
        if any(token in key_lower for token in ("secret", "password", "token", "api_key")):
            if value in (None, ""):
                return ""
            return "***"
        if isinstance(value, dict):
            return {
                str(item_key): self._redact_config_value(str(item_key), item_value)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self._redact_config_value(key, item) for item in value]
        return value

    def _admin_scope_for_path(self, path: str) -> str:
        read_paths = {
            "/remote/admin/status",
            "/remote/admin/github/status",
            "/remote/admin/agent-runs/events",
            "/remote/admin/agent-runs/list",
            "/remote/admin/agent-runs/load",
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
