from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote

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
                and parts[4] == "workspace"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                workspace = self.service.taskflow_service.get_workspace_v1(
                    parts[3],
                    runtime_control_plane=self.service.runtime_control_plane,
                )
                self._send_json(HTTPStatus.OK, workspace)
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "project-memory"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                self._send_json(
                    HTTPStatus.OK,
                    self.service.taskflow_service.get_project_memory_view(parts[3]),
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "review-cards-v1"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                cards = self.service.taskflow_service.render_review_cards_v1(parts[3])
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "review_cards": [card.to_dict() for card in cards]},
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "projector-preview"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                query = parse_qs(parsed.query or "")
                target = (query.get("target") or ["openspec"])[0]
                self._send_json(
                    HTTPStatus.OK,
                    self.service.taskflow_service.get_projector_preview(
                        parts[3],
                        target=target,
                    ),
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "complexity"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "complexity": self.service.taskflow_service.get_complexity_assessment(
                            parts[3]
                        ),
                    },
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "runtime"
            ):
                state = self.service.taskflow_service.get_taskflow_state(parts[3])
                _assert_taskflow_peer(state, peer_id)
                projection = self.service.taskflow_service.get_runtime_projection(
                    parts[3],
                    runtime_control_plane=self.service.runtime_control_plane,
                )
                self._send_json(HTTPStatus.OK, projection)
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
                if action == "compile":
                    plan = self.service.taskflow_service.compile_goal(taskflow_id)
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "plan": plan.to_dict()},
                    )
                    return
                if action == "discovery-turn":
                    state = self.service.taskflow_service.record_discovery_turn(
                        taskflow_id,
                        actor=str(payload.get("actor") or "agent"),
                        goal_statement=payload.get("goal_statement"),
                        background_delta=payload.get("background_delta"),
                        scope_in=(
                            _payload_list(payload.get("scope_in"))
                            if "scope_in" in payload
                            else None
                        ),
                        scope_out=(
                            _payload_list(payload.get("scope_out"))
                            if "scope_out" in payload
                            else None
                        ),
                        deferred_scope=(
                            _payload_list(payload.get("deferred_scope"))
                            if "deferred_scope" in payload
                            else None
                        ),
                        success_criteria=(
                            _payload_list(payload.get("success_criteria"))
                            if "success_criteria" in payload
                            else None
                        ),
                        assumptions=_payload_list(payload.get("assumptions")),
                        questions=_payload_list(
                            payload.get("questions")
                            or payload.get("open_questions")
                        ),
                        rules=_payload_list(payload.get("rules")),
                        examples=_payload_list(payload.get("examples")),
                        scenarios=_payload_list(payload.get("scenarios")),
                        acceptance_examples=_payload_list(
                            payload.get("acceptance_examples")
                        ),
                        decisions=_payload_list(payload.get("decisions")),
                        work_item_candidates=_payload_list(
                            payload.get("work_item_candidates")
                        ),
                        complexity_evidence=_payload_list(
                            payload.get("complexity_evidence")
                        ),
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "taskflow": state.to_dict()},
                    )
                    return
            if (
                len(parts) == 6
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "complexity"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                if parts[5] == "evidence":
                    state = self.service.taskflow_service.record_complexity_evidence(
                        taskflow_id,
                        evidence=_payload_list(
                            payload.get("evidence")
                            or payload.get("complexity_evidence")
                        ),
                        actor=str(payload.get("actor") or "agent"),
                    )
                elif parts[5] == "override":
                    state = self.service.taskflow_service.override_complexity(
                        taskflow_id,
                        level=str(payload.get("level") or ""),
                        reason=str(payload.get("reason") or ""),
                        actor=str(payload.get("actor") or "user"),
                    )
                elif parts[5] == "refresh":
                    state = self.service.taskflow_service.refresh_complexity_assessment(
                        taskflow_id
                    )
                elif parts[5] == "scan-repo":
                    state = self.service.taskflow_service.scan_repo_complexity(
                        taskflow_id,
                        workspace_path=(
                            str(payload["workspace_path"])
                            if payload.get("workspace_path") is not None
                            else None
                        ),
                        repository_id=str(payload.get("repository_id") or ""),
                    )
                else:
                    state = None
                if state is not None:
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "taskflow": state.to_dict(),
                            "complexity": self.service.taskflow_service.get_complexity_assessment(
                                taskflow_id
                            ),
                        },
                    )
                    return
            if (
                len(parts) == 6
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "brief"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                version = (
                    int(payload["version"])
                    if payload.get("version") is not None
                    else None
                )
                if parts[5] == "compile":
                    state = self.service.taskflow_service.compile_brief_draft(
                        taskflow_id,
                        actor=str(payload.get("actor") or "agent"),
                    )
                elif parts[5] == "ready":
                    state = self.service.taskflow_service.mark_brief_ready(
                        taskflow_id,
                        version=version,
                        actor=str(payload.get("actor") or "agent"),
                    )
                elif parts[5] == "confirm":
                    state = self.service.taskflow_service.confirm_brief(
                        taskflow_id,
                        version=version,
                        actor=str(payload.get("actor") or "user"),
                    )
                else:
                    state = None
                if state is not None:
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "taskflow": state.to_dict()},
                    )
                    return
            if (
                len(parts) == 7
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "review-cards-v1"
                and parts[6] == "actions"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                answer = self.service.taskflow_service.answer_review_card_v1(
                    taskflow_id,
                    card_id=parts[5],
                    action=str(payload.get("action") or ""),
                    value=payload.get("value"),
                    actor=str(payload.get("actor") or "user"),
                    comment=str(payload.get("comment") or payload.get("reason") or ""),
                )
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "answer": answer.to_dict(),
                        "taskflow": state.to_dict(),
                    },
                )
                return
            if (
                len(parts) == 7
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "project-memory"
                and parts[5] == "patches"
                and parts[6] == "preview"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                self._send_json(
                    HTTPStatus.OK,
                    self.service.taskflow_service.preview_project_memory_patch(
                        taskflow_id,
                        actor=str(payload.get("actor") or "user"),
                        reason=str(payload.get("reason") or ""),
                        source=str(payload.get("source") or "api"),
                        operations=_payload_list(payload.get("operations")),
                    ),
                )
                return
            if (
                len(parts) == 8
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "project-memory"
                and parts[5] == "patches"
                and parts[7] == "apply"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                self._send_json(
                    HTTPStatus.OK,
                    self.service.taskflow_service.apply_project_memory_patch(
                        taskflow_id,
                        proposal_id=parts[6],
                        actor=str(payload.get("actor") or "user"),
                        reason=str(payload.get("reason") or ""),
                        source=str(payload.get("source") or "api"),
                        operations=_payload_list(payload.get("operations")),
                    ),
                )
                return
            if (
                len(parts) == 7
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "compiler-decisions"
                and parts[6] == "review"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                self._send_json(
                    HTTPStatus.OK,
                    self.service.taskflow_service.review_compiler_decision(
                        taskflow_id,
                        decision_id=parts[5],
                        action=str(payload.get("action") or ""),
                        actor=str(payload.get("actor") or "user"),
                        reason=str(payload.get("reason") or ""),
                        value=payload.get("value"),
                    ),
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "dispatch-decisions"
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                decision = self.service.taskflow_service.request_dispatch_decision(
                    taskflow_id,
                    work_item_ids=_payload_list(payload.get("work_item_ids")),
                    actor=str(payload.get("actor") or "user"),
                    rationale=str(payload.get("rationale") or ""),
                    metadata=_payload_dict(payload.get("metadata")),
                )
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "dispatch_decision": decision.to_dict(),
                        "taskflow": state.to_dict(),
                    },
                )
                return
            if (
                len(parts) == 7
                and parts[:3] == ["remote", "taskflow", "taskflows"]
                and parts[4] == "dispatch-decisions"
                and parts[6] in {"confirm", "reject"}
            ):
                taskflow_id = parts[3]
                state = self.service.taskflow_service.get_taskflow_state(taskflow_id)
                _assert_taskflow_peer(state, peer_id)
                if parts[6] == "confirm":
                    state = self.service.taskflow_service.confirm_dispatch_decision(
                        taskflow_id,
                        decision_id=parts[5],
                        actor=str(payload.get("actor") or "user"),
                    )
                else:
                    state = self.service.taskflow_service.reject_dispatch_decision(
                        taskflow_id,
                        decision_id=parts[5],
                        actor=str(payload.get("actor") or "user"),
                    )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "taskflow": state.to_dict()},
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
                    dispatch_decision_id=(
                        str(payload["dispatch_decision_id"])
                        if payload.get("dispatch_decision_id") is not None
                        else None
                    ),
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
