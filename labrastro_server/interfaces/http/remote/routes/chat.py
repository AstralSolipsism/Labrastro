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
    ):
        resolution = self._session_run_control_resolver().resolve(
            peer_token,
            session_run_id,
            SessionRunControlPolicy(
                branch_binding_id=branch_binding_id,
                require_branch_binding_id=required,
            ),
        )
        if resolution.kind == "ok":
            return resolution.peer_id, resolution.session, resolution.binding, resolution.scope
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
        elif resolution.kind == "session_run_projection_unavailable":
            self._send_error(
                HTTPStatus.CONFLICT,
                "session_run_projection_unavailable",
                "SessionRun projection is unavailable while a persisted AgentRun binding remains.",
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
    ) -> None:
        agent_run = None
        try:
            agent_run = self.service.runtime_control_plane.get_agent_run(
                binding.agent_run_id
            )
        except Exception:
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        writer = session.scoped_writer(
            branch_binding_id=scope.branch_binding_id,
            agent_run_id=scope.agent_run_id,
        )
        run_status = str(agent_run.status.value)
        terminal = bool(getattr(agent_run, "is_terminal", False))
        status_payload = writer.status_response_payload(
            cursor,
            agent_run_status=run_status,
            activation_id=str(agent_run.current_activation_id or ""),
            terminal=terminal,
            selected=scope.selected,
        )

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
            terminal=bool(getattr(agent_run, "is_terminal", False)),
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
            return str(agent_run.status.value), bool(
                getattr(agent_run, "is_terminal", False)
            )

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

        control = self._resolve_session_run_control(
            req.peer_token,
            req.session_run_id,
            req.branch_binding_id,
        )
        if control is None:
            return
        _peer_id, session, binding, scope = control
        self.service._project_agent_run_events_to_session_run(session, binding)
        self._send_session_run_status_response(session, binding, req.cursor, scope)

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
            selected_terminal = bool(getattr(selected_agent_run, "is_terminal", False))
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
            selected_terminal = bool(getattr(selected_agent_run, "is_terminal", False))
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
