from __future__ import annotations

import gzip
import json
import queue
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
    SessionRunControlScopeProof,
)
from labrastro_server.interfaces.http.remote.protocol import (
    AgentRunSteerResponse,
    SessionRunAgentRunSteerRequest,
    ApprovalReplyRequest,
    ApprovalReplyResponse,
    SessionRunCancelRequest,
    SessionRunCancelResponse,
    SessionRunStartRequest,
    SessionRunStartResponse,
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
from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.model_bridge import (
    AgentRunModelBridge,
    AgentRunModelBridgeError,
    provider_response_to_dict,
)
from labrastro_server.services.agent_runtime.runtime_store import clamp_event_limit
from reuleauxcoder.domain.agent_runtime.models import (
    AgentRunStatus,
    ExecutorType,
    WorkerKind,
)
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.services.providers.stream_supervisor import ProviderStreamInterruptedError


MODEL_REQUEST_SSE_HEARTBEAT_SEC = 5.0
MODEL_REQUEST_SSE_QUEUE_MAX_SIZE = 256


def _agent_run_model_interrupted_payload(
    exc: ProviderStreamInterruptedError,
) -> dict[str, Any]:
    diagnostic = exc.original_error if exc.original_error is not None else exc
    partial = provider_response_to_dict(exc.partial_response)
    return {
        **partial,
        "error": "provider_stream_interrupted",
        "message": "Provider stream interrupted.",
        "message_key": "provider_stream_interrupted.recovering",
        "notice_code": "provider_stream_interrupted",
        "interruption": dict(exc.interruption),
        "stream_status": "interrupted",
        "diagnostic_error_type": type(diagnostic).__name__,
        "diagnostic_message": str(diagnostic),
    }


def _agent_run_model_error_payload(exc: AgentRunModelBridgeError) -> dict[str, Any]:
    payload = dict(exc.details or {})
    payload.update({"error": exc.code, "message": exc.message})
    if exc.details:
        payload["details"] = dict(exc.details)
    return payload


def _agent_run_model_unhandled_error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "error": "provider_request_failed",
        "message": "Provider request failed.",
        "diagnostic_error_type": type(exc).__name__,
        "diagnostic_message": str(exc),
    }


def _required_scope_error(exc: Exception) -> str:
    if not isinstance(exc, ValueError):
        return ""
    code = str(exc).strip()
    if code in {"branch_binding_id_required", "session_run_id_required"}:
        return code
    return ""


def _activation_steer_response_payload(steer: Any) -> dict[str, Any]:
    return {
        "id": steer.id,
        "activation_id": steer.activation_id,
        "source": steer.source.value,
        "payload": dict(steer.payload),
        "created_at": steer.created_at,
        "delivered_at": steer.delivered_at,
        "status": steer.status.value,
        "metadata": dict(steer.metadata),
    }


def _canonical_steer_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _find_existing_activation_steer(
    control_plane: Any,
    agent_run_id: str,
    *,
    activation_id: str,
    sender: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        detail = control_plane.load_agent_run_detail(agent_run_id)
    except KeyError:
        return None
    target_payload = _canonical_steer_payload(payload)
    for steer in detail.get("activation_steers", []):
        if not isinstance(steer, dict):
            continue
        metadata = steer.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        if str(steer.get("activation_id") or "") != activation_id:
            continue
        if str(metadata.get("sender") or "") != sender:
            continue
        if str(metadata.get("idempotency_key") or "") != idempotency_key:
            continue
        if _canonical_steer_payload(steer.get("payload", {})) != target_payload:
            continue
        return dict(steer)
    return None


class RemoteAgentRunRoutes:
    def _send_agent_run_model_sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Request-ID", self._request_id())
        self.end_headers()

    def _handle_agent_run_events_get(self, parsed: Any) -> None:
        peer_id = self._verify_query_peer(parsed)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        parts = [
            unquote(part)
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        if (
            len(parts) != 4
            or parts[:2] != ["remote", "agent-runs"]
            or parts[3] != "events"
        ):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        task_id = parts[2]
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

    def _handle_agent_run_steer(self, parsed: Any) -> None:
        parts = [
            unquote(part)
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        if (
            len(parts) != 4
            or parts[:2] != ["remote", "agent-runs"]
            or parts[3] != "steer"
        ):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        payload = self._read_json()
        payload["agent_run_id"] = parts[2]
        try:
            req = SessionRunAgentRunSteerRequest.from_dict(payload)
        except Exception as exc:
            scope_error = _required_scope_error(exc)
            if scope_error:
                self._send_error(HTTPStatus.BAD_REQUEST, scope_error)
            else:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_agent_run_steer_request")
            return
        peer_id = self._verify_peer_token(req.peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        if not req.session_run_id:
            self._send_error(HTTPStatus.BAD_REQUEST, "session_run_id_required")
            return
        if not req.branch_binding_id:
            self._send_error(HTTPStatus.BAD_REQUEST, "branch_binding_id_required")
            return
        finder = getattr(self.service.runtime_control_plane, "find_session_run_binding", None)
        if not callable(finder):
            self._send_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "session_run_bindings_unavailable",
            )
            return
        binding = finder(
            session_run_id=req.session_run_id,
            branch_binding_id=req.branch_binding_id,
            selected_only=False,
        )
        if binding is None:
            self._send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        scope = SessionRunControlScopeProof.from_binding(
            session_run_id=req.session_run_id,
            binding=binding,
        )
        if scope is None:
            self._send_error(HTTPStatus.CONFLICT, "session_run_scope_proof_invalid")
            return
        if not self._session_binding_peer_matches(binding, peer_id):
            self._send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if binding.agent_run_id != req.agent_run_id:
            self._send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        try:
            task = self.service.runtime_control_plane.get_agent_run(req.agent_run_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        current_activation_id = str(task.current_activation_id or "")
        if not current_activation_id or task.status.value not in {
            "queued",
            "dispatched",
            "running",
        }:
            self._send_error(HTTPStatus.CONFLICT, "agent_run_not_steerable")
            return
        if req.activation_id and req.activation_id != current_activation_id:
            self._send_error(HTTPStatus.CONFLICT, "activation_mismatch")
            return
        idempotency_key = (
            str(req.idempotency_key or "").strip()
            or str(req.client_steer_id or "").strip()
        )
        if not idempotency_key:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "activation_steer_idempotency_key_required",
            )
            return
        metadata = dict(req.metadata)
        metadata.update(
            {
                "idempotency_key": idempotency_key,
                "sender": f"peer:{peer_id}",
                "peer_id": peer_id,
                "session_run_id": binding.session_run_id,
                "branch_binding_id": binding.branch_binding_id,
                "scope_id": scope.scope_id,
                "selected": scope.selected,
                "expected_activation_id": req.activation_id or current_activation_id,
            }
        )
        if req.client_steer_id:
            metadata["client_steer_id"] = req.client_steer_id
        duplicate = _find_existing_activation_steer(
            self.service.runtime_control_plane,
            req.agent_run_id,
            activation_id=current_activation_id,
            sender=f"peer:{peer_id}",
            idempotency_key=idempotency_key,
            payload=req.payload,
        )
        try:
            steer = self.service.runtime_control_plane.append_activation_steer(
                req.agent_run_id,
                source="user",
                payload=req.payload,
                metadata=metadata,
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        except ValueError as exc:
            message = str(exc)
            if "only active AgentRun activations" in message or "active worker claim" in message:
                self._send_error(HTTPStatus.CONFLICT, "agent_run_not_steerable")
                return
            if "idempotency_conflict" in message:
                self._send_error(HTTPStatus.CONFLICT, "activation_steer_idempotency_conflict")
                return
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_agent_run_steer",
                message or "invalid AgentRun steer",
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(
            HTTPStatus.OK,
            AgentRunSteerResponse(
                ok=True,
                status=(
                    "duplicate"
                    if duplicate is not None and duplicate.get("id") == steer.id
                    else "accepted"
                ),
                activation_steer=_activation_steer_response_payload(steer),
            ).to_dict(),
        )

    def _handle_agent_run_activation_claim(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        raw_executors = payload.get("executors", [])
        executors = raw_executors if isinstance(raw_executors, list) else []
        try:
            executor_values = [
                ExecutorType(str(executor)).value for executor in executors
            ]
        except ValueError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_executor",
                "executor must be one of reuleauxcoder, fake, codex, claude, gemini",
            )
            return
        worker_id = str(payload.get("worker_id") or peer_id)
        peer = self.service.relay_server.registry.get(peer_id)
        peer_features = list(peer.features) if peer is not None else []
        workspace_root = peer.workspace_root if peer is not None else None
        worker_kind = str(payload.get("worker_kind") or "").strip()
        if worker_kind:
            try:
                WorkerKind(worker_kind)
            except ValueError:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_worker_kind",
                    "worker_kind must be one of local_peer, server_worker, sandbox_worker",
                )
                return
            if f"worker_kind:{worker_kind}" not in set(peer_features):
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "worker_kind_not_registered",
                    "worker_kind is not registered for this peer",
                )
                return
        claim = self.service.runtime_control_plane.claim_agent_run_activation(
            worker_id=worker_id,
            worker_kind=worker_kind,
            executors=executor_values,
            peer_id=peer_id,
            peer_features=peer_features,
            workspace_root=workspace_root,
            wait_sec=float(payload.get("wait_sec") or 0),
        )
        self._send_json(
            HTTPStatus.OK,
            {"claim": claim.to_dict() if claim is not None else None},
        )

    def _handle_agent_run_activation_heartbeat(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        request_id = str(payload.get("request_id") or "")
        task_id = str(payload.get("agent_run_id") or "")
        activation_id = str(payload.get("activation_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not request_id or not task_id or not activation_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_agent_run_id_activation_id_and_worker_id_required",
            )
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        response = self.service.runtime_control_plane.heartbeat_agent_run_activation(
            request_id=request_id,
            task_id=task_id,
            activation_id=activation_id,
            worker_id=worker_id,
            peer_id=peer_id,
            lease_sec=(
                int(payload["lease_sec"])
                if payload.get("lease_sec") is not None
                else None
            ),
            delivered_steer_ids=[
                str(item)
                for item in (
                    payload.get("delivered_steer_ids")
                    if isinstance(payload.get("delivered_steer_ids"), list)
                    else []
                )
                if str(item).strip()
            ],
        )
        self._send_json(HTTPStatus.OK, response)

    def _handle_agent_run_activation_session(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        request_id = str(payload.get("request_id") or "")
        task_id = str(payload.get("agent_run_id") or "")
        activation_id = str(payload.get("activation_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not request_id or not task_id or not activation_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_agent_run_id_activation_id_and_worker_id_required",
            )
            return
        metadata: dict[str, Any] = {}
        for key in ("repo_url", "cache_path"):
            if payload.get(key) is not None:
                metadata[key] = str(payload[key])
        try:
            ok, reason = self.service.runtime_control_plane.pin_claimed_activation_session(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
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
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        if not ok:
            status = (
                HTTPStatus.NOT_FOUND
                if reason == "agent_run_not_found"
                else HTTPStatus.FORBIDDEN
            )
            self._send_error(status, reason or "claim_owner_mismatch")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_agent_run_activation_event(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        task_id = str(payload.get("agent_run_id") or "")
        request_id = str(payload.get("request_id") or "")
        activation_id = str(payload.get("activation_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        event_type = str(payload.get("type") or "")
        if not task_id or not event_type or not request_id or not activation_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_agent_run_id_activation_id_worker_id_and_type_required",
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
            activation_id=activation_id,
            worker_id=worker_id,
            peer_id=peer_id,
        )
        if not ok:
            status = (
                HTTPStatus.NOT_FOUND
                if reason == "agent_run_not_found"
                else HTTPStatus.FORBIDDEN
            )
            self._send_error(status, reason or "claim_owner_mismatch")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_agent_run_activation_model_request(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        bridge = AgentRunModelBridge(
            runtime_control_plane=self.service.runtime_control_plane,
            admin_manager=self.service.admin_manager,
        )
        try:
            prepared = bridge.prepare(payload, peer_id=peer_id)
        except AgentRunModelBridgeError as exc:
            self._send_error(exc.status, exc.code, exc.message, exc.details or {})
            return
        self.service.runtime_control_plane.append_executor_event(
            str(prepared.metadata.get("agent_run_id") or ""),
            ExecutorEvent.status(
                "model_request_started",
                provider_id=prepared.provider_config.id,
                model=prepared.provider_model,
            ),
            request_id=str(prepared.metadata.get("request_id") or ""),
            activation_id=str(prepared.metadata.get("activation_id") or ""),
            worker_id=str(prepared.metadata.get("worker_id") or ""),
            peer_id=peer_id,
        )
        self.service.relay_server.registry.update_heartbeat(peer_id)
        if payload.get("stream") is False:
            try:
                response = bridge.execute(prepared)
            except ProviderStreamInterruptedError as exc:
                interrupted = _agent_run_model_interrupted_payload(exc)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": False,
                        **interrupted,
                        "response": provider_response_to_dict(exc.partial_response),
                    },
                )
                return
            except AgentRunModelBridgeError as exc:
                self._send_error(exc.status, exc.code, exc.message, exc.details or {})
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "response": provider_response_to_dict(response)},
            )
            return
        self._send_agent_run_model_sse_headers()

        stop_event = threading.Event()
        sse_events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(
            maxsize=MODEL_REQUEST_SSE_QUEUE_MAX_SIZE
        )

        def enqueue_event(event: str, data: dict[str, Any]) -> None:
            if stop_event.is_set():
                raise BrokenPipeError("model request stream disconnected")
            try:
                sse_events.put((event, data), timeout=0.1)
            except queue.Full as exc:
                stop_event.set()
                raise BrokenPipeError("model request stream queue full") from exc

        def enqueue_terminal_event(event: str, data: dict[str, Any]) -> None:
            if stop_event.is_set():
                return
            try:
                enqueue_event(event, data)
            except (BrokenPipeError, ConnectionResetError):
                stop_event.set()

        def run_bridge() -> None:
            try:
                response = bridge.execute(
                    prepared,
                    on_token=lambda text: enqueue_event("token", {"text": text}),
                    on_reasoning_token=lambda text: enqueue_event(
                        "reasoning_token", {"text": text}
                    ),
                    on_tool_call_delta=lambda delta: enqueue_event(
                        "tool_call_delta", dict(delta)
                    ),
                )
                enqueue_terminal_event("done", provider_response_to_dict(response))
            except (BrokenPipeError, ConnectionResetError):
                stop_event.set()
            except ProviderStreamInterruptedError as exc:
                enqueue_terminal_event(
                    "interrupted",
                    _agent_run_model_interrupted_payload(exc),
                )
            except AgentRunModelBridgeError as exc:
                enqueue_terminal_event(
                    "error",
                    _agent_run_model_error_payload(exc),
                )
            except Exception as exc:  # pragma: no cover - defensive bridge boundary
                enqueue_terminal_event(
                    "error",
                    _agent_run_model_unhandled_error_payload(exc),
                )

        threading.Thread(
            target=run_bridge,
            name="agent-run-activation-model-request",
            daemon=True,
        ).start()
        try:
            while True:
                try:
                    event, data = sse_events.get(timeout=MODEL_REQUEST_SSE_HEARTBEAT_SEC)
                except queue.Empty:
                    try:
                        self._write_sse_event("heartbeat", {"status": "alive"})
                    except (BrokenPipeError, ConnectionResetError):
                        stop_event.set()
                        self.close_connection = True
                        break
                    continue
                try:
                    self._write_sse_event(event, data)
                except (BrokenPipeError, ConnectionResetError):
                    stop_event.set()
                    self.close_connection = True
                    break
                if event in {"done", "error", "interrupted"}:
                    stop_event.set()
                    self.close_connection = True
                    break
        finally:
            stop_event.set()

    def _handle_agent_run_activation_complete(self) -> None:
        payload = self._read_json()
        peer_token = payload.get("peer_token")
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if self.service.runtime_control_plane is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "agent_runs_unavailable")
            return
        task_id = str(payload.get("agent_run_id") or "")
        request_id = str(payload.get("request_id") or "")
        activation_id = str(payload.get("activation_id") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not task_id or not request_id or not activation_id or not worker_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request_id_agent_run_id_activation_id_and_worker_id_required",
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
            ok, reason, completed = self.service.runtime_control_plane.complete_claimed_agent_run_activation(
                task_id,
                result,
                request_id=request_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
                artifacts=[
                    artifact
                    for artifact in artifacts
                    if isinstance(artifact, dict)
                ],
            )
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "agent_run_not_found")
            return
        if not ok:
            status = (
                HTTPStatus.NOT_FOUND
                if reason == "agent_run_not_found"
                else HTTPStatus.FORBIDDEN
            )
            self._send_error(status, reason or "claim_owner_mismatch")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        github: dict[str, Any] | None = None
        github_pr_service = getattr(self.service, "github_pr_service", None)
        if (
            completed is not None
            and completed.status == AgentRunStatus.COMPLETED
            and github_pr_service is not None
        ):
            github = github_pr_service.ensure_pr_for_task(task_id).to_dict()
        response: dict[str, Any] = {"ok": True}
        if github is not None:
            response["github"] = github
        self._send_json(HTTPStatus.OK, response)
