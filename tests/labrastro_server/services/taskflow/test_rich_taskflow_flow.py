from __future__ import annotations

import pytest

from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.taskflow_service import TaskflowService
from labrastro_server.taskflow.domain.project_state import ProjectState, TraceEntityType, WorkItemType
from labrastro_server.taskflow.domain.taskflow_state import (
    BriefStatus,
    DecisionOption,
    RiskRecord,
    TaskflowEventType,
)


def _service() -> TaskflowService:
    project_service = ProjectService()
    project_service.save_project_state(
        ProjectState.new(project_id="project-1", name="Taskflow")
    )
    return TaskflowService(project_service=project_service)


def test_discovery_turn_answer_decision_and_confirmed_brief_compile_to_acceptance_metadata() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Build complete taskflow discovery.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )

    state = service.record_discovery_turn(
        state.meta.taskflow_id,
        actor="agent",
        rules=[
            {
                "id": "rule-confirm-before-dispatch",
                "statement": "Runtime tasks require a confirmed brief.",
                "status": "proposed",
                "risk_level": "high",
            }
        ],
        examples=[
            {
                "id": "example-unconfirmed-dispatch",
                "rule_id": "rule-confirm-before-dispatch",
                "title": "Unconfirmed brief blocks dispatch",
                "kind": "negative",
                "given": ["Brief is draft"],
                "when": ["Dispatch is requested"],
                "then": ["No runtime task is created"],
                "observable_outputs": ["runtime_event"],
            }
        ],
        decisions=[
            {
                "id": "decision-confirm-boundary",
                "decision_type": "product_scope",
                "question": "What confirmation boundary should Taskflow use?",
                "why_it_matters": "It protects execution authority.",
                "options": [
                    {
                        "id": "brief",
                        "label": "Confirm brief before dispatch",
                        "tradeoff": "Keeps execution explicit.",
                    }
                ],
                "recommended": "brief",
                "linked_rule_ids": ["rule-confirm-before-dispatch"],
            }
        ],
        work_item_candidates=[
            {
                "id": "candidate-1",
                "title": "Implement discovery confirmation boundary",
                "description": "Persist decisions, rules, examples, and brief confirmation.",
                "type": "implementation",
                "acceptance_refs": ["example-unconfirmed-dispatch"],
                "decision_refs": ["decision-confirm-boundary"],
                "scenario_refs": ["example-unconfirmed-dispatch"],
                "metadata": {"estimated_size": "M"},
            }
        ],
    )

    assert any(
        event.type == TaskflowEventType.DISCOVERY_TURN_RECORDED
        for event in state.events
    )
    assert state.outputs.current_brief_version == 1

    state = service.answer_decision(
        state.meta.taskflow_id,
        decision_id="decision-confirm-boundary",
        selected_option_id="brief",
        answer="brief",
        rationale="Brief confirmation is the clean execution boundary.",
        actor="user",
    )

    assert state.design.local_decisions[0].chosen == "brief"
    assert state.outputs.current_brief_version == 2
    assert any(event.type == TaskflowEventType.DECISION_ANSWERED for event in state.events)

    with pytest.raises(ValueError, match="confirmed brief"):
        service.compile_goal(state.meta.taskflow_id)

    ready = service.mark_brief_ready(state.meta.taskflow_id, actor="agent")
    assert ready.outputs.brief_versions[-1].status == BriefStatus.READY

    confirmed = service.confirm_brief(state.meta.taskflow_id, version=2, actor="user")
    assert confirmed.outputs.confirmed_brief_version == 2
    assert any(event.type == TaskflowEventType.BRIEF_CONFIRMED for event in confirmed.events)

    plan = service.compile_goal(state.meta.taskflow_id)
    compiled = plan.work_item_candidates[0]

    assert compiled.acceptance_refs == ["example-unconfirmed-dispatch"]
    assert compiled.decision_refs == ["decision-confirm-boundary"]
    assert compiled.metadata["acceptance"]["source_brief_version"] == 2
    assert compiled.metadata["acceptance"]["rule_ids"] == [
        "rule-confirm-before-dispatch"
    ]
    assert compiled.metadata["acceptance"]["observable_outputs"] == ["runtime_event"]


def test_brief_snapshot_is_immutable_and_compile_requires_fresh_confirmation() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Protect brief history.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    state = service.record_discovery_turn(
        state.meta.taskflow_id,
        rules=[
            {
                "id": "rule-immutable",
                "statement": "Brief snapshots keep original rule text.",
                "status": "confirmed",
            }
        ],
        examples=[
            {
                "id": "example-immutable",
                "rule_id": "rule-immutable",
                "title": "Brief v1 contains original text",
                "then": ["The stored rule text is unchanged"],
            }
        ],
        work_item_candidates=[
            {
                "id": "candidate-immutable",
                "title": "Implement immutable brief",
                "description": "Store full brief snapshots.",
                "acceptance_refs": ["example-immutable"],
            }
        ],
    )
    first_hash = state.outputs.brief_versions[-1].content_hash
    service.mark_brief_ready(state.meta.taskflow_id)
    confirmed = service.confirm_brief(state.meta.taskflow_id, version=1)
    service.compile_goal(state.meta.taskflow_id)

    changed = service.record_discovery_turn(
        confirmed.meta.taskflow_id,
        rules=[
            {
                "id": "rule-immutable",
                "statement": "Brief snapshots keep revised rule text.",
                "status": "confirmed",
            }
        ],
    )

    assert changed.outputs.brief_versions[0].rules[0]["statement"] == (
        "Brief snapshots keep original rule text."
    )
    assert changed.outputs.brief_versions[-1].content_hash != first_hash
    assert "rules" in changed.outputs.brief_versions[-1].diff_summary["changed_sections"]
    with pytest.raises(ValueError, match="current brief version must be confirmed"):
        service.compile_goal(state.meta.taskflow_id)


def test_review_card_answer_records_decision_answer_and_new_brief_version() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Review decision card.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        decisions=[
            {
                "id": "decision-review",
                "question": "Which confirmation boundary should be used?",
                "options": [{"id": "brief", "label": "Brief"}],
                "recommended": "brief",
            }
        ],
    )

    answer = service.answer_review_card(
        state.meta.taskflow_id,
        card_id=f"{state.meta.taskflow_id}:decision:decision-review",
        action="accept_recommendation",
        actor="user",
    )
    updated = service.get_taskflow_state(state.meta.taskflow_id)

    assert answer.card_id.endswith("decision-review")
    assert updated.outputs.review_card_answers[-1].id == answer.id
    assert updated.design.local_decisions[0].answer_refs
    assert updated.outputs.current_brief_version == 2
    assert any(event.type == TaskflowEventType.REVIEW_CARD_ANSWERED for event in updated.events)
    assert any(event.type == TaskflowEventType.DECISION_ANSWERED for event in updated.events)


def test_high_risk_question_blocks_compile_until_answered() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Build API with unknown data migration.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        questions=[
            {
                "id": "question-data-migration",
                "stage": "risk",
                "question": "Does this require a data migration?",
                "why_needed": "Migration changes rollback planning.",
                "risk_if_unknown": "high",
                "blocking_scope": "compile",
                "blocks_compile": True,
            }
        ],
        work_item_candidates=[
            {
                "id": "candidate-1",
                "title": "Implement API",
                "description": "Build the API after migration scope is known.",
                "acceptance_refs": ["example-api"],
            }
        ],
        examples=[
            {
                "id": "example-api",
                "title": "API accepted",
                "then": ["API behavior is testable"],
            }
        ],
    )
    service.mark_brief_ready(state.meta.taskflow_id)
    service.confirm_brief(state.meta.taskflow_id, version=1)

    with pytest.raises(ValueError, match="unresolved high-risk"):
        service.compile_goal(state.meta.taskflow_id)

    answered = service.answer_question(
        state.meta.taskflow_id,
        question_id="question-data-migration",
        answer="No migration is required.",
        actor="user",
    )
    service.mark_brief_ready(state.meta.taskflow_id)
    service.confirm_brief(
        state.meta.taskflow_id,
        version=answered.outputs.current_brief_version,
    )

    plan = service.compile_goal(state.meta.taskflow_id)

    assert plan.work_item_candidates[0].title == "Implement API"


def test_dangling_acceptance_ref_blocks_compile() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Reject dangling acceptance references.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        work_item_candidates=[
            {
                "id": "candidate-1",
                "title": "Implement dangling work",
                "description": "This candidate points at a missing example.",
                "acceptance_refs": ["missing-example"],
            }
        ],
    )
    service.mark_brief_ready(state.meta.taskflow_id)
    service.confirm_brief(state.meta.taskflow_id, version=1)

    with pytest.raises(ValueError, match="acceptance-coverage"):
        service.compile_goal(state.meta.taskflow_id)


def test_explicit_candidates_keep_scenario_trace_and_generate_risk_spike() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Compile traceable risk work.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        rules=[
            {
                "id": "rule-trace",
                "statement": "Trace all acceptance and scenario refs.",
                "status": "confirmed",
                "example_ids": ["example-trace"],
                "scenario_ids": ["scenario-trace"],
            }
        ],
        examples=[
            {
                "id": "example-trace",
                "rule_id": "rule-trace",
                "title": "Acceptance ref is traced",
                "observable_outputs": ["trace_link"],
            }
        ],
        scenarios=[
            {
                "id": "scenario-trace",
                "title": "Scenario ref is traced",
                "then": ["Scenario has a trace link"],
            }
        ],
        work_item_candidates=[
            {
                "id": "candidate-trace",
                "title": "Implement traceable work",
                "description": "Keep all trace refs.",
                "acceptance_refs": ["example-trace"],
                "scenario_refs": ["scenario-trace"],
                "metadata": {"estimated_size": "S"},
            }
        ],
    )
    stored = service.get_taskflow_state(state.meta.taskflow_id)
    stored.design.risks.append(
        RiskRecord(
            id="risk-unknown",
            statement="Unknown migration behavior",
            impact="high",
            mitigation="Run a spike before implementation.",
        )
    )
    service.save_taskflow_state(stored)
    service.compile_brief_draft(state.meta.taskflow_id)
    service.mark_brief_ready(state.meta.taskflow_id)
    refreshed = service.get_taskflow_state(state.meta.taskflow_id)
    service.confirm_brief(state.meta.taskflow_id, version=refreshed.outputs.current_brief_version)

    plan = service.compile_goal(state.meta.taskflow_id)

    assert any(item.risk_refs == ["risk-unknown"] for item in plan.work_item_candidates)
    assert any(item.type == WorkItemType.SPIKE.value for item in plan.work_item_candidates)
    project = service.project_service.get_project_state("project-1")
    assert project is not None
    assert any(
        link.source_type == TraceEntityType.EXAMPLE and link.source_id == "example-trace"
        for link in project.traceability.decision_links
    )
    assert any(
        link.source_type == TraceEntityType.SCENARIO and link.source_id == "scenario-trace"
        for link in project.traceability.decision_links
    )


def test_dispatch_can_reconstruct_plan_from_persisted_state_after_service_rebuild() -> None:
    project_service = ProjectService()
    project_service.save_project_state(
        ProjectState.new(project_id="project-1", name="Taskflow")
    )
    service = TaskflowService(project_service=project_service)
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Dispatch after service rebuild.",
        taskflow_id="taskflow-1",
        goal_id="goal-1",
    )
    service.record_discovery_turn(
        state.meta.taskflow_id,
        work_item_candidates=[
            {
                "id": "candidate-1",
                "title": "Implement resumable dispatch",
                "description": "Dispatch can reconstruct the compiled plan.",
                "acceptance_refs": ["acceptance-resume"],
            }
        ],
        examples=[
            {
                "id": "acceptance-resume",
                "title": "Dispatch resume accepted",
                "then": ["A TaskRun is created"],
            }
        ],
    )
    service.confirm_goal(state.meta.taskflow_id)
    plan = service.compile_goal(state.meta.taskflow_id)
    dispatch_decision = service.request_dispatch_decision(
        state.meta.taskflow_id,
        work_item_ids=[plan.work_item_candidates[0].work_item_id],
        actor="user",
    )
    service.confirm_dispatch_decision(
        state.meta.taskflow_id,
        decision_id=dispatch_decision.id,
        actor="user",
    )

    rebuilt = TaskflowService(
        project_service=project_service,
        state_store=service.state_store,
    )
    run = rebuilt.dispatch_task_run(
        state.meta.taskflow_id,
        work_item_id=plan.work_item_candidates[0].work_item_id,
        dispatch_decision_id=dispatch_decision.id,
    )

    assert run.work_item_id == plan.work_item_candidates[0].work_item_id
    assert run.dispatch_ref_id == dispatch_decision.id


def test_plugin_goal_gets_evidence_based_l1_complexity_floor() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="我要把 understand anything 做成个 astrbot 插件",
        taskflow_id="taskflow-plugin",
        goal_id="goal-plugin",
    )

    estimate = state.compiler.complexity_estimate

    assert estimate is not None
    assert estimate.level == "L1"
    assert estimate.level_floor == "L1"
    assert "external-integration-floor" in estimate.hard_escalations
    assert any(
        evidence.dimension == "interface_impact"
        and evidence.source_path == "intent.goal_statement"
        for evidence in estimate.evidence
    )
    assert "scope" in estimate.required_artifacts


def test_complexity_evidence_drives_recipe_artifacts_and_readiness_gates() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Build API plugin with migration and rollout risk.",
        taskflow_id="taskflow-complexity",
        goal_id="goal-complexity",
    )
    state = service.record_discovery_turn(
        state.meta.taskflow_id,
        success_criteria=["Plugin responds through the public API"],
        examples=[
            {
                "id": "example-public-api",
                "title": "Public API accepted",
                "then": ["The API returns the plugin result"],
                "observable_outputs": ["tests"],
            }
        ],
        work_item_candidates=[
            {
                "id": "candidate-api",
                "title": "Implement public plugin API",
                "description": "Expose the plugin through a public API.",
                "acceptance_refs": ["example-public-api"],
            }
        ],
        complexity_evidence=[
            {
                "id": "evidence-public-api",
                "dimension": "interface_impact",
                "source_type": "goal",
                "source_id": "goal-complexity",
                "score_delta": 2,
                "rationale": "Public API contract affects consumers.",
            },
            {
                "id": "evidence-migration",
                "dimension": "data_impact",
                "source_type": "risk",
                "source_id": "risk-migration",
                "score_delta": 2,
                "rationale": "Schema migration has unknown rollback behavior.",
            },
            {
                "id": "evidence-rollout",
                "dimension": "ops_impact",
                "source_type": "risk",
                "source_id": "risk-rollout",
                "score_delta": 2,
                "rationale": "Production rollout requires monitoring and rollback.",
            },
        ],
    )

    estimate = state.compiler.complexity_estimate
    gate_by_name = {gate.name: gate for gate in state.compiler.readiness_gates}

    assert estimate is not None
    assert estimate.level in {"L2", "L3"}
    assert "public-interface-floor" in estimate.hard_escalations
    assert "data-migration-floor" in estimate.hard_escalations
    assert {"api_contract", "consumer_impact"}.issubset(estimate.required_artifacts)
    assert {"migration_plan", "rollback_plan", "data_validation"}.issubset(
        estimate.required_artifacts
    )
    assert {"rollout_plan", "runbook", "monitoring_signal"}.issubset(
        estimate.required_artifacts
    )
    assert gate_by_name["api-contract"].phase == "compile"
    assert gate_by_name["migration-plan"].blocks_compile is True
    assert gate_by_name["runbook"].source == "complexity"
    assert state.compiler.compile_readiness_score < 100


def test_complexity_override_records_rationale_and_versions_brief() -> None:
    service = _service()
    state = service.start_taskflow(
        project_id="project-1",
        raw_goal="Small looking task with hidden governance impact.",
        taskflow_id="taskflow-override",
        goal_id="goal-override",
    )
    before = state.outputs.current_brief_version

    updated = service.override_complexity(
        state.meta.taskflow_id,
        level="L3",
        reason="User knows this crosses governance and production rollout boundaries.",
        actor="architect",
    )
    estimate = updated.compiler.complexity_estimate

    assert estimate is not None
    assert estimate.level == "L3"
    assert estimate.overridden_by == "architect"
    assert "governance" in estimate.override_reason
    assert updated.outputs.current_brief_version != before
    assert updated.outputs.brief_versions[-1].complexity_estimate["level"] == "L3"
