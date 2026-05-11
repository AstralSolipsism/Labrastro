from __future__ import annotations

import pytest

from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.taskflow_service import TaskflowService
from labrastro_server.taskflow.domain.project_state import (
    ProjectState,
    TaskRunExecutor,
    TaskRunStatus,
)
from labrastro_server.taskflow.domain.taskflow_state import (
    Assumption,
    ConfirmationState,
    ReadinessGate,
    TaskflowStatus,
    WorkItemCandidate,
)


def test_taskflow_service_runs_goal_compile_and_dispatch_flow() -> None:
    project_service = ProjectService()
    project_service.save_project_state(
        ProjectState.new(project_id="project-1", name="Taskflow")
    )
    service = TaskflowService(project_service=project_service)

    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Build the Taskflow compiler skeleton.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.clarify_goal(
        state.meta.taskflow_id,
        scope_in=["ProjectState", "TaskflowState", "PlanCompiler"],
        scope_out=["Frontend rendering"],
        success_criteria=["Compiler emits traceable WorkItem candidates."],
        assumptions=[
            Assumption(
                id="assumption-1",
                statement="TaskDraft becomes a WorkItem candidate.",
                impact="high",
                state=ConfirmationState.CONFIRMED_BY_USER,
            )
        ],
        work_item_candidates=[
            WorkItemCandidate(
                id="candidate-1",
                title="Implement PlanCompiler",
                description="Compile TaskflowState into WorkItem candidates and links.",
                type="implementation",
                acceptance_refs=["acceptance-1"],
                decision_refs=["decision-1"],
                dedupe_key="project-1:implementation:plan-compiler",
            )
        ],
        readiness_gates=[
            ReadinessGate(
                id="gate-1",
                name="dispatch-readiness",
                passed=True,
                rationale="Goal, scope, acceptance, and candidate work item exist.",
            )
        ],
        readiness_score=90,
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        examples=[
            {
                "id": "acceptance-1",
                "title": "PlanCompiler acceptance",
                "then": ["Compiler emits traceable WorkItem candidates."],
            }
        ],
        decisions=[
            {
                "id": "decision-1",
                "question": "Should PlanCompiler be implemented?",
                "chosen": "yes",
            }
        ],
    )
    confirmed = service.confirm_goal(state.meta.taskflow_id, confirmed_by="user")

    plan = service.compile_goal(confirmed.meta.taskflow_id)
    dispatch_decision = service.request_dispatch_decision(
        confirmed.meta.taskflow_id,
        work_item_ids=[plan.work_item_candidates[0].work_item_id],
        actor="user",
    )
    service.confirm_dispatch_decision(
        confirmed.meta.taskflow_id,
        decision_id=dispatch_decision.id,
        actor="user",
    )
    run = service.dispatch_task_run(
        confirmed.meta.taskflow_id,
        work_item_id=plan.work_item_candidates[0].work_item_id,
        dispatch_decision_id=dispatch_decision.id,
        executor=TaskRunExecutor.AGENT,
    )
    saved_project = project_service.get_project_state("project-1")
    saved_state = service.get_taskflow_state("taskflow-1")

    assert plan.objective == "Build the Taskflow compiler skeleton."
    assert plan.work_item_candidates[0].action == "create"
    assert saved_project is not None
    assert saved_project.work_items.active_work_items[0].title == "Implement PlanCompiler"
    assert run.status == TaskRunStatus.PENDING
    assert saved_state.meta.status == TaskflowStatus.READY_FOR_DISPATCH
    assert saved_state.outputs.task_run_refs == [run.id]


def test_taskflow_service_blocks_dispatch_when_readiness_gate_fails() -> None:
    project_service = ProjectService()
    project_service.save_project_state(
        ProjectState.new(project_id="project-1", name="Taskflow")
    )
    service = TaskflowService(project_service=project_service)
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Build without enough information.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.clarify_goal(
        state.meta.taskflow_id,
        work_item_candidates=[
            WorkItemCandidate(
                id="candidate-1",
                title="Implement incomplete work",
                description="Missing readiness evidence.",
                type="implementation",
                acceptance_refs=["acceptance-1"],
            )
        ],
        assumptions=[
            Assumption(
                id="assumption-1",
                statement="This goal is intentionally incomplete.",
                state=ConfirmationState.CONFIRMED_BY_USER,
            )
        ],
        readiness_score=30,
        readiness_gates=[
                ReadinessGate(
                    id="gate-1",
                    name="acceptance-coverage",
                    passed=True,
                    rationale="Acceptance example is present.",
                )
        ],
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        examples=[
            {
                "id": "acceptance-1",
                "title": "Incomplete work is observable",
                "then": ["A dispatch decision is still required."],
            }
        ],
    )
    service.confirm_goal(state.meta.taskflow_id)
    service.compile_goal(state.meta.taskflow_id)

    with pytest.raises(ValueError, match="dispatch_decision_id"):
        service.dispatch_task_run(
            state.meta.taskflow_id,
            work_item_id="work-candidate-1",
        )
