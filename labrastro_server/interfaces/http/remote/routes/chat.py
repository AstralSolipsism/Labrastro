from __future__ import annotations

import gzip
import json
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
    ChatCancelRequest,
    ChatCancelResponse,
    ChatFollowUpCancelRequest,
    ChatFollowUpRequest,
    ChatFollowUpResponse,
    ChatRecoverRequest,
    ChatRecoverResponse,
    ChatRequest,
    ChatResponse,
    ChatStartRequest,
    ChatStartResponse,
    ChatStatusRequest,
    ChatStatusResponse,
    ChatEventsRequest,
    ChatEventsBatch,
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


def _chat_events_handler_error_payload(exc: Exception) -> dict[str, str]:
    message = str(exc).strip()
    code = getattr(exc, "code", None)
    protocol_message = getattr(exc, "message", None)
    if isinstance(code, str) and code.startswith("REMOTE_"):
        return {
            "message": str(protocol_message or message or code),
            "code": code,
        }
    if isinstance(exc, ValueError) and message.startswith("remote peer "):
        return {"message": message, "code": "chat_handler_failed"}
    return {"message": "chat_handler_failed", "code": "chat_handler_failed"}


def _sse_wait_timeout(timeout_sec: float) -> float:
    if timeout_sec <= 0:
        return 15.0
    return min(max(timeout_sec, 0.05), 30.0)


class RemoteChatRoutes:
    def _has_chat_model_context(self, peer_id: str, req: ChatStartRequest) -> bool:
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

    def _get_chat_control_session(self, peer_token: str, chat_id: str):
        control_peer_id = self.service.relay_server.token_manager.verify_peer_token(
            peer_token
        )
        if control_peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return None
        session = self.service._get_chat_session(chat_id)
        if session is None:
            self._send_error(HTTPStatus.NOT_FOUND, "chat_not_found")
            return None
        return control_peer_id, session

    def _handle_chat(self) -> None:
        payload = self._read_json()
        try:
            req = ChatRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return

        workflow_mode = (
            str(req.workflow_mode).strip().lower()
            if req.workflow_mode is not None
            else None
        )
        if workflow_mode == "taskflow":
            if self.service.chat_events_handler is None:
                self._send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "chat_events_unavailable",
                )
                return
            session = self.service._create_chat_session(
                peer_id,
                mode=req.mode,
                workflow_mode=workflow_mode,
                taskflow_id=req.taskflow_id,
                provider_id=req.provider_id,
                model_id=req.model_id,
                model_parameters=req.parameters,
                locale=getattr(req, "locale", None),
                initial_prompt=req.prompt,
            )
            session.append_event(
                "chat_start",
                {
                    "prompt": req.prompt,
                    "mode": req.mode,
                    "workflow_mode": workflow_mode,
                    "taskflow_id": req.taskflow_id,
                    "provider_id": req.provider_id,
                    "model_id": req.model_id,
                    "locale": getattr(req, "locale", None),
                },
            )
            session.mark_running()
            with self.service._get_peer_chat_lock(peer_id):
                try:
                    self.service.chat_events_handler(peer_id, req.prompt, session)
                except Exception as exc:
                    payload = _chat_events_handler_error_payload(exc)
                    session.append_event("error", payload)
                    session.append_event(
                        "chat_failed",
                        {**payload, "recoverable": False},
                    )
                finally:
                    session.mark_done()
            response_text = ""
            error_text = None
            for event in session.events:
                if event["type"] == "chat_end":
                    response_text = str(
                        event.get("payload", {}).get("response") or ""
                    )
                if event["type"] == "error":
                    error_text = str(
                        event.get("payload", {}).get("message") or "error"
                    )
            self._send_json(
                HTTPStatus.OK,
                ChatResponse(
                    response=response_text, error=error_text
                ).to_dict(),
            )
            return

        if self.service.chat_handler is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "chat_unavailable")
            return

        with self.service._get_peer_chat_lock(peer_id):
            try:
                response = self.service.chat_handler(peer_id, req.prompt)
            except Exception:
                response = ChatResponse(response="", error="chat_handler_failed")

        self._send_json(HTTPStatus.OK, response.to_dict())

    def _handle_chat_start(self) -> None:
        payload = self._read_json()
        try:
            req = ChatStartRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_start_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.chat_events_handler is None:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "chat_events_unavailable",
            )
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
                    "chat.start requires provider_id and model_id for a new chat session",
                )
                return
        existing = self.service._get_chat_session_by_request(
            peer_id,
            req.session_hint,
            req.client_request_id,
        )
        if existing is not None:
            self._send_json(
                HTTPStatus.OK,
                ChatStartResponse(
                    chat_id=existing.chat_id,
                    session_id=existing.session_id,
                ).to_dict(),
            )
            return
        session = self.service._create_chat_session(
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
            initial_prompt=req.prompt,
        )
        session.append_event(
            "chat_start",
            {
                "prompt": req.prompt,
                "mode": req.mode,
                "workflow_mode": workflow_mode,
                "taskflow_id": req.taskflow_id,
                "provider_id": req.provider_id,
                "model_id": req.model_id,
                "locale": req.locale,
            },
        )
        session.mark_running()

        def _run_chat() -> None:
            with self.service._get_peer_chat_lock(peer_id):
                try:
                    self.service.chat_events_handler(peer_id, req.prompt, session)
                except Exception as exc:
                    payload = _chat_events_handler_error_payload(exc)
                    session.append_event("error", payload)
                    session.append_event(
                        "chat_failed",
                        {**payload, "recoverable": False},
                    )
                finally:
                    session.mark_done()

        threading.Thread(target=_run_chat, daemon=True).start()
        self._send_json(
            HTTPStatus.OK,
            ChatStartResponse(
                chat_id=session.chat_id,
                session_id=session.session_id,
            ).to_dict(),
        )

    def _handle_chat_events(self) -> None:
        payload = self._read_json()
        try:
            req = ChatEventsRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_events_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        _control_peer_id, session = control

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
                        "chat",
                        ChatEventsBatch(
                            events=events, done=done, next_cursor=next_cursor
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

    def _handle_chat_status(self) -> None:
        payload = self._read_json()
        try:
            req = ChatStatusRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_status_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        _control_peer_id, session = control

        self._send_json(
            HTTPStatus.OK,
            ChatStatusResponse.from_dict(
                session.status_payload(req.cursor)
            ).to_dict(),
        )

    def _handle_chat_cancel(self) -> None:
        payload = self._read_json()
        try:
            req = ChatCancelRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_cancel_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        _control_peer_id, session = control

        reason = req.reason or "chat_cancelled"
        first_request = session.request_cancel(reason)
        if first_request:
            session.append_event(
                "chat_cancel_requested", {"reason": reason}
            )
            session.append_event("chat_cancelled", {"reason": reason})
            session.mark_done()
        self._send_json(
            HTTPStatus.OK, ChatCancelResponse(ok=True).to_dict()
        )

    def _handle_chat_follow_up(self) -> None:
        payload = self._read_json()
        try:
            req = ChatFollowUpRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_follow_up_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        _control_peer_id, session = control
        if session.done or not session.running:
            self._send_error(HTTPStatus.CONFLICT, "chat_not_running")
            return
        try:
            ticket = session.submit_follow_up(
                req.text,
                followup_id=req.followup_id,
                client_request_id=req.client_request_id,
            )
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "empty_follow_up")
            return
        self._send_json(
            HTTPStatus.OK,
            ChatFollowUpResponse(
                ok=True,
                followup_id=str(ticket.get("followup_id") or ""),
                state=str(ticket.get("state") or "pending"),
            ).to_dict(),
        )

    def _handle_chat_recover(self) -> None:
        payload = self._read_json()
        try:
            req = ChatRecoverRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_recover_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        peer_id, session = control
        if self.service.chat_events_handler is None:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "chat_events_unavailable",
            )
            return
        if session.running:
            self._send_error(HTTPStatus.CONFLICT, "chat_already_running")
            return
        try:
            prompt, ticket = session.consume_recovery(req.action)
        except ValueError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc) or "recovery_not_available")
            return
        session.append_event(
            "chat_recovery_start",
            {
                "recovery_id": ticket.get("recovery_id"),
                "action": ticket.get("action"),
            },
        )

        def _run_recovery() -> None:
            with self.service._get_peer_chat_lock(peer_id):
                try:
                    self.service.chat_events_handler(peer_id, prompt, session)
                except Exception as exc:
                    payload = _chat_events_handler_error_payload(exc)
                    session.append_event("error", payload)
                    session.append_event(
                        "chat_failed",
                        {**payload, "recoverable": False},
                    )
                finally:
                    session.mark_done()

        threading.Thread(target=_run_recovery, daemon=True).start()
        self._send_json(
            HTTPStatus.OK,
            ChatRecoverResponse(
                ok=True,
                chat_id=session.chat_id,
                state=str(ticket.get("state") or "consumed"),
            ).to_dict(),
        )

    def _handle_chat_follow_up_cancel(self) -> None:
        payload = self._read_json()
        try:
            req = ChatFollowUpCancelRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_follow_up_cancel_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        _control_peer_id, session = control
        if not req.followup_id:
            self._send_error(HTTPStatus.BAD_REQUEST, "missing_followup_id")
            return
        ok = session.cancel_follow_up(req.followup_id, req.reason)
        if not ok:
            self._send_error(HTTPStatus.NOT_FOUND, "follow_up_not_found")
            return
        self._send_json(
            HTTPStatus.OK,
            ChatFollowUpResponse(
                ok=True,
                followup_id=req.followup_id,
                state="cancelled",
            ).to_dict(),
        )

    def _handle_approval_reply(self) -> None:
        payload = self._read_json()
        try:
            req = ApprovalReplyRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_approval_reply_request")
            return

        control = self._get_chat_control_session(req.peer_token, req.chat_id)
        if control is None:
            return
        _control_peer_id, session = control
        ok = session.resolve_approval(req.approval_id, req.decision, req.reason)
        if not ok:
            self._send_error(HTTPStatus.NOT_FOUND, "approval_not_found")
            return
        self._send_json(HTTPStatus.OK, ApprovalReplyResponse(ok=True).to_dict())
