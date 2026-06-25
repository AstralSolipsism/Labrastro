from __future__ import annotations

import gzip
import json
import logging
import threading
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
from labrastro_server.interfaces.http.remote.session_run_control import (
    SessionRunControlPolicy,
    SessionRunControlResolution,
    SessionRunControlResolver,
    SessionRunControlScopeProof,
)
from labrastro_server.interfaces.http.remote.protocol import (
    ApprovalReplyRequest,
    ApprovalReplyResponse,
    SessionRunCancelRequest,
    SessionRunCancelResponse,
    SessionRunStopRequest,
    SessionRunStopResponse,
    ChatCommandDispatchRequest,
    SessionRunContinueRequest,
    SessionRunContinueResponse,
    SessionRunBranchSelectRequest,
    SessionRunRecoverRequest,
    SessionRunRecoverResponse,
    SessionRunStartRequest,
    SessionRunStartResponse,
    SessionRunStatusRequest,
    SessionRunStatusResponse,
    SessionRunUserInputReplyRequest,
    SessionRunUserInputReplyResponse,
    SessionRunEventsRequest,
    SessionRunEventsBatch,
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
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunActivationInputKind,
    AgentRunRequest,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from reuleauxcoder.domain.agent_runtime.models import (
    ExecutionLocation,
    ModelRequestOrigin,
    WorkerKind,
)
from reuleauxcoder.domain.config.models import (
    DEFAULT_MAIN_CHAT_AGENT_ID,
)
from reuleauxcoder.interfaces.events import UIEventKind

logger = logging.getLogger(__name__)


def _remote_runtime_error_payload(exc: Exception) -> dict[str, Any]:
    message = str(exc).strip()
    code = getattr(exc, "code", None)
    protocol_message = getattr(exc, "message", None)
    details = getattr(exc, "details", None)
    provider_diagnostic = getattr(exc, "provider_diagnostic", None)
    diagnostic_payload = details if isinstance(details, dict) else {}
    if isinstance(provider_diagnostic, dict):
        diagnostic_payload = {**diagnostic_payload, "provider_diagnostic": provider_diagnostic}
    if isinstance(code, str) and code.startswith("REMOTE_"):
        return {
            **diagnostic_payload,
            "message": str(protocol_message or message or code),
            "code": code,
        }
    if isinstance(exc, ValueError) and message.startswith("remote peer "):
        return {**diagnostic_payload, "message": message, "code": "session_run_handler_failed"}
    return {
        **diagnostic_payload,
        "message": str(diagnostic_payload.get("message") or "session_run_handler_failed"),
        "code": str(diagnostic_payload.get("code") or "session_run_handler_failed"),
    }


def _required_scope_error(exc: Exception) -> str:
    if not isinstance(exc, ValueError):
        return ""
    code = str(exc).strip()
    if code in {"branch_binding_id_required", "session_run_id_required"}:
        return code
    return ""


def _sse_wait_timeout(timeout_sec: float) -> float:
    if timeout_sec <= 0:
        return 15.0
    return min(max(timeout_sec, 0.05), 30.0)


_WORKFLOW_MODE_VALUES = frozenset({"chat", "taskflow", "capability_package_ingest"})


def _reserved_workflow_mode_name(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in _WORKFLOW_MODE_VALUES else ""


def _binding_status_value(binding: Any) -> str:
    status = getattr(binding, "status", "active")
    return str(getattr(status, "value", status) or "active").strip().lower()


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _agent_run_status_value(agent_run: Any) -> str:
    return _enum_str(getattr(agent_run, "status", ""))


def _agent_run_mainline_state_value(agent_run: Any) -> str:
    return _enum_str(getattr(agent_run, "mainline_state", ""))


def _agent_run_activation_state_value(agent_run: Any) -> str:
    return _enum_str(getattr(agent_run, "activation_state", ""))


def _agent_run_waiting_reason_value(agent_run: Any) -> str:
    return _enum_str(getattr(agent_run, "waiting_reason", ""))


def _current_activation_status(control_plane: Any, agent_run: Any) -> str:
    activation_id = str(getattr(agent_run, "current_activation_id", "") or "").strip()
    if not activation_id:
        return "none"
    loader = getattr(control_plane, "load_agent_run_detail", None)
    if callable(loader):
        try:
            detail = loader(str(getattr(agent_run, "id", "") or ""), event_limit=1)
        except Exception:
            detail = {}
        activations = detail.get("activations") if isinstance(detail, dict) else None
        if isinstance(activations, list):
            for activation in activations:
                if not isinstance(activation, dict):
                    continue
                if str(activation.get("id") or activation.get("activation_id") or "") != activation_id:
                    continue
                status = str(activation.get("status") or "").strip().lower()
                if status:
                    return status
    return _agent_run_status_value(agent_run) or "none"


def _activation_state_for(agent_run: Any, activation_status: str) -> str:
    stored = _agent_run_activation_state_value(agent_run)
    if stored:
        return stored
    status = str(activation_status or "").strip().lower()
    if not status:
        return "none"
    if status == "waiting":
        if _agent_run_waiting_reason_value(agent_run) in {"user_approval", "user_input"}:
            return "waiting_user"
        return "waiting_server"
    return status


def _agent_run_state_for(agent_run: Any, binding_status: str) -> str:
    status = _agent_run_status_value(agent_run)
    stored = _agent_run_mainline_state_value(agent_run)
    if binding_status in {"closed", "deleted"}:
        if status == "cancelled":
            return "cancelled"
        if status == "failed":
            return "failed"
        if status == "blocked" or stored == "unrecoverable":
            return "unrecoverable"
        return "closed"
    if stored:
        return stored
    if status in {"queued", "dispatched", "running"}:
        return "executing"
    if status == "waiting":
        if _agent_run_waiting_reason_value(agent_run) in {"user_approval", "user_input"}:
            return "waiting_feedback"
        return "executing"
    if status == "completed":
        return "continuable"
    if status == "blocked":
        return "blocked"
    if status in {"cancelled", "failed"}:
        return status
    return "none"


def _mainline_state_for(
    agent_run: Any,
    *,
    binding_status: str,
    agent_run_state: str,
    activation_state: str,
) -> str:
    status = _agent_run_status_value(agent_run)
    stored = _agent_run_mainline_state_value(agent_run)
    if binding_status in {"closed", "deleted"}:
        if status == "cancelled":
            return "cancelled"
        if status == "failed":
            return "failed"
        if status == "blocked" or stored == "unrecoverable":
            return "unrecoverable"
        return "closed"
    if stored == "continuable":
        return "settled"
    if stored == "waiting_feedback":
        return "waiting_user"
    if stored:
        return stored
    if status in {"queued", "dispatched", "running"}:
        return "executing"
    if status == "waiting":
        return "waiting_user" if activation_state == "waiting_user" else "executing"
    if status == "completed" and agent_run_state == "continuable":
        return "settled"
    if status == "blocked":
        return "blocked"
    if status in {"cancelled", "failed"}:
        return status
    return "none"


def _projection_state_for(
    projection_state: str,
    *,
    mainline_state: str,
    binding_status: str,
) -> str:
    if mainline_state == "unrecoverable":
        return "nonrecoverable"
    if binding_status in {"closed", "deleted"} or mainline_state in {
        "closed",
        "cancelled",
        "failed",
    }:
        return "drained"
    if mainline_state == "settled":
        return "drained"
    state = str(projection_state or "").strip().lower()
    if state in {"live", "recovered", "drained", "nonrecoverable"}:
        return state
    return "live"


def _closed_reason_for(binding: Any, agent_run: Any) -> str | None:
    binding_status = _binding_status_value(binding)
    if binding_status not in {"closed", "deleted"}:
        return None
    metadata = getattr(binding, "metadata", None)
    reason = ""
    if isinstance(metadata, dict):
        reason = str(metadata.get("status_reason") or "").strip().lower()
    agent_status = _agent_run_status_value(agent_run)
    if binding_status == "deleted" or "branch_deleted" in reason:
        return "branch_deleted"
    if "scope" in reason or "owner_session_deleted" in reason:
        return "scope_invalid"
    if agent_status == "cancelled":
        return "user_cancelled"
    if agent_status == "failed":
        return "mainline_failed"
    if agent_status == "blocked" or "unrecoverable" in reason:
        return "unrecoverable_failure"
    if "explicit" in reason or "user_closed" in reason:
        return "explicit_close"
    return "explicit_close"


def _session_run_status_facts(
    *,
    control_plane: Any,
    agent_run: Any,
    binding: Any,
    projection_state: str,
) -> dict[str, Any]:
    binding_status = _binding_status_value(binding)
    activation_status = _current_activation_status(control_plane, agent_run)
    activation_state = _activation_state_for(agent_run, activation_status)
    agent_run_state = _agent_run_state_for(agent_run, binding_status)
    mainline_state = _mainline_state_for(
        agent_run,
        binding_status=binding_status,
        agent_run_state=agent_run_state,
        activation_state=activation_state,
    )
    working = (
        binding_status == "active"
        and activation_state in {"queued", "dispatched", "running", "waiting_server"}
        and mainline_state == "executing"
    )
    continuable = (
        binding_status == "active"
        and mainline_state == "settled"
        and agent_run_state == "continuable"
    )
    resolved_projection_state = _projection_state_for(
        projection_state,
        mainline_state=mainline_state,
        binding_status=binding_status,
    )
    recoverable = (
        binding_status == "active"
        and mainline_state
        in {"executing", "waiting_user", "blocked", "settled"}
        and resolved_projection_state in {"live", "recovered", "drained"}
    )
    event_stream_allowed = (
        working and resolved_projection_state in {"live", "recovered"}
    )
    terminal = binding_status in {"closed", "deleted"} or mainline_state in {
        "closed",
        "cancelled",
        "failed",
        "unrecoverable",
    }
    transport_state = (
        "streaming"
        if event_stream_allowed
        else "closed"
        if terminal
        else "disconnected"
    )
    return {
        "mainlineState": mainline_state,
        "agentRunState": agent_run_state,
        "activationState": activation_state,
        "bindingStatus": binding_status,
        "projectionState": resolved_projection_state,
        "working": working,
        "continuable": continuable,
        "recoverable": recoverable,
        "eventStreamAllowed": event_stream_allowed,
        "terminal": terminal,
        "closedReason": _closed_reason_for(binding, agent_run),
        "transportState": transport_state,
    }


class RemoteChatRoutes:
    def _has_chat_model_context(self, peer_id: str, req: SessionRunStartRequest) -> bool:
        provider_id = str(req.provider_id or "").strip()
        model_id = str(req.model_id or "").strip()
        if provider_id and model_id:
            return True
        if provider_id or model_id:
            return False
        session_id = str(req.session_hint or "").strip()
        if not session_id or self.service.session_handler is None:
            return False
        try:
            payload = dict(
                self.service.session_handler(
                    "load",
                    peer_id,
                    {"session_id": session_id},
                )
            )
        except Exception:
            return False
        if payload.get("ok") is False:
            return False
        runtime_state = payload.get("runtime_state")
        if not isinstance(runtime_state, dict):
            return False
        active_provider = str(
            runtime_state.get("active_model_provider")
            or runtime_state.get("provider_id")
            or runtime_state.get("provider")
            or ""
        ).strip()
        active_model = str(
            runtime_state.get("active_model")
            or runtime_state.get("model_id")
            or runtime_state.get("model")
            or ""
        ).strip()
        return bool(active_provider and active_model)

    def _session_run_control_resolver(self) -> SessionRunControlResolver:
        return SessionRunControlResolver(
            self.service,
            binding_peer_matches=self._session_binding_peer_matches,
        )

    def _resolve_session_run_control(
        self,
        peer_token: str,
        session_run_id: str,
        branch_binding_id: str | None,
        *,
        required: bool = True,
        include_inactive_binding: bool = False,
    ):
        resolution = self._resolve_session_run_control_resolution(
            peer_token,
            session_run_id,
            SessionRunControlPolicy(
                branch_binding_id=branch_binding_id,
                require_branch_binding_id=required,
                include_inactive_binding=include_inactive_binding,
            ),
        )
        if resolution is not None:
            return resolution.peer_id, resolution.session, resolution.binding, resolution.scope
        return None

    def _resolve_session_run_control_resolution(
        self,
        peer_token: str,
        session_run_id: str,
        policy: SessionRunControlPolicy,
    ) -> SessionRunControlResolution | None:
        resolution = self._session_run_control_resolver().resolve(
            peer_token,
            session_run_id,
            policy,
        )
        if resolution.kind == "ok":
            return resolution
        self._send_session_run_control_error(resolution)
        return None

    def _send_session_run_control_error(
        self,
        resolution: SessionRunControlResolution,
    ) -> None:
        if resolution.kind == "invalid_peer_token":
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
        elif resolution.kind == "session_run_not_found":
            self._send_error(HTTPStatus.NOT_FOUND, "session_run_not_found")
        elif resolution.kind == "session_run_projection_nonrecoverable":
            self._send_error(
                HTTPStatus.CONFLICT,
                "session_run_projection_nonrecoverable",
                "SessionRun projection cannot be recovered from persisted binding and AgentRun facts.",
                resolution.details,
            )
        elif resolution.kind == "session_run_binding_store_unavailable":
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_binding_store_unavailable",
                str(resolution.details.get("message") or "SessionRun binding store is unavailable."),
            )
        elif resolution.kind == "agent_runs_unavailable":
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
        elif resolution.kind == "session_run_bindings_unavailable":
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_bindings_unavailable",
            )
        elif resolution.kind == "branch_binding_id_required":
            self._send_error(HTTPStatus.BAD_REQUEST, "branch_binding_id_required")
        elif resolution.kind == "session_run_branch_binding_not_found":
            self._send_error(HTTPStatus.NOT_FOUND, "session_run_branch_binding_not_found")
        elif resolution.kind == "session_run_binding_not_found":
            self._send_error(HTTPStatus.CONFLICT, "session_run_binding_not_found")
        elif resolution.kind == "session_run_binding_peer_mismatch":
            self._send_error(HTTPStatus.FORBIDDEN, "session_run_binding_peer_mismatch")
        elif resolution.kind == "session_run_scope_proof_invalid":
            self._send_error(HTTPStatus.CONFLICT, "session_run_scope_proof_invalid")

    def _send_invalid_session_run_request_error(
        self,
        exc: Exception,
        invalid_error: str,
    ) -> None:
        scope_error = _required_scope_error(exc)
        if scope_error:
            self._send_error(HTTPStatus.BAD_REQUEST, scope_error)
            return
        self._send_error(HTTPStatus.BAD_REQUEST, invalid_error)

    def _scope_proof_for(self, session, binding) -> SessionRunControlScopeProof | None:
        scope = SessionRunControlScopeProof.from_binding(
            session_run_id=session.session_run_id,
            binding=binding,
        )
        if scope is None:
            self._send_error(HTTPStatus.CONFLICT, "session_run_scope_proof_invalid")
            return None
        return scope

    def _send_session_run_status_response(
        self,
        session,
        binding,
        cursor: int,
        scope: SessionRunControlScopeProof,
        *,
        projection_state: str = "live",
    ) -> None:
        agent_run = None
        try:
            agent_run = self.service.runtime_control_plane.get_agent_run(
                binding.agent_run_id
            )
        except Exception:
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        binding_status = _binding_status_value(binding)
        agent_run_status = _agent_run_status_value(agent_run)
        if binding_status == "active" and agent_run_status in {"failed", "cancelled"}:
            closer = getattr(self.service.runtime_control_plane, "close_session_run_binding", None)
            if callable(closer):
                closed_ok = closer(
                    str(getattr(binding, "id", "") or ""),
                    reason=f"agent_run_closed:{agent_run_status}",
                )
                closed = None
                finder = getattr(self.service.runtime_control_plane, "find_session_run_binding", None)
                if closed_ok and callable(finder):
                    closed = finder(
                        session_run_id=str(getattr(binding, "session_run_id", "") or ""),
                        branch_binding_id=str(getattr(binding, "branch_binding_id", "") or ""),
                        selected_only=False,
                        include_inactive=True,
                    )
                if closed is not None:
                    binding = closed
                    binding_status = _binding_status_value(binding)
                    if hasattr(session, "record_branch_binding"):
                        session.record_branch_binding(binding)
        facts = _session_run_status_facts(
            control_plane=self.service.runtime_control_plane,
            agent_run=agent_run,
            binding=binding,
            projection_state=projection_state,
        )
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        run_status = str(agent_run.status.value)
        status_payload = writer.status_response_payload(
            cursor,
            agent_run_status=run_status,
            activation_id=str(agent_run.current_activation_id or ""),
            terminal=bool(facts["terminal"]),
            selected=scope.selected,
        )
        status_payload["running"] = bool(facts["working"])
        status_payload["done"] = bool(facts["terminal"])
        status_payload["reconnectable"] = bool(facts["eventStreamAllowed"])
        if facts["mainlineState"] in {"settled", "blocked", "waiting_user"}:
            status_payload["status"] = facts["mainlineState"]
        elif facts["terminal"]:
            status_payload["status"] = facts["mainlineState"]
        status_payload["terminal"] = bool(facts["terminal"])
        status_payload["bindingStatus"] = facts["bindingStatus"]
        status_payload["recoverable"] = bool(facts["recoverable"])
        status_payload["eventStreamAllowed"] = bool(facts["eventStreamAllowed"])
        status_payload["projectionState"] = facts["projectionState"]
        status_payload["mainlineState"] = facts["mainlineState"]
        status_payload["agentRunState"] = facts["agentRunState"]
        status_payload["activationState"] = facts["activationState"]
        status_payload["working"] = bool(facts["working"])
        status_payload["continuable"] = bool(facts["continuable"])
        status_payload["closedReason"] = facts["closedReason"]
        status_payload["transportState"] = facts["transportState"]

        self._send_json(
            HTTPStatus.OK,
            SessionRunStatusResponse.from_dict(
                status_payload
            ).to_dict(),
        )

    def _session_run_start_mainline_response(
        self,
        session,
        binding,
        scope: SessionRunControlScopeProof,
    ) -> SessionRunStartResponse:
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        agent_run = self.service.runtime_control_plane.get_agent_run(
            scope.agent_run_id
        )
        activation_id = str(agent_run.current_activation_id or "")
        status_payload = writer.status_response_payload(
            0,
            agent_run_status=str(agent_run.status.value),
            activation_id=activation_id,
            terminal=str(agent_run.status.value).strip().lower()
            in {"failed", "cancelled"},
            selected=scope.selected,
        )
        runtime_state = (
            dict(status_payload.get("runtime_state"))
            if isinstance(status_payload.get("runtime_state"), dict)
            else {}
        )
        return SessionRunStartResponse(
            session_run_id=session.session_run_id,
            session_id=session.session_id,
            agent_run_id=scope.agent_run_id,
            activation_id=activation_id or None,
            branch_binding_id=scope.branch_binding_id,
            scope_id=scope.scope_id,
            selected=scope.selected,
            agent_id=session.agent_id,
            workflow_mode=session.workflow_mode,
            runtime_state=runtime_state,
        )

    def _handle_session_run_start(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunStartRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_start_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return

        workflow_mode = (
            str(req.workflow_mode).strip().lower()
            if req.workflow_mode is not None
            else None
        )
        reserved_mode = _reserved_workflow_mode_name(req.mode)
        if reserved_mode:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_session_run_mode",
                "mode selects an executor mode profile; use workflow_mode for chat/taskflow routing",
            )
            return
        has_provider = bool(str(req.provider_id or "").strip())
        has_model = bool(str(req.model_id or "").strip())
        if has_provider != has_model:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "provider_model_required",
                "provider_id and model_id must be provided together",
            )
            return
        if getattr(self.service, "require_explicit_chat_model", False):
            if not self._has_chat_model_context(peer_id, req):
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "model_selection_required",
                    "sessionRun.start requires provider_id and model_id for a new session run",
                )
                return
        existing = self.service._get_session_run_by_request(
            peer_id,
            req.session_hint,
            req.client_request_id,
        )
        if existing is not None:
            resolution = self._session_run_control_resolver().resolve(
                req.peer_token,
                existing.session_run_id,
                SessionRunControlPolicy(
                    branch_binding_id="main",
                    require_branch_binding_id=True,
                ),
            )
            if resolution.kind != "ok":
                self._send_session_run_control_error(resolution)
                return
            try:
                response = self._session_run_start_mainline_response(
                    resolution.session,
                    resolution.binding,
                    resolution.scope,
                )
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
                return
            self._send_json(
                HTTPStatus.OK,
                response.to_dict(),
            )
            return
        session = self.service._create_session_run(
            peer_id,
            req.session_hint,
            mode=req.mode,
            workflow_mode=workflow_mode,
            taskflow_id=req.taskflow_id,
            provider_id=req.provider_id,
            model_id=req.model_id,
            client_request_id=req.client_request_id,
            branch_binding_id="main",
            model_parameters=req.parameters,
            locale=req.locale,
            mentions=req.mentions,
            initial_prompt=req.prompt,
        )
        agent_id = str(req.taskflow_id or DEFAULT_MAIN_CHAT_AGENT_ID).strip()
        metadata: dict[str, Any] = {
            "remote_peer_id": peer_id,
            "session_hint": str(req.session_hint or ""),
        }
        if workflow_mode:
            metadata["workflow_mode"] = workflow_mode
        if req.mode:
            metadata["mode"] = str(req.mode)
        if req.locale:
            metadata["locale"] = str(req.locale)
        if req.mentions:
            metadata["mentions"] = list(req.mentions)
        try:
            agent_run = self.service.runtime_control_plane.submit_agent_run(
                AgentRunRequest(
                    agent_id=agent_id,
                    prompt=req.prompt,
                    owner_session_run_id=session.session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
                    execution_location=ExecutionLocation.REMOTE_SERVER,
                    worker_kind=WorkerKind.SERVER_WORKER,
                    model_request_origin=ModelRequestOrigin.SERVER,
                    model=req.model_id,
                    metadata=metadata,
                )
            )
        except ValueError as exc:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_session_run_agent_run",
                str(exc) or "invalid AgentRun",
            )
            return
        binding = self.service.runtime_control_plane.create_session_run_binding(
            session_run_id=session.session_run_id,
            session_id=session.session_id or "",
            peer_id=peer_id,
            agent_run_id=agent_run.id,
            branch_binding_id="main",
            selected=True,
            target_agent_run_id=agent_run.id,
            metadata={"binding_kind": "mainline"},
        )
        session.agent_id = agent_run.agent_id
        session.record_branch_binding(binding)
        scope = self._scope_proof_for(session, binding)
        if scope is None:
            return
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        runtime_state = writer.apply_selected_runtime_scope(
            activation_id=str(agent_run.current_activation_id or "")
        )
        writer.append_event(
            "session_run_start",
            {
                "prompt": req.prompt,
                "mode": req.mode,
                "workflow_mode": workflow_mode,
                "taskflow_id": req.taskflow_id,
                "provider_id": req.provider_id,
                "model_id": req.model_id,
                "locale": req.locale,
                "mentions": list(req.mentions),
                "agent_run_id": agent_run.id,
                "activation_id": agent_run.current_activation_id,
                "branch_binding_id": binding.branch_binding_id,
            },
        )
        writer.append_event(
            "session_run_binding_selected",
            {
                "agent_run_id": agent_run.id,
                "branch_binding_id": binding.branch_binding_id,
                "binding_id": binding.id,
            },
        )
        writer.mark_running()
        self._send_json(
            HTTPStatus.OK,
            SessionRunStartResponse(
                session_run_id=session.session_run_id,
                session_id=session.session_id,
                agent_run_id=agent_run.id,
                activation_id=str(agent_run.current_activation_id or "") or None,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
                agent_id=agent_run.agent_id,
                workflow_mode=workflow_mode,
                runtime_state=runtime_state,
            ).to_dict(),
        )

    def _handle_session_run_continue(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunContinueRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_continue_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
            include_inactive_binding=True,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        self.service._project_agent_run_events_to_session_run(session, binding)
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        try:
            agent_run = self.service.runtime_control_plane.continue_agent_run(
                binding.agent_run_id,
                input_kind=AgentRunActivationInputKind.USER_REQUEST,
                input_payload={
                    "source": "session_run_continue",
                    "session_run_id": session.session_run_id,
                    "branch_binding_id": binding.branch_binding_id,
                    "client_request_id": str(req.client_request_id or ""),
                    "locale": str(req.locale or ""),
                    "mentions": list(req.mentions),
                },
                resume_session=True,
                prompt=req.prompt,
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        except ValueError as exc:
            self._send_error(
                HTTPStatus.CONFLICT,
                "agent_run_not_continuable",
                str(exc) or "AgentRun is not continuable",
            )
            return
        if getattr(binding, "selected", False):
            writer.apply_selected_runtime_scope(
                activation_id=str(agent_run.current_activation_id or ""),
                reset_terminal=True,
            )
            writer.mark_running()
        writer.append_event(
            "session_run_continue",
            {
                "prompt": req.prompt,
                "agent_run_id": agent_run.id,
                "activation_id": agent_run.current_activation_id,
                "branch_binding_id": binding.branch_binding_id,
                "client_request_id": req.client_request_id,
                "locale": req.locale,
                "mentions": list(req.mentions),
            },
        )
        self._send_json(
            HTTPStatus.OK,
            SessionRunContinueResponse(
                ok=True,
                session_run_id=session.session_run_id,
                activation_id=str(agent_run.current_activation_id or ""),
                agent_run_id=agent_run.id,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
            ).to_dict(),
        )

    def _handle_chat_command(self) -> None:
        payload = self._read_json()
        try:
            req = ChatCommandDispatchRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_command_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.chat_command_handler is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "chat_command_unavailable")
            return
        if not req.command_text.startswith("/"):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_chat_command",
                "chat command dispatch requires slash command text",
            )
            return

        with self.service._get_peer_chat_lock(peer_id):
            result = self.service.chat_command_handler(peer_id, req)
        self._send_json(HTTPStatus.OK, result.to_dict())

    def _handle_session_run_events(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunEventsRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_events_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        self.service._project_agent_run_events_to_session_run(session, binding)
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )

        projected_during_snapshot = 0

        def runtime_snapshot() -> tuple[str, bool | None]:
            nonlocal projected_during_snapshot
            projected_during_snapshot += (
                self.service._project_agent_run_events_to_session_run(
                    session,
                    binding,
                )
            )
            agent_run = self.service.runtime_control_plane.get_agent_run(
                scope.agent_run_id
            )
            status = str(agent_run.status.value)
            return status, status.strip().lower() in {"failed", "cancelled"}

        self._send_sse_headers()
        cursor = int(req.cursor)
        wait_timeout = _sse_wait_timeout(req.timeout_sec)
        while True:
            try:
                projected_during_snapshot = 0
                batch_payload = writer.events_response_payload(
                    cursor,
                    wait_timeout,
                    runtime_snapshot=runtime_snapshot,
                    require_terminal_event_for_done=True,
                    selected=scope.selected,
                )
                if (
                    projected_during_snapshot
                    or (
                        not batch_payload.get("events")
                        and not bool(batch_payload.get("done"))
                    )
                ):
                    batch_payload = writer.events_response_payload(
                        cursor,
                        0,
                        runtime_snapshot=runtime_snapshot,
                        require_terminal_event_for_done=True,
                        selected=scope.selected,
                    )
                events = list(batch_payload.get("events") or [])
                done = bool(batch_payload.get("done"))
                if events or done:
                    self._write_sse_event(
                        "session_run",
                        SessionRunEventsBatch.from_dict(batch_payload).to_dict(),
                    )
                    cursor = int(batch_payload.get("next_cursor") or cursor)
                    if done:
                        self.close_connection = True
                        break
                    continue
                self._write_sse_comment("ping")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                break

    def _send_sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Request-ID", self._request_id())
        self.end_headers()

    def _write_sse_event(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_sse_comment(self, comment: str) -> None:
        self.wfile.write(f": {comment}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _handle_session_run_status(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunStatusRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_status_request",
            )
            return

        resolution = self._resolve_session_run_control_resolution(
            req.peer_token,
            req.session_run_id,
            SessionRunControlPolicy(
                branch_binding_id=req.branch_binding_id,
                require_branch_binding_id=True,
                include_inactive_binding=True,
            ),
        )
        if resolution is None:
            return
        session = resolution.session
        binding = resolution.binding
        scope = resolution.scope
        if session is None or binding is None or scope is None:
            self._send_error(HTTPStatus.CONFLICT, "session_run_scope_proof_invalid")
            return
        self.service._project_agent_run_events_to_session_run(session, binding)
        self._send_session_run_status_response(
            session,
            binding,
            req.cursor,
            scope,
            projection_state=resolution.projection_state,
        )

    def _handle_session_run_branch_select(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunBranchSelectRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_branch_select_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
            include_inactive_binding=True,
        )
        if control is None:
            return
        _peer_id, session, binding, _scope = control
        runtime = self.service.runtime_control_plane
        selector = getattr(runtime, "select_session_run_branch", None)
        if not callable(selector):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_bindings_unavailable",
            )
            return
        try:
            selected = selector(
                session_run_id=session.session_run_id,
                branch_binding_id=binding.branch_binding_id,
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "session_run_branch_binding_not_found")
            return
        except PermissionError:
            self._send_error(HTTPStatus.FORBIDDEN, "session_run_binding_peer_mismatch")
            return
        except Exception as exc:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_binding_store_unavailable",
                str(exc) or "SessionRun binding store is unavailable.",
            )
            return
        lister = getattr(runtime, "list_session_run_bindings", None)
        if callable(lister) and hasattr(session, "sync_branch_bindings"):
            try:
                session.sync_branch_bindings(
                    lister(session_run_id=session.session_run_id)
                )
            except Exception as exc:
                self._send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "session_run_binding_store_unavailable",
                    str(exc) or "SessionRun binding store is unavailable.",
                )
                return
        elif hasattr(session, "record_branch_binding"):
            session.record_branch_binding(selected)
        selected_scope = self._scope_proof_for(session, selected)
        if selected_scope is None:
            return
        selected_writer = session.scoped_writer(
            branch_binding_id=selected_scope.branch_binding_id,
            agent_run_id=selected_scope.agent_run_id,
        )
        selected_activation_id = ""
        selected_runtime_status = ""
        selected_terminal: bool | None = None
        try:
            selected_agent_run = self.service.runtime_control_plane.get_agent_run(
                selected_scope.agent_run_id
            )
            selected_activation_id = str(selected_agent_run.current_activation_id or "")
            selected_runtime_status = str(selected_agent_run.status.value)
            selected_terminal = selected_runtime_status.strip().lower() in {
                "failed",
                "cancelled",
            }
        except Exception:
            selected_terminal = None
        selected_writer.apply_selected_runtime_scope(
            activation_id=selected_activation_id,
            runtime_status=selected_runtime_status,
            terminal=selected_terminal,
        )
        self._send_session_run_status_response(session, selected, req.cursor, selected_scope)

    def _handle_session_run_cancel(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunCancelRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_cancel_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )

        reason = req.reason or "session_run_cancelled"
        first_request, resolved_approvals, resolved_user_inputs = session.request_branch_cancel(
            reason,
            binding.branch_binding_id,
        )
        ok = self.service.runtime_control_plane.cancel_agent_run(
            binding.agent_run_id,
            reason=reason,
        )
        if first_request:
            writer.append_event(
                "session_run_cancel_requested",
                {"reason": reason, "branch_binding_id": binding.branch_binding_id},
            )
            for event_payload in resolved_approvals:
                writer.append_event("approval_resolved", event_payload)
            for event_payload in resolved_user_inputs:
                writer.append_event("user_input_resolved", event_payload)
            writer.append_event(
                "session_run_cancelled",
                {"reason": reason, "branch_binding_id": binding.branch_binding_id},
            )
            if getattr(binding, "selected", False):
                writer.mark_done(reason)
        self._send_json(
            HTTPStatus.OK,
            SessionRunCancelResponse(
                ok=ok,
                session_run_id=session.session_run_id,
                agent_run_id=scope.agent_run_id,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
            ).to_dict(),
        )

    def _handle_session_run_stop(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunStopRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_stop_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )

        reason = req.reason or "user_stop"
        writer.append_event(
            "session_run_stop_requested",
            {"reason": reason, "branch_binding_id": binding.branch_binding_id},
        )
        ok = self.service.runtime_control_plane.stop_agent_run_activation(
            binding.agent_run_id,
            reason=reason,
        )
        if ok:
            writer.append_event(
                "session_run_stopped",
                {"reason": reason, "branch_binding_id": binding.branch_binding_id},
            )
            if getattr(binding, "selected", False):
                writer.mark_done()
        self._send_json(
            HTTPStatus.OK,
            SessionRunStopResponse(
                ok=ok,
                session_run_id=session.session_run_id,
                agent_run_id=scope.agent_run_id,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
                error=None if ok else "session_run_not_stoppable",
            ).to_dict(),
        )

    def _handle_session_run_recover(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunRecoverRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_recover_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
            include_inactive_binding=True,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        if getattr(binding, "selected", False):
            try:
                selected_agent_run = self.service.runtime_control_plane.get_agent_run(
                    binding.agent_run_id
                )
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
                return
            selected_status = str(selected_agent_run.status.value).strip().lower()
            selected_terminal = selected_status in {"failed", "cancelled"}
            if (
                not selected_terminal
                and selected_status in {"queued", "waiting", "dispatched", "running"}
            ):
                self._send_error(HTTPStatus.CONFLICT, "session_run_already_running")
                return
        try:
            prompt, ticket = session.consume_recovery(
                req.action,
                branch_binding_id=binding.branch_binding_id,
            )
        except ValueError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc) or "recovery_not_available")
            return
        writer.append_event(
            "session_run_recovery_start",
            {
                "recovery_id": ticket.get("recovery_id"),
                "action": ticket.get("action"),
                "branch_binding_id": binding.branch_binding_id,
            },
        )
        try:
            agent_run = self.service.runtime_control_plane.continue_agent_run(
                binding.agent_run_id,
                input_kind=AgentRunActivationInputKind.USER_REQUEST,
                input_payload={
                    "source": "session_run_recover",
                    "session_run_id": session.session_run_id,
                    "branch_binding_id": binding.branch_binding_id,
                    "recovery_id": str(ticket.get("recovery_id") or ""),
                    "action": str(ticket.get("action") or ""),
                },
                resume_session=True,
                prompt=prompt,
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        except ValueError as exc:
            self._send_error(
                HTTPStatus.CONFLICT,
                "agent_run_not_continuable",
                str(exc) or "AgentRun is not continuable",
            )
            return
        if getattr(binding, "selected", False):
            writer.apply_selected_runtime_scope(
                activation_id=str(agent_run.current_activation_id or ""),
                reset_terminal=True,
            )
            writer.mark_running()
        self._send_json(
            HTTPStatus.OK,
            SessionRunRecoverResponse(
                ok=True,
                session_run_id=session.session_run_id,
                agent_run_id=agent_run.id,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
                state=str(ticket.get("state") or "consumed"),
            ).to_dict(),
        )

    def _handle_approval_reply(self) -> None:
        payload = self._read_json()
        try:
            req = ApprovalReplyRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_approval_reply_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
            include_inactive_binding=True,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        if req.decision not in {"allow_once", "deny_once"}:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_approval_decision")
            return
        metadata = (
            {"approved_save_candidate": req.approved_save_candidate}
            if req.decision == "allow_once" and req.approved_save_candidate
            else None
        )
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        state = writer.resolve_approval(
            req.approval_id,
            req.decision,
            req.reason,
            metadata,
        )
        if state is None:
            self._send_error(HTTPStatus.NOT_FOUND, "approval_not_found")
            return
        self._send_json(
            HTTPStatus.OK,
            ApprovalReplyResponse(
                ok=True,
                state=state,
                session_run_id=session.session_run_id,
                agent_run_id=scope.agent_run_id,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
            ).to_dict(),
        )

    def _handle_session_run_user_input_reply(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunUserInputReplyRequest.from_dict(payload)
        except Exception as exc:
            self._send_invalid_session_run_request_error(
                exc,
                "invalid_session_run_user_input_reply_request",
            )
            return

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
            include_inactive_binding=True,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        if req.action not in {"accept", "decline", "cancel"}:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_user_input_action")
            return
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        state = writer.resolve_user_input(
            req.input_id,
            req.action,
            req.content,
            req.reason,
        )
        if state is None:
            self._send_error(HTTPStatus.NOT_FOUND, "user_input_not_found")
            return
        self._send_json(
            HTTPStatus.OK,
            SessionRunUserInputReplyResponse(
                ok=True,
                state=state,
                session_run_id=session.session_run_id,
                agent_run_id=scope.agent_run_id,
                branch_binding_id=scope.branch_binding_id,
                scope_id=scope.scope_id,
                selected=scope.selected,
            ).to_dict(),
        )
