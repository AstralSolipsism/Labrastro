from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.taskflow_service import TaskflowService
from labrastro_server.taskflow.domain.project_state import (
    ProjectState,
    TaskRun,
    TaskRunStatus,
    TraceEntityType,
    TraceRelationType,
)
from labrastro_server.taskflow.domain.taskflow_state import (
    ReadinessGate,
    TaskflowStatus,
    WorkItemCandidate,
)
from labrastro_server.taskflow.ports.dispatch import TaskflowDispatchResult


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[TaskRun] = []

    def dispatch_task_run(
        self,
        task_run: TaskRun,
        *,
        executor_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskflowDispatchResult:
        self.calls.append(task_run)
        return TaskflowDispatchResult(
            selected_executor_id=executor_hint or "fake-executor",
            agent_run_ref={"id": f"agent-run-{task_run.id}"},
            reason="fake-dispatched",
        )


def test_single_taskflow_service_exposes_only_new_mainline_api() -> None:
    service = TaskflowService(project_service=ProjectService())

    expected = {
        "start_taskflow",
        "clarify_goal",
        "render_review_cards",
        "confirm_goal",
        "compile_goal",
        "dispatch_task_run",
    }
    old_api = {
        "create_goal",
        "record_brief",
        "create_issue_draft",
        "create_task_draft",
        "dispatch_task_draft",
    }

    assert expected <= {name for name, _ in inspect.getmembers(service, inspect.ismethod)}
    assert old_api.isdisjoint(dir(service))


def test_taskflow_service_dispatches_task_run_through_neutral_port() -> None:
    dispatcher = FakeDispatcher()
    project_service = ProjectService()
    project_service.save_project_state(
        ProjectState.new(project_id="project-1", name="Taskflow")
    )
    service = TaskflowService(project_service=project_service, dispatcher=dispatcher)

    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Build the compiler-only taskflow service.",
        session_id="session-1",
        peer_id="peer-1",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.clarify_goal(
        state.meta.taskflow_id,
        work_item_candidates=[
            WorkItemCandidate(
                id="candidate-1",
                title="Implement compiler service",
                description="Use ProjectState and TaskflowState as the only truth.",
                type="implementation",
                dedupe_key="project-1:implementation:compiler-service",
            )
        ],
        readiness_gates=[
            ReadinessGate(
                id="gate-1",
                name="ready",
                passed=True,
                rationale="The work item candidate is explicit.",
            )
        ],
        readiness_score=90,
    )
    service.confirm_goal("taskflow-1")
    plan = service.compile_goal("taskflow-1")

    run = service.dispatch_task_run(
        "taskflow-1",
        work_item_id=plan.work_item_candidates[0].work_item_id,
        executor_hint="fake-executor",
    )

    assert dispatcher.calls == [run]
    assert run.status == TaskRunStatus.DISPATCHED
    stored = project_service.get_project_state("project-1")
    assert stored is not None
    links = stored.traceability.task_run_links
    assert any(
        link.source_type == TraceEntityType.TASK_RUN
        and link.source_id == run.id
        and link.target_type == TraceEntityType.AGENT_RUN
        and link.target_id == f"agent-run-{run.id}"
        and link.relation_type == TraceRelationType.DISPATCHES
        for link in links
    )
    assert service.get_taskflow_state("taskflow-1").meta.status == (
        TaskflowStatus.DISPATCHED
    )


def test_old_taskflow_service_aliases_are_removed() -> None:
    import labrastro_server.services.taskflow as legacy_exports

    assert not hasattr(legacy_exports, "CompilerTaskflowService")
    assert legacy_exports.TaskflowService is TaskflowService


def test_taskflow_core_and_application_do_not_import_executor_or_reuleauxcoder() -> None:
    root = Path(__file__).resolve().parents[3] / "labrastro_server" / "taskflow"
    forbidden = (
        "reuleauxcoder.",
        "reuleauxcoder\\",
        "services.agent_runtime",
        "AgentRunRequest",
        "AgentRunRecord",
        "AgentConfig",
        "TaskDraftRecord",
    )

    for source in root.rglob("*.py"):
        if "\\adapters\\" in str(source):
            continue
        text = source.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), source


def test_remote_taskflow_routes_use_new_resource_names_only() -> None:
    route_source = Path(
        "labrastro_server/interfaces/http/remote/routes/taskflow.py"
    ).read_text(encoding="utf-8")

    assert "task-drafts" not in route_source
    assert "record_brief" not in route_source
    assert "dispatch_task_draft" not in route_source
    assert "taskflows" in route_source
    assert "work-items" in route_source


def test_reuleauxcoder_adapter_submits_agent_run_from_task_run_only() -> None:
    from labrastro_server.adapters.reuleauxcoder.taskflow_dispatcher import (
        ReuleauxCoderTaskflowDispatcher,
    )
    from labrastro_server.services.agent_runtime.control_plane import (
        AgentRunControlPlane,
    )

    runtime = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {"docs_profile": {"executor": "fake"}},
            "agents": {
                "docs": {
                    "runtime_profile": "docs_profile",
                    "dispatch": {"profile": "Best for documentation tasks."},
                }
            },
        }
    )
    task_run = TaskRun(
        id="task-run-1",
        project_id="project-1",
        goal_id="goal-1",
        work_item_id="work-1",
        metadata={
            "taskflow_id": "taskflow-1",
            "work_item_description": "Write documentation",
        },
    )

    result = ReuleauxCoderTaskflowDispatcher(runtime).dispatch_task_run(
        task_run,
    )

    assert result.selected_executor_id == "docs"
    agent_run_id = str((result.agent_run_ref or {}).get("id") or "")
    assert agent_run_id
    agent_run = runtime.get_agent_run(agent_run_id)
    assert agent_run.agent_id == "docs"
    assert agent_run.source.value == "taskflow"
    assert agent_run.metadata["task_run_id"] == "task-run-1"
    assert agent_run.metadata["work_item_id"] == "work-1"
    assert agent_run.metadata["agent_run_source"] == "taskflow"
    assert "taskflow_task_draft_id" not in agent_run.metadata
