from __future__ import annotations

from labrastro_server.services.agent_runtime.session_branch_runtime import (
    BranchRuntimeScope,
    SessionRunRuntimeModel,
    SessionRunScopedEvent,
    SessionRuntimeOperation,
    reduce_branch_runtime_event,
    scope_id_for,
)


def _scope(branch_binding_id: str, *, status: str = "running") -> BranchRuntimeScope:
    return BranchRuntimeScope(
        scope_id=scope_id_for("run-1", branch_binding_id),
        session_run_id="run-1",
        branch_binding_id=branch_binding_id,
        agent_run_id=f"agent-{branch_binding_id}",
        runtime_revision=1,
        status=status,
        pending_next_turns=[],
        pending_approvals=[],
        pending_user_inputs=[],
        operations_by_id={},
    )


def selected_main_with_running_sibling() -> SessionRunRuntimeModel:
    main = _scope("main")
    branch_a = _scope("branch-a")
    return SessionRunRuntimeModel(
        selected_scope_id=main.scope_id,
        scopes_by_id={
            main.scope_id: main,
            branch_a.scope_id: branch_a,
        },
    )


def terminal_event(branch_binding_id: str, *, agent_run_id: str | None = None) -> SessionRunScopedEvent:
    return SessionRunScopedEvent(
        kind="terminal",
        session_run_id="run-1",
        branch_binding_id=branch_binding_id,
        agent_run_id=agent_run_id,
        status="done",
    )


def test_terminal_event_for_selected_branch_changes_selected_branch_scope_only() -> None:
    model = selected_main_with_running_sibling()

    next_model = reduce_branch_runtime_event(
        model,
        terminal_event("main", agent_run_id="agent-main"),
    )

    assert next_model.scopes_by_id[scope_id_for("run-1", "main")].status == "done"
    assert next_model.scopes_by_id[scope_id_for("run-1", "branch-a")].status == "running"


def test_terminal_event_for_sibling_branch_updates_sibling_scope_only() -> None:
    model = selected_main_with_running_sibling()

    next_model = reduce_branch_runtime_event(
        model,
        terminal_event("branch-a", agent_run_id="agent-branch-a"),
    )

    assert next_model.selected_scope_id == scope_id_for("run-1", "main")
    assert next_model.scopes_by_id[scope_id_for("run-1", "main")].status == "running"
    assert next_model.scopes_by_id[scope_id_for("run-1", "branch-a")].status == "done"


def test_pending_next_turn_is_keyed_by_session_run_id_and_branch_binding_id() -> None:
    model = selected_main_with_running_sibling()

    next_model = reduce_branch_runtime_event(
        model,
        SessionRunScopedEvent(
            kind="pending_next_turn",
            session_run_id="run-1",
            branch_binding_id="branch-a",
            agent_run_id="agent-branch-a",
            pending_next_turn={"text": "queued for branch A", "client_request_id": "req-a"},
        ),
    )

    assert next_model.scopes_by_id[scope_id_for("run-1", "main")].pending_next_turns == []
    assert next_model.scopes_by_id[scope_id_for("run-1", "branch-a")].pending_next_turns == [
        {"text": "queued for branch A", "client_request_id": "req-a"}
    ]


def test_operation_failure_settles_operation_in_its_scope() -> None:
    model = selected_main_with_running_sibling()
    branch_scope_id = scope_id_for("run-1", "branch-a")
    model.scopes_by_id[branch_scope_id].operations_by_id["op-1"] = SessionRuntimeOperation(
        operation_id="op-1",
        kind="continue",
        scope_id=branch_scope_id,
        source_revision=1,
        visible=False,
    )

    next_model = reduce_branch_runtime_event(
        model,
        SessionRunScopedEvent(
            kind="operation_failure",
            session_run_id="run-1",
            branch_binding_id="branch-a",
            agent_run_id="agent-branch-a",
            operation_id="op-1",
        ),
    )

    assert "op-1" not in next_model.scopes_by_id[branch_scope_id].operations_by_id


def test_runtime_scope_event_without_agent_run_id_fails_closed() -> None:
    model = selected_main_with_running_sibling()

    next_model = reduce_branch_runtime_event(model, terminal_event("branch-a"))

    assert next_model == model
