from __future__ import annotations

import pytest

from reuleauxcoder.domain.issue_assignment.models import AssignmentStatus, MentionStatus
from labrastro_server.adapters.reuleauxcoder.taskflow_dispatcher import (
    ReuleauxCoderTaskflowDispatcher,
)
from labrastro_server.services.agent_runtime.control_plane import AgentRuntimeControlPlane
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.taskflow.service import TaskflowService


def _runtime() -> AgentRuntimeControlPlane:
    return AgentRuntimeControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "docs_profile": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                },
                "design_profile": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                },
            },
            "agents": {
                "docs": {
                    "name": "Docs Agent",
                    "aliases": ["writer", "docsbot"],
                    "runtime_profile": "docs_profile",
                    "capabilities": ["docs", "research", "write_docs"],
                    "max_concurrent_tasks": 2,
                },
                "design": {
                    "name": "Designer",
                    "aliases": ["designer"],
                    "runtime_profile": "design_profile",
                    "capabilities": ["design"],
                },
            },
        }
    )


def _service() -> tuple[IssueAssignmentService, TaskflowService, AgentRuntimeControlPlane]:
    runtime = _runtime()
    taskflow = TaskflowService(dispatcher=ReuleauxCoderTaskflowDispatcher(runtime))
    return IssueAssignmentService(taskflow_service=taskflow), taskflow, runtime


def _work_item(taskflow: TaskflowService, project_id: str, work_item_id: str):
    project = taskflow.project_service.get_project_state(project_id)
    assert project is not None
    for item in [
        *project.work_items.active_work_items,
        *project.work_items.reusable_work_items,
    ]:
        if item.id == work_item_id:
            return item
    raise AssertionError(f"work item not found: {work_item_id}")


def test_assignment_creates_work_item_but_does_not_dispatch_before_dispatch_call() -> None:
    service, taskflow, runtime = _service()
    issue = service.create_issue(
        title="Write onboarding docs",
        description="Create the onboarding documentation.",
        peer_id="peer-a",
    )

    assignment = service.create_assignment(
        issue.id,
        peer_id="peer-a",
        target_agent_id="docs",
        required_capabilities=["docs"],
        preferred_capabilities=["write_docs"],
        task_type="docs",
    )

    assert assignment.status == AssignmentStatus.READY
    assert assignment.work_item_id is not None
    item = _work_item(taskflow, "peer-peer-a", assignment.work_item_id)
    assert item.metadata["issue_id"] == issue.id
    assert item.metadata["assignment_id"] == assignment.id
    assert runtime.list_tasks() == []


def test_assignment_dispatch_uses_taskflow_decision_and_runtime_metadata() -> None:
    service, taskflow, runtime = _service()
    issue = service.create_issue(
        title="Write docs",
        description="Write the docs.",
        peer_id="peer-a",
    )
    assignment = service.create_assignment(
        issue.id,
        peer_id="peer-a",
        target_agent_id="docs",
        required_capabilities=["docs"],
    )

    dispatched = service.dispatch_assignment(assignment.id, peer_id="peer-a")

    assert dispatched.status == AssignmentStatus.DISPATCHED
    assert dispatched.runtime_task_id is not None
    task = runtime.get_task(dispatched.runtime_task_id)
    assert task.agent_id == "docs"
    assert task.metadata["issue_id"] == issue.id
    assert task.metadata["assignment_id"] == assignment.id
    assert task.metadata["dispatch_source"] == "assignment"
    assert dispatched.task_run_id is not None


def test_assignment_without_capable_agent_needs_assignment_and_creates_no_runtime() -> None:
    service, _taskflow, runtime = _service()
    issue = service.create_issue(
        title="Secret ops",
        description="Use unavailable secret vault.",
        peer_id="peer-a",
    )
    assignment = service.create_assignment(
        issue.id,
        peer_id="peer-a",
        required_capabilities=["secret_vault"],
    )

    updated = service.dispatch_assignment(assignment.id, peer_id="peer-a")

    assert updated.status == AssignmentStatus.NEEDS_ASSIGNMENT
    assert updated.runtime_task_id is None
    assert runtime.list_tasks() == []


def test_assignment_can_be_reassigned_before_dispatch() -> None:
    service, _taskflow, _runtime = _service()
    issue = service.create_issue(
        title="Review design",
        description="Review the design.",
        peer_id="peer-a",
    )
    assignment = service.create_assignment(
        issue.id,
        peer_id="peer-a",
        target_agent_id="docs",
    )

    reassigned = service.reassign_assignment(
        assignment.id,
        peer_id="peer-a",
        agent_id="design",
        reason="needs design review",
    )

    assert reassigned.target_agent_id == "design"
    assert reassigned.status == AssignmentStatus.READY


def test_mention_resolves_alias_and_creates_assignment_but_not_runtime_task() -> None:
    service, _taskflow, runtime = _service()
    issue = service.create_issue(
        title="Draft guide",
        description="Draft the guide.",
        peer_id="peer-a",
    )

    mention = service.create_mention(
        raw_text="@writer please draft this guide",
        peer_id="peer-a",
        issue_id=issue.id,
        prompt="Draft the guide.",
    )

    assert mention.status == MentionStatus.READY
    assert mention.resolved_agent_id == "docs"
    assert mention.assignment_id is not None
    assignment = service.store.get_assignment(mention.assignment_id)
    assert assignment.target_agent_id == "docs"
    assert assignment.source == "mention"
    assert runtime.list_tasks() == []


def test_mention_conflict_or_unknown_agent_needs_assignment() -> None:
    runtime = _runtime()
    runtime.runtime_snapshot["agents"]["other_docs"] = {
        "aliases": ["writer"],
        "runtime_profile": "docs_profile",
        "capabilities": ["docs"],
    }
    service = IssueAssignmentService(
        taskflow_service=TaskflowService(
            dispatcher=ReuleauxCoderTaskflowDispatcher(runtime)
        )
    )

    mention = service.parse_mention(raw_text="@writer help", peer_id="peer-a")

    assert mention.status == MentionStatus.NEEDS_ASSIGNMENT
    assert mention.reason == "alias_ambiguous"
    assert {candidate["agent_id"] for candidate in mention.candidates} == {
        "docs",
        "other_docs",
    }


def test_peer_cannot_access_other_peer_issue_or_assignment() -> None:
    service, _taskflow, _runtime = _service()
    issue = service.create_issue(title="Owned", peer_id="peer-a")
    assignment = service.create_assignment(issue.id, peer_id="peer-a")

    with pytest.raises(PermissionError):
        service.load_issue_detail(issue.id, peer_id="peer-b")
    with pytest.raises(PermissionError):
        service.dispatch_assignment(assignment.id, peer_id="peer-b")
