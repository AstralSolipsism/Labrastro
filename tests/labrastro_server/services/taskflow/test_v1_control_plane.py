from __future__ import annotations

import pytest

from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.taskflow_service import TaskflowService
from labrastro_server.taskflow.domain.project_state import ProjectState, WorkItem
from labrastro_server.taskflow.domain.taskflow_state import TaskflowEventType


def _service() -> TaskflowService:
    project_service = ProjectService()
    project_service.save_project_state(
        ProjectState.new(project_id="project-v1", name="Taskflow V1")
    )
    return TaskflowService(project_service=project_service)


def _compiled(service: TaskflowService, suffix: str = "v1") -> tuple[str, str]:
    state = service.start_taskflow(
        project_id="project-v1",
        raw_goal="Implement the Taskflow V1 control plane.",
        taskflow_id=f"taskflow-{suffix}",
        goal_id=f"goal-{suffix}",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        questions=[
            {
                "id": "question-risk",
                "stage": "risk",
                "question": "Does this need migration planning?",
                "why_needed": "Migration scope changes dispatch safety.",
                "risk_if_unknown": "medium",
                "default_suggestion": "No migration.",
            }
        ],
        assumptions=[
            {
                "id": "assumption-owner",
                "statement": "The project owner reviews compiler decisions.",
                "impact": "high",
                "reason": "Compiler review is a V1 control surface.",
            }
        ],
        decisions=[
            {
                "id": "decision-dispatch",
                "question": "How should dispatch be authorized?",
                "why_it_matters": "TaskRuns must not be implicit.",
                "options": [{"id": "confirmed", "label": "Confirmed dispatch"}],
                "recommended": "confirmed",
            }
        ],
        examples=[
            {
                "id": "acceptance-v1",
                "title": "Workspace accepted",
                "then": ["Workspace shows memory, compiler, runtime, and trace."],
            }
        ],
        work_item_candidates=[
            {
                "id": f"candidate-{suffix}",
                "title": "Implement V1 workspace",
                "description": "Add workspace projection and review surfaces.",
                "acceptance_refs": ["acceptance-v1"],
                "decision_refs": ["decision-dispatch"],
            }
        ],
    )
    brief = service.compile_brief_draft(state.meta.taskflow_id, actor="user")
    version = brief.outputs.current_brief_version
    brief = service.mark_brief_ready(
        state.meta.taskflow_id,
        version=version,
        actor="user",
    )
    service.confirm_brief(
        state.meta.taskflow_id,
        version=brief.outputs.current_brief_version,
        actor="user",
    )
    plan = service.compile_goal(state.meta.taskflow_id)
    return state.meta.taskflow_id, plan.work_item_candidates[0].work_item_id


def test_review_card_v1_accept_edit_skip_reopen_and_discuss_updates_state() -> None:
    service = _service()
    taskflow_id, _work_item_id = _compiled(service)

    cards = service.render_review_cards_v1(taskflow_id)
    by_kind = {card.kind: card for card in cards}
    assert by_kind["question"].why_needed
    assert {"accept", "edit", "skip", "reopen", "discuss"} == {
        action.id for action in by_kind["question"].actions
    }

    service.answer_review_card_v1(
        taskflow_id,
        card_id=by_kind["question"].id,
        action="edit",
        value="Migration is covered by the ledger migration.",
        actor="user",
        comment="Owner clarified scope.",
    )
    edited = service.get_taskflow_state(taskflow_id)
    question = edited.clarification.open_questions[0]
    assert question.answer == "Migration is covered by the ledger migration."

    before_assumption = edited.clarification.assumptions[0].to_dict()
    service.answer_review_card_v1(
        taskflow_id,
        card_id=by_kind["assumption"].id,
        action="discuss",
        actor="user",
        comment="Needs stakeholder wording.",
    )
    discussed = service.get_taskflow_state(taskflow_id)
    assert discussed.clarification.assumptions[0].to_dict() == before_assumption
    assert discussed.outputs.review_card_answers[-1].action == "discuss"

    service.answer_review_card_v1(
        taskflow_id,
        card_id=by_kind["assumption"].id,
        action="accept",
        actor="user",
    )
    accepted = service.get_taskflow_state(taskflow_id)
    assert accepted.clarification.assumptions[0].state.value == "confirmed_by_user"

    service.answer_review_card_v1(
        taskflow_id,
        card_id=by_kind["decision"].id,
        action="skip",
        actor="user",
        comment="Deferred for another review.",
    )
    skipped = service.get_taskflow_state(taskflow_id)
    assert skipped.design.local_decisions[0].metadata["skip_consequence"]

    service.answer_review_card_v1(
        taskflow_id,
        card_id=by_kind["decision"].id,
        action="reopen",
        actor="user",
        comment="Needs another pass.",
    )
    reopened = service.get_taskflow_state(taskflow_id)
    assert reopened.compiler.traceability_index["compiler_review_stale"] is True
    assert reopened.outputs.dispatch_decisions == []
    assert any(
        event.type == TaskflowEventType.REVIEW_CARD_REOPENED
        for event in reopened.events
    )


def test_project_memory_patch_preview_apply_marks_compiler_stale_without_mutating_task_runs() -> None:
    service = _service()
    taskflow_id, work_item_id = _compiled(service)
    sibling_taskflow_id, _ = _compiled(service, suffix="sibling")
    decision = service.request_dispatch_decision(taskflow_id, work_item_ids=[work_item_id])
    service.confirm_dispatch_decision(taskflow_id, decision_id=decision.id)
    run = service.dispatch_task_run(
        taskflow_id,
        work_item_id=work_item_id,
        dispatch_decision_id=decision.id,
    )

    operations = [
        {
            "type": "upsert_term",
            "term": "CompilerDecision",
            "definition": "Structured explanation for a compiled WorkItem candidate.",
        },
        {"type": "remove_term", "term": "TemporaryTerm"},
        {
            "type": "upsert_decision",
            "id": "project-decision-1",
            "topic": "Dispatch requires confirmation",
            "rationale": "TaskRuns are never implicit.",
        },
        {
            "type": "upsert_constraint",
            "id": "constraint-1",
            "statement": "Dispatch must stay explicit.",
            "severity": "high",
            "source": "taskflow-v1",
        },
        {
            "type": "upsert_work_item",
            "work_item": {
                "id": "memory-work-1",
                "project_id": "project-v1",
                "title": "Govern Project Memory",
                "description": "Patch ProjectState through proposals.",
                "type": "implementation",
                "status": "ready",
            },
        },
        {
            "type": "update_work_item_status",
            "work_item_id": "memory-work-1",
            "status": "active",
        },
        {
            "type": "upsert_trace_link",
            "id": "trace-replace",
            "project_id": "project-v1",
            "source_type": "decision",
            "source_id": "project-decision-1",
            "target_type": "work_item",
            "target_id": "memory-work-1",
            "relation_type": "implements",
            "rationale": "Initial trace.",
        },
        {
            "type": "upsert_trace_link",
            "id": "trace-replace",
            "project_id": "project-v1",
            "source_type": "decision",
            "source_id": "project-decision-1",
            "target_type": "work_item",
            "target_id": "memory-work-1",
            "relation_type": "implements",
            "rationale": "Replaced trace.",
        },
        {
            "type": "upsert_trace_link",
            "id": "trace-remove",
            "project_id": "project-v1",
            "source_type": "decision",
            "source_id": "project-decision-1",
            "target_type": "work_item",
            "target_id": "memory-work-1",
            "relation_type": "implements",
            "rationale": "Temporary trace.",
        },
        {"type": "remove_trace_link", "id": "trace-remove"},
    ]
    preview = service.preview_project_memory_patch(
        taskflow_id,
        actor="user",
        reason="Align shared vocabulary.",
        source="workspace",
        operations=operations,
    )
    assert preview["proposal"]["status"] == "pending"
    assert preview["proposal"]["diff"][0]["after"].startswith("Structured")
    project_after_preview = service.project_service.get_project_state("project-v1")
    assert project_after_preview is not None
    assert project_after_preview.projections.reviews[-1]["status"] == "pending"

    applied = service.apply_project_memory_patch(
        taskflow_id,
        proposal_id=preview["proposal"]["id"],
        actor="user",
        reason="Align shared vocabulary.",
        source="workspace",
        operations=[],
    )

    state = service.get_taskflow_state(taskflow_id)
    sibling = service.get_taskflow_state(sibling_taskflow_id)
    project = service.project_service.get_project_state("project-v1")
    assert project is not None
    assert project.knowledge_base.ubiquitous_language["CompilerDecision"].startswith("Structured")
    assert "TemporaryTerm" not in project.knowledge_base.ubiquitous_language
    assert project.decisions.project_decisions[0].id == "project-decision-1"
    assert project.project_profile.constraints[0].severity == "high"
    memory_work = next(item for item in project.list_work_items() if item.id == "memory-work-1")
    assert memory_work.status.value == "active"
    trace = next(item for item in project.traceability.decision_links if item.id == "trace-replace")
    assert trace.rationale == "Replaced trace."
    assert all(item.id != "trace-remove" for item in project.traceability.decision_links)
    assert applied["proposal"]["status"] == "applied"
    assert state.compiler.traceability_index["compiler_review_stale"] is True
    assert sibling.compiler.traceability_index["compiler_review_stale"] is True
    assert project.traceability.task_runs[0].id == run.id
    assert any(
        event.type == TaskflowEventType.PROJECT_MEMORY_PATCH_APPLIED
        for event in state.events
    )


def test_compiler_decision_override_blocks_dispatch_until_recompiled() -> None:
    service = _service()
    taskflow_id, work_item_id = _compiled(service)
    state = service.get_taskflow_state(taskflow_id)
    decision_id = state.outputs.compiler_decisions[0]["id"]
    assert state.outputs.compiler_decisions[0]["action"] == "create"

    reviewed = service.review_compiler_decision(
        taskflow_id,
        decision_id=decision_id,
        action="force_create",
        actor="user",
        reason="Need separate delivery boundary.",
    )
    assert reviewed["compiler_decision"]["status"] == "stale"

    with pytest.raises(ValueError, match="compiler review is stale"):
        service.request_dispatch_decision(taskflow_id, work_item_ids=[work_item_id])

    service.compile_goal(taskflow_id)
    refreshed = service.get_taskflow_state(taskflow_id)
    assert refreshed.compiler.traceability_index.get("compiler_review_stale") is None
    service.request_dispatch_decision(taskflow_id, work_item_ids=[work_item_id])


def test_compiler_decision_review_actions_validate_and_survive_recompile() -> None:
    service = _service()
    taskflow_id, _work_item_id = _compiled(service)
    project = service.project_service.get_project_state("project-v1")
    assert project is not None
    project.upsert_work_item(
        WorkItem(
            id="existing-work",
            project_id="project-v1",
            title="Existing reusable V1 workspace",
            description="Existing reusable boundary.",
            type="implementation",
            status="ready",
            dedupe_key="existing-key",
        )
    )
    service.project_service.save_project_state(project)
    decision_id = service.get_taskflow_state(taskflow_id).outputs.compiler_decisions[0]["id"]

    service.review_compiler_decision(
        taskflow_id,
        decision_id=decision_id,
        action="accept",
        actor="user",
    )
    assert service.get_taskflow_state(taskflow_id).outputs.compiler_decisions[0]["status"] == "accepted"

    with pytest.raises(ValueError, match="force_reuse requires"):
        service.review_compiler_decision(
            taskflow_id,
            decision_id=decision_id,
            action="force_reuse",
            actor="user",
            reason="Reuse existing boundary.",
        )

    service.review_compiler_decision(
        taskflow_id,
        decision_id=decision_id,
        action="force_reuse",
        actor="user",
        reason="Reuse existing boundary.",
        value={"work_item_id": "existing-work"},
    )
    reused = service.compile_goal(taskflow_id)
    assert reused.work_item_candidates[0].action == "reuse"
    assert reused.work_item_candidates[0].work_item_id == "existing-work"
    recompiled_state = service.get_taskflow_state(taskflow_id)
    assert recompiled_state.outputs.compiler_decisions[0]["override"]["action"] == "force_reuse"

    split_decision_id = recompiled_state.outputs.compiler_decisions[0]["id"]
    service.review_compiler_decision(
        taskflow_id,
        decision_id=split_decision_id,
        action="split",
        actor="user",
        reason="Needs separate review surface.",
        value={"description": "Split runtime projection from memory governance."},
    )
    split_plan = service.compile_goal(taskflow_id)
    assert "Split requested" in split_plan.work_item_candidates[0].rationale

    reject_decision_id = service.get_taskflow_state(taskflow_id).outputs.compiler_decisions[0]["id"]
    service.review_compiler_decision(
        taskflow_id,
        decision_id=reject_decision_id,
        action="reject",
        actor="user",
        reason="Drop this candidate from the plan.",
    )
    rejected_plan = service.compile_goal(taskflow_id)
    assert rejected_plan.work_item_candidates == []


def test_workspace_and_projector_preview_are_read_only_v1_contracts() -> None:
    service = _service()
    taskflow_id, _work_item_id = _compiled(service)

    workspace = service.get_workspace_v1(taskflow_id)
    preview = service.get_projector_preview(taskflow_id, target="speckit")

    assert workspace["schema_version"] == "taskflow.workspace.v1"
    assert workspace["project_memory"]["project_id"] == "project-v1"
    assert workspace["compiler_review"]["decisions"]
    assert workspace["projector_previews"][0]["read_only"] is True
    assert preview["projector_preview"]["target"] == "speckit"
    assert preview["projector_preview"]["status"] == "preview_only"
