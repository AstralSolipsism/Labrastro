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

class RemoteChatRoutes:
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
            if self.service.stream_chat_handler is None:
                self._send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "chat_stream_unavailable",
                )
                return
            session = self.service._create_chat_session(
                peer_id,
                mode=req.mode,
                workflow_mode=workflow_mode,
                taskflow_goal_id=req.taskflow_goal_id,
            )
            session.append_event(
                "chat_start",
                {
                    "prompt": req.prompt,
                    "mode": req.mode,
                    "workflow_mode": workflow_mode,
                    "taskflow_goal_id": req.taskflow_goal_id,
                },
            )
            session.mark_running()
            with self.service._get_peer_chat_lock(peer_id):
                try:
                    self.service.stream_chat_handler(peer_id, req.prompt, session)
                except Exception:
                    session.append_event("error", {"message": "chat_handler_failed"})
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
        if self.service.stream_chat_handler is None:
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "chat_stream_unavailable",
            )
            return

        workflow_mode = (
            str(req.workflow_mode).strip().lower()
            if req.workflow_mode is not None
            else None
        )
        session = self.service._create_chat_session(
            peer_id,
            req.session_hint,
            mode=req.mode,
            workflow_mode=workflow_mode,
            taskflow_goal_id=req.taskflow_goal_id,
        )
        session.append_event(
            "chat_start",
            {
                "prompt": req.prompt,
                "mode": req.mode,
                "workflow_mode": workflow_mode,
                "taskflow_goal_id": req.taskflow_goal_id,
            },
        )
        session.mark_running()

        def _run_chat() -> None:
            with self.service._get_peer_chat_lock(peer_id):
                try:
                    self.service.stream_chat_handler(peer_id, req.prompt, session)
                except Exception:
                    session.append_event("error", {"message": "chat_handler_failed"})
                finally:
                    session.mark_done()

        threading.Thread(target=_run_chat, daemon=True).start()
        self._send_json(
            HTTPStatus.OK, ChatStartResponse(chat_id=session.chat_id).to_dict()
        )

    def _handle_chat_stream(self) -> None:
        payload = self._read_json()
        try:
            req = ChatStreamRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_stream_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        session = self.service._get_chat_session(req.chat_id)
        if session is None or session.peer_id != peer_id:
            self._send_error(HTTPStatus.NOT_FOUND, "chat_not_found")
            return

        events, done, next_cursor = session.wait_events(
            req.cursor, req.timeout_sec
        )
        self._send_json(
            HTTPStatus.OK,
            ChatStreamResponse(
                events=events, done=done, next_cursor=next_cursor
            ).to_dict(),
        )

    def _handle_chat_cancel(self) -> None:
        payload = self._read_json()
        try:
            req = ChatCancelRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_chat_cancel_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        session = self.service._get_chat_session(req.chat_id)
        if session is None or session.peer_id != peer_id:
            self._send_error(HTTPStatus.NOT_FOUND, "chat_not_found")
            return

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

    def _handle_approval_reply(self) -> None:
        payload = self._read_json()
        try:
            req = ApprovalReplyRequest.from_dict(payload)
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_approval_reply_request")
            return

        peer_id = self.service.relay_server.token_manager.verify_peer_token(
            req.peer_token
        )
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        session = self.service._get_chat_session(req.chat_id)
        if session is None or session.peer_id != peer_id:
            self._send_error(HTTPStatus.NOT_FOUND, "chat_not_found")
            return
        ok = session.resolve_approval(req.approval_id, req.decision, req.reason)
        if not ok:
            self._send_error(HTTPStatus.NOT_FOUND, "approval_not_found")
            return
        self._send_json(HTTPStatus.OK, ApprovalReplyResponse(ok=True).to_dict())


