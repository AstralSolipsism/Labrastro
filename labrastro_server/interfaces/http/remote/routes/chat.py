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

    def _get_session_run_control(self, peer_token: str, session_run_id: str):
        control_peer_id = self.service.relay_server.token_manager.verify_peer_token(
            peer_token
        )
        if control_peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return None
        session = self.service._get_session_run(session_run_id)
        if session is None:
            self._send_error(HTTPStatus.NOT_FOUND, "session_run_not_found")
            return None
        return control_peer_id, session

    def _selected_session_run_binding(self, session, peer_id: str | None = None):
        runtime = self.service.runtime_control_plane
        if runtime is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return None
        finder = getattr(runtime, "find_session_run_binding", None)
        if not callable(finder):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_bindings_unavailable",
            )
            return None
        binding = finder(session_run_id=session.session_run_id)
        if binding is None:
            self._send_error(HTTPStatus.CONFLICT, "session_run_binding_not_found")
            return None
        if peer_id is not None and not self._session_binding_peer_matches(binding, peer_id):
            self._send_error(HTTPStatus.FORBIDDEN, "session_run_binding_peer_mismatch")
            return None
        lister = getattr(runtime, "list_session_run_bindings", None)
        if callable(lister) and hasattr(session, "sync_branch_bindings"):
            session.sync_branch_bindings(
                lister(session_run_id=session.session_run_id)
            )
        elif hasattr(session, "record_branch_binding"):
            session.record_branch_binding(binding)
        return binding

    def _send_session_run_status_response(self, session, binding, cursor: int) -> None:
        agent_run = None
        try:
            agent_run = self.service.runtime_control_plane.get_agent_run(
                binding.agent_run_id
            )
        except Exception:
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        status_payload = session.status_payload(cursor)
        run_status = str(agent_run.status.value)
        terminal = bool(getattr(agent_run, "is_terminal", False))
        runtime_state = (
            dict(status_payload.get("runtime_state"))
            if isinstance(status_payload.get("runtime_state"), dict)
            else {}
        )
        runtime_state.update(
            {
                "agent_run_id": agent_run.id,
                "activation_id": str(agent_run.current_activation_id or ""),
                "branch_binding_id": binding.branch_binding_id,
            }
        )
        status_payload.update(
            {
                "status": run_status,
                "running": run_status in {"queued", "dispatched", "running"},
                "done": terminal,
                "reconnectable": not terminal,
                "agent_run_id": agent_run.id,
                "branch_binding_id": binding.branch_binding_id,
                "runtime_state": runtime_state,
            }
        )

        self._send_json(
            HTTPStatus.OK,
            SessionRunStatusResponse.from_dict(
                status_payload
            ).to_dict(),
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
            activation_id = ""
            if existing.agent_run_id:
                try:
                    activation_id = str(
                        self.service.runtime_control_plane.get_agent_run(
                            existing.agent_run_id
                        ).current_activation_id
                        or ""
                    )
                except Exception:
                    activation_id = ""
            self._send_json(
                HTTPStatus.OK,
                SessionRunStartResponse(
                    session_run_id=existing.session_run_id,
                    session_id=existing.session_id,
                    agent_run_id=existing.agent_run_id,
                    activation_id=activation_id or None,
                    branch_binding_id=existing.branch_binding_id,
                    agent_id=existing.agent_id,
                    workflow_mode=existing.workflow_mode,
                    runtime_state=dict(existing.runtime_state),
                ).to_dict(),
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
            model_parameters=req.parameters,
            locale=req.locale,
            mentions=req.mentions,
            initial_prompt=req.prompt,
        )
        agent_id = str(req.taskflow_id or req.mode or "chat").strip() or "chat"
        metadata: dict[str, Any] = {
            "remote_peer_id": peer_id,
            "session_hint": str(req.session_hint or ""),
        }
        if workflow_mode:
            metadata["workflow_mode"] = workflow_mode
        if req.mode:
            metadata["mode"] = str(req.mode)
        try:
            agent_run = self.service.runtime_control_plane.submit_agent_run(
                AgentRunRequest(
                    agent_id=agent_id,
                    prompt=req.prompt,
                    owner_session_run_id=session.session_run_id,
                    source="chat",
                    trigger_mode="interactive_chat",
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
        session.agent_run_id = agent_run.id
        session.branch_binding_id = binding.branch_binding_id
        session.record_branch_binding(binding)
        session.runtime_state.update(
            {
                "agent_run_id": agent_run.id,
                "activation_id": str(agent_run.current_activation_id or ""),
                "branch_binding_id": binding.branch_binding_id,
            }
        )
        session.append_event(
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
        session.append_event(
            "session_run_binding_selected",
            {
                "agent_run_id": agent_run.id,
                "branch_binding_id": binding.branch_binding_id,
                "binding_id": binding.id,
            },
        )
        session.mark_running()
        self._send_json(
            HTTPStatus.OK,
            SessionRunStartResponse(
                session_run_id=session.session_run_id,
                session_id=session.session_id,
                agent_run_id=agent_run.id,
                activation_id=str(agent_run.current_activation_id or "") or None,
                branch_binding_id=binding.branch_binding_id,
                agent_id=agent_run.agent_id,
                workflow_mode=workflow_mode,
                runtime_state=dict(session.runtime_state),
            ).to_dict(),
        )

    def _handle_session_run_continue(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunContinueRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_continue_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        peer_id, session = control
        binding = self._selected_session_run_binding(session, peer_id)
        if binding is None:
            return
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
        session.done = False
        session.finished_at = None
        session.mark_running()
        session.agent_run_id = agent_run.id
        session.branch_binding_id = binding.branch_binding_id
        session.runtime_state.update(
            {
                "agent_run_id": agent_run.id,
                "activation_id": str(agent_run.current_activation_id or ""),
                "branch_binding_id": binding.branch_binding_id,
            }
        )
        session.append_event(
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
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_events_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        control_peer_id, session = control
        binding = self._selected_session_run_binding(session, control_peer_id)
        if binding is None:
            return

        self._send_sse_headers()
        cursor = int(req.cursor)
        wait_timeout = _sse_wait_timeout(req.timeout_sec)
        while True:
            try:
                events, done, next_cursor = session.wait_events(
                    cursor, wait_timeout
                )
                if events or done:
                    self._write_sse_event(
                        "session_run",
                        SessionRunEventsBatch(
                            events=events,
                            done=done,
                            next_cursor=next_cursor,
                            branches=session.branch_summaries(cursor),
                        ).to_dict(),
                    )
                    cursor = next_cursor
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
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_status_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        control_peer_id, session = control
        binding = self._selected_session_run_binding(session, control_peer_id)
        if binding is None:
            return
        self._send_session_run_status_response(session, binding, req.cursor)

    def _handle_session_run_branch_select(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunBranchSelectRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_branch_select_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        control_peer_id, session = control
        runtime = self.service.runtime_control_plane
        if runtime is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        finder = getattr(runtime, "find_session_run_binding", None)
        selector = getattr(runtime, "select_session_run_branch", None)
        if not callable(finder) or not callable(selector):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_bindings_unavailable",
            )
            return
        binding = finder(
            session_run_id=session.session_run_id,
            branch_binding_id=req.branch_binding_id,
            selected_only=False,
        )
        if binding is None:
            self._send_error(HTTPStatus.NOT_FOUND, "session_run_branch_binding_not_found")
            return
        if not self._session_binding_peer_matches(binding, control_peer_id):
            self._send_error(HTTPStatus.FORBIDDEN, "session_run_binding_peer_mismatch")
            return
        try:
            selected = selector(
                session_run_id=session.session_run_id,
                branch_binding_id=req.branch_binding_id,
            )
        except Exception:
            self._send_error(HTTPStatus.NOT_FOUND, "session_run_branch_binding_not_found")
            return
        session.branch_binding_id = selected.branch_binding_id
        lister = getattr(runtime, "list_session_run_bindings", None)
        if callable(lister) and hasattr(session, "sync_branch_bindings"):
            session.sync_branch_bindings(
                lister(session_run_id=session.session_run_id)
            )
        elif hasattr(session, "record_branch_binding"):
            session.record_branch_binding(selected)
        self._send_session_run_status_response(session, selected, req.cursor)

    def _handle_session_run_cancel(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunCancelRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_cancel_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        control_peer_id, session = control
        binding = self._selected_session_run_binding(session, control_peer_id)
        if binding is None:
            return

        reason = req.reason or "session_run_cancelled"
        first_request, resolved_approvals = session.request_cancel(
            reason,
            branch_binding_id=binding.branch_binding_id,
        )
        ok = self.service.runtime_control_plane.cancel_agent_run(
            binding.agent_run_id,
            reason=reason,
        )
        if first_request:
            session.append_event(
                "session_run_cancel_requested", {"reason": reason}
            )
            for event_payload in resolved_approvals:
                session.append_event("approval_resolved", event_payload)
            session.append_event("session_run_cancelled", {"reason": reason})
            session.mark_done(reason)
        self._send_json(
            HTTPStatus.OK, SessionRunCancelResponse(ok=ok).to_dict()
        )

    def _handle_session_run_recover(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunRecoverRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_recover_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        peer_id, session = control
        binding = self._selected_session_run_binding(session, peer_id)
        if binding is None:
            return
        if session.running:
            self._send_error(HTTPStatus.CONFLICT, "session_run_already_running")
            return
        try:
            prompt, ticket = session.consume_recovery(req.action)
        except ValueError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc) or "recovery_not_available")
            return
        session.append_event(
            "session_run_recovery_start",
            {
                "recovery_id": ticket.get("recovery_id"),
                "action": ticket.get("action"),
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
        session.done = False
        session.finished_at = None
        session.agent_run_id = agent_run.id
        session.branch_binding_id = binding.branch_binding_id
        session.runtime_state.update(
            {
                "agent_run_id": agent_run.id,
                "activation_id": str(agent_run.current_activation_id or ""),
                "branch_binding_id": binding.branch_binding_id,
            }
        )
        session.mark_running()
        self._send_json(
            HTTPStatus.OK,
            SessionRunRecoverResponse(
                ok=True,
                session_run_id=session.session_run_id,
                state=str(ticket.get("state") or "consumed"),
            ).to_dict(),
        )

    def _handle_approval_reply(self) -> None:
        payload = self._read_json()
        try:
            req = ApprovalReplyRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_approval_reply_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        control_peer_id, session = control
        binding = self._selected_session_run_binding(session, control_peer_id)
        if binding is None:
            return
        if req.decision not in {"allow_once", "deny_once"}:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_approval_decision")
            return
        metadata = (
            {"approved_save_candidate": req.approved_save_candidate}
            if req.decision == "allow_once" and req.approved_save_candidate
            else None
        )
        state = session.resolve_approval(
            req.approval_id,
            req.decision,
            req.reason,
            metadata,
            branch_binding_id=binding.branch_binding_id,
        )
        if state is None:
            self._send_error(HTTPStatus.NOT_FOUND, "approval_not_found")
            return
        self._send_json(
            HTTPStatus.OK,
            ApprovalReplyResponse(ok=True, state=state).to_dict(),
        )

    def _handle_session_run_user_input_reply(self) -> None:
        payload = self._read_json()
        try:
            req = SessionRunUserInputReplyRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_session_run_user_input_reply_request")
            return

        control = self._get_session_run_control(req.peer_token, req.session_run_id)
        if control is None:
            return
        control_peer_id, session = control
        binding = self._selected_session_run_binding(session, control_peer_id)
        if binding is None:
            return
        if req.action not in {"accept", "decline", "cancel"}:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_user_input_action")
            return
        state = session.resolve_user_input(
            req.input_id,
            req.action,
            req.content,
            req.reason,
            branch_binding_id=binding.branch_binding_id,
        )
        if state is None:
            self._send_error(HTTPStatus.NOT_FOUND, "user_input_not_found")
            return
        self._send_json(
            HTTPStatus.OK,
            SessionRunUserInputReplyResponse(ok=True, state=state).to_dict(),
        )
