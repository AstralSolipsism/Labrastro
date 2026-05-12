from __future__ import annotations

import pytest

from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.taskflow_service import TaskflowService
from labrastro_server.taskflow.domain.project_state import ProjectState, TaskRunStatus
from labrastro_server.taskflow.ports.dispatch import TaskflowDispatchResult


class _SubmittingDispatcher:
    def dispatch_task_run(
        self,
        task_run,
        *,
        executor_hint: str | None = None,
        metadata: dict | None = None,
    ) -> TaskflowDispatchResult:
        return TaskflowDispatchResult(
            selected_executor_id="agent-1",
            reason="agent_run_submitted",
            agent_run_ref={"id": f"agent-run-{task_run.id}", "status": "queued"},
        )


class _Runtime:
    def load_agent_run_detail(self, task_id: str, *, event_limit: int = 50) -> dict:
        return {
            "agent_run": {"id": task_id, "status": "running"},
            "events": [{"id": "event-1", "event_type": "started"}],
            "artifacts": [{"id": "artifact-1", "type": "log"}],
            "claim": {"status": "active"},
        }


def _service(*, dispatcher=None) -> TaskflowService:
    project_service = ProjectService()
    project_service.save_project_state(ProjectState.new(project_id="project-1"))
    return TaskflowService(project_service=project_service, dispatcher=dispatcher)


def _record_discovery(service: TaskflowService) -> str:
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Implement the Taskflow V1 operating console.",
        taskflow_id="taskflow-v1",
        goal_id="goal-v1",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        examples=[{
            "id": "acceptance-1",
            "title": "Core loop accepted",
            "then": ["Taskflow shows dispatch and runtime state."],
        }],
        work_item_candidates=[{
            "id": "candidate-1",
            "title": "Implement core loop",
            "description": "Compile, authorize, dispatch, and observe TaskRuns.",
            "acceptance_refs": ["acceptance-1"],
        }],
    )
    return state.meta.taskflow_id


def _confirmed_plan(service: TaskflowService):
    taskflow_id = _record_discovery(service)
    service.confirm_goal(taskflow_id)
    plan = service.compile_goal(taskflow_id)
    return taskflow_id, plan


def test_confirmed_brief_required_before_compile() -> None:
    service = _service()
    taskflow_id = _record_discovery(service)

    with pytest.raises(ValueError, match="confirmed brief"):
        service.compile_goal(taskflow_id)


def test_dispatch_decision_required_before_dispatch() -> None:
    service = _service()
    taskflow_id, plan = _confirmed_plan(service)

    with pytest.raises(ValueError, match="dispatch_decision_id is required"):
        service.dispatch_task_run(
            taskflow_id,
            work_item_id=plan.work_item_candidates[0].work_item_id,
        )


def test_no_runtime_task_before_dispatch_confirmation() -> None:
    service = _service()
    taskflow_id, plan = _confirmed_plan(service)
    decision = service.request_dispatch_decision(
        taskflow_id,
        work_item_ids=[plan.work_item_candidates[0].work_item_id],
    )

    with pytest.raises(ValueError, match="must be confirmed"):
        service.dispatch_task_run(
            taskflow_id,
            work_item_id=plan.work_item_candidates[0].work_item_id,
            dispatch_decision_id=decision.id,
        )
    projection = service.get_runtime_projection(taskflow_id)
    assert projection["task_runs"] == []


def test_dispatch_result_links_task_run_to_agent_run() -> None:
    service = _service(dispatcher=_SubmittingDispatcher())
    taskflow_id, plan = _confirmed_plan(service)
    decision = service.request_dispatch_decision(
        taskflow_id,
        work_item_ids=[plan.work_item_candidates[0].work_item_id],
    )
    service.confirm_dispatch_decision(taskflow_id, decision_id=decision.id)

    run = service.dispatch_task_run(
        taskflow_id,
        work_item_id=plan.work_item_candidates[0].work_item_id,
        dispatch_decision_id=decision.id,
    )
    projection = service.get_runtime_projection(
        taskflow_id,
        runtime_control_plane=_Runtime(),
    )

    assert projection["task_runs"][0]["task_run"]["id"] == run.id
    assert projection["task_runs"][0]["agent_run"]["id"] == f"agent-run-{run.id}"
    assert projection["task_runs"][0]["events"][0]["event_type"] == "started"
    assert projection["task_runs"][0]["artifacts"][0]["id"] == "artifact-1"


def test_failed_task_run_has_visible_liveness_reason() -> None:
    service = _service()
    taskflow_id, plan = _confirmed_plan(service)
    decision = service.request_dispatch_decision(
        taskflow_id,
        work_item_ids=[plan.work_item_candidates[0].work_item_id],
    )
    service.confirm_dispatch_decision(taskflow_id, decision_id=decision.id)
    run = service.dispatch_task_run(
        taskflow_id,
        work_item_id=plan.work_item_candidates[0].work_item_id,
        dispatch_decision_id=decision.id,
    )
    project = service.project_service.get_project_state("project-1")
    assert project is not None
    project.traceability.task_runs[0].status = TaskRunStatus.FAILED
    project.traceability.task_runs[0].metadata["failure_reason"] = (
        "Unit tests failed after dispatch."
    )
    service.project_service.save_project_state(project)

    projection = service.get_runtime_projection(taskflow_id)

    assert projection["task_runs"][0]["task_run"]["id"] == run.id
    assert projection["task_runs"][0]["liveness"]["state"] == "failed"
    assert projection["task_runs"][0]["liveness"]["reason"] == (
        "Unit tests failed after dispatch."
    )
