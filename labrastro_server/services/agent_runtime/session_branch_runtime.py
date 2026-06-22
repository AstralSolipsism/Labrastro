from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass
class SessionRuntimeOperation:
    operation_id: str
    kind: str
    scope_id: str
    source_revision: int
    visible: bool
    target_branch_binding_id: str = ""
    optimistic_effect: dict[str, Any] | None = None


@dataclass
class BranchRuntimeScope:
    scope_id: str
    session_run_id: str
    branch_binding_id: str
    agent_run_id: str
    runtime_revision: int
    status: str
    pending_next_turns: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    pending_user_inputs: list[dict[str, Any]] = field(default_factory=list)
    operations_by_id: dict[str, SessionRuntimeOperation] = field(default_factory=dict)
    active_activation_id: str = ""
    stream_cursor: int | None = None


@dataclass
class SessionRunRuntimeModel:
    selected_scope_id: str
    scopes_by_id: dict[str, BranchRuntimeScope]


@dataclass
class SessionRunScopedEvent:
    kind: str
    session_run_id: str
    branch_binding_id: str
    agent_run_id: str | None = None
    scope_id: str = ""
    status: str = ""
    pending_next_turn: dict[str, Any] | None = None
    operation_id: str = ""


def scope_id_for(session_run_id: str, branch_binding_id: str) -> str:
    return f"{_normalized(session_run_id)}:{_normalized(branch_binding_id)}"


def reduce_branch_runtime_event(
    model: SessionRunRuntimeModel,
    event: SessionRunScopedEvent,
) -> SessionRunRuntimeModel:
    scope = _resolve_scope(model, event)
    if scope is None:
        return model
    if event.kind == "terminal":
        return _replace_scope(
            model,
            _copy_scope(
                scope,
                status=_normalized(event.status) or scope.status,
                runtime_revision=scope.runtime_revision + 1,
            ),
        )
    if event.kind == "pending_next_turn":
        if not event.pending_next_turn:
            return model
        return _replace_scope(
            model,
            _copy_scope(
                scope,
                runtime_revision=scope.runtime_revision + 1,
                pending_next_turns=[
                    *scope.pending_next_turns,
                    dict(event.pending_next_turn),
                ],
            ),
        )
    if event.kind in {"operation_failure", "operation_success"}:
        operation_id = _normalized(event.operation_id)
        if not operation_id or operation_id not in scope.operations_by_id:
            return model
        operations = dict(scope.operations_by_id)
        operations.pop(operation_id, None)
        return _replace_scope(
            model,
            _copy_scope(
                scope,
                runtime_revision=scope.runtime_revision + 1,
                operations_by_id=operations,
            ),
        )
    return model


def _resolve_scope(
    model: SessionRunRuntimeModel,
    event: SessionRunScopedEvent,
) -> BranchRuntimeScope | None:
    if not _normalized(event.session_run_id) or not _normalized(event.branch_binding_id):
        return None
    scope_id = _normalized(event.scope_id) or scope_id_for(
        event.session_run_id,
        event.branch_binding_id,
    )
    scope = model.scopes_by_id.get(scope_id)
    if scope is None:
        return None
    if not _normalized(event.agent_run_id):
        return None
    if event.agent_run_id != scope.agent_run_id:
        return None
    return scope


def _replace_scope(
    model: SessionRunRuntimeModel,
    scope: BranchRuntimeScope,
) -> SessionRunRuntimeModel:
    scopes = dict(model.scopes_by_id)
    scopes[scope.scope_id] = scope
    return replace(model, scopes_by_id=scopes)


def _copy_scope(scope: BranchRuntimeScope, **changes: Any) -> BranchRuntimeScope:
    copied = {
        "pending_next_turns": list(scope.pending_next_turns),
        "pending_approvals": list(scope.pending_approvals),
        "pending_user_inputs": list(scope.pending_user_inputs),
        "operations_by_id": dict(scope.operations_by_id),
    }
    copied.update(changes)
    return replace(scope, **copied)


def _normalized(value: str | None) -> str:
    return str(value or "").strip()
