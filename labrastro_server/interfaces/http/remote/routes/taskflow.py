from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from labrastro_server.taskflow.domain.taskflow_state import (
    Assumption,
    ReadinessGate,
    WorkItemCandidate,
)


def _payload_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _payload_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _assert_taskflow_peer(state: Any, peer_id: str) -> None:
    context = state.compiler.traceability_index.get("request_context")
    owner = context.get("peer_id") if isinstance(context, dict) else None
    if owner and owner != peer_id:
        raise PermissionError("taskflow belongs to another peer")


class RemoteTaskflowRoutes:
    def _handle_taskflow_get(self, parsed: Any) -> None:
        peer_id = self._verify_query_peer(parsed)
        if peer_id is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        parts = [
            unquote(part)
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        try:
            if len(parts) == 4 and parts[:3] == ["remote", "taskflow", "taskflows"]:
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "taskflow": state.to_dict()},
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "review-cards"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                cards = self.service.taskflow_service.render_review_cards(parts[3])
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "review_cards": [card.to_dict() for card in cards]},
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
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        parts = [
            unquote(part)
            for part in path.strip("/").split("/")
            if part
        ]
        try:
            if parts == ["remote", "taskflow", "taskflows"]:
                state = self.service.taskflow_service.start_taskflow(
                    project_id=str(payload.get("project_id") or ""),
                    raw_goal=str(payload.get("raw_goal") or payload.get("goal") or ""),
                    session_id=(
                        str(payload["session_id"])
                        if payload.get("session_id") is not None
                        else None
                    ),
                    peer_id=peer_id,
                    metadata=_payload_dict(payload.get("metadata")),
                    taskflow_id=(
                        str(payload["taskflow_id"])
                        if payload.get("taskflow_id") is not None
                        else None
                    ),
                    goal_id=(
                        str(payload["goal_id"])
                        if payload.get("goal_id") is not None
                        else None
                    ),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "taskflow": state.to_dict()},
                )
                return
            if len(parts) == 5 and parts[:3] == ["remote", "taskflow", "taskflows"]:
                taskflow_id = parts[3]
                action = parts[4]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                if action == "clarify":
                    state = self.service.taskflow_service.clarify_goal(
                        taskflow_id,
                        goal_statement=payload.get("goal_statement"),
                        background_delta=payload.get("background_delta"),
                        scope_in=_payload_list(payload.get("scope_in")),
                        scope_out=_payload_list(payload.get("scope_out")),
                        deferred_scope=_payload_list(payload.get("deferred_scope")),
                        success_criteria=_payload_list(
                            payload.get("success_criteria")
                        ),
                        assumptions=[
                            Assumption.from_dict(_payload_dict(item))
                            for item in _payload_list(payload.get("assumptions"))
                        ],
                        work_item_candidates=[
                            WorkItemCandidate.from_dict(_payload_dict(item))
                            for item in _payload_list(
                                payload.get("work_item_candidates")
                            )
                        ],
                        readiness_gates=[
                            ReadinessGate.from_dict(_payload_dict(item))
                            for item in _payload_list(payload.get("readiness_gates"))
                        ],
                        readiness_score=(
                            int(payload["readiness_score"])
                            if payload.get("readiness_score") is not None
                            else None
                        ),
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "taskflow": state.to_dict()},
                    )
                    return
                if action == "confirm":
                    state = self.service.taskflow_service.confirm_goal(
                        taskflow_id,
                        confirmed_by=str(payload.get("confirmed_by") or "user"),
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "taskflow": state.to_dict()},
                    )
                    return
                if action == "compile":
                    plan = self.service.taskflow_service.compile_goal(taskflow_id)
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "plan": plan.to_dict()},
                    )
                    return
            if (
                len(parts) == 7
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "work-items"
                and parts[6] == "dispatch"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                run = self.service.taskflow_service.dispatch_task_run(
                    parts[3],
                    work_item_id=parts[5],
                    executor_hint=(
                        str(payload["executor_hint"])
                        if payload.get("executor_hint") is not None
                        else None
                    ),
                    metadata=_payload_dict(payload.get("metadata")),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "task_run": run.to_dict()},
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
