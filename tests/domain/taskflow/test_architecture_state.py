from __future__ import annotations

from labrastro_server.taskflow.domain.complexity import ComplexityEstimator, ComplexityLevel
from labrastro_server.taskflow.compiler.plan_compiler import PlanCompiler
from labrastro_server.taskflow.domain.project_state import (
    ProjectState,
    WorkItem,
    WorkItemStatus,
    WorkItemType,
)
from labrastro_server.taskflow.interaction.review_cards import CardRenderer
from labrastro_server.taskflow.domain.taskflow_state import (
    Assumption,
    ConfirmationState,
    DecisionRecord,
    RiskRecord,
    ScenarioRecord,
    TaskflowState,
    WorkItemCandidate,
)


def test_project_state_contains_long_lived_cross_goal_knowledge() -> None:
    state = ProjectState.new(project_id="project-1", name="Taskflow")
    state.project_profile.background = "AI coding assistant task planning backend."
    state.knowledge_base.ubiquitous_language["WorkItem"] = (
        "Reusable normalized work definition."
    )
    state.decisions.project_decisions.append(
        DecisionRecord(
            id="decision-project-state",
            topic="State source of truth",
            rationale="State is authoritative; generated documents are projections.",
            state=ConfirmationState.CONFIRMED_BY_USER,
        )
    )

    restored = ProjectState.from_dict(state.to_dict())

    assert restored.project_id == "project-1"
    assert restored.project_profile.background.startswith("AI coding assistant")
    assert restored.knowledge_base.ubiquitous_language["WorkItem"] == (
        "Reusable normalized work definition."
    )
    assert restored.decisions.project_decisions[0].id == "decision-project-state"
    assert hasattr(restored.work_items, "active_work_items")
    assert hasattr(restored.traceability, "goal_links")
    assert hasattr(restored.projections, "runbooks")


def test_taskflow_state_contains_single_goal_compiler_snapshot_sections() -> None:
    state = TaskflowState.new(
        taskflow_id="taskflow-1",
        project_id="project-1",
        goal_id="goal-1",
        goal_statement="Build the Taskflow compiler skeleton.",
    )
    state.clarification.assumptions.append(
        Assumption(
            id="assumption-1",
            statement="Generated documents are projections, not source state.",
            impact="high",
            state=ConfirmationState.SUGGESTED_BY_SYSTEM,
            reason="Prevents document templates becoming the source of truth.",
        )
    )
    state.compiler.readiness_score = 82

    restored = TaskflowState.from_dict(state.to_dict())

    assert restored.meta.taskflow_id == "taskflow-1"
    assert restored.meta.project_id == "project-1"
    assert restored.intent.goal_statement == "Build the Taskflow compiler skeleton."
    assert restored.clarification.assumptions[0].state == (
        ConfirmationState.SUGGESTED_BY_SYSTEM
    )
    assert hasattr(restored.refs, "reused_work_item_refs")
    assert hasattr(restored.domain, "bounded_context_refs")
    assert hasattr(restored.relations, "goal_work_links")
    assert hasattr(restored.outputs, "work_item_candidates")


def test_complexity_estimator_selects_risk_driven_recipe() -> None:
    estimate = ComplexityEstimator().estimate(
        {
            "goal_clarity": 3,
            "business_impact": 3,
            "user_count": 2,
            "domain_complexity": 3,
            "technical_risk": 3,
            "interface_impact": 2,
            "data_impact": 2,
            "ops_impact": 2,
            "reversibility": 2,
            "org_collaboration": 1,
        }
    )

    assert estimate.level == ComplexityLevel.L3
    assert estimate.recipe_id == "complex-project"
    assert "sbe_bdd" in estimate.recipe_steps
    assert "compile_plan" in estimate.recipe_steps


def test_card_renderer_projects_assumptions_scope_and_decisions() -> None:
    state = TaskflowState.new(
        taskflow_id="taskflow-1",
        project_id="project-1",
        goal_id="goal-1",
        goal_statement="Build review card rendering.",
    )
    state.intent.scope_in = ["AssumptionCard", "ScopeCard", "DecisionCard"]
    state.intent.scope_out = ["Long-form PRD review"]
    state.intent.deferred_scope = ["Full artifact projector"]
    state.clarification.assumptions.append(
        Assumption(
            id="assumption-1",
            statement="The user confirms cards instead of reading full documents.",
            impact="high",
            state=ConfirmationState.SUGGESTED_BY_SYSTEM,
            reason="Matches Taskflow UX principles.",
        )
    )
    state.design.local_decisions.append(
        DecisionRecord(
            id="decision-1",
            topic="Card data shape",
            options=["dict-only", "typed-card"],
            recommended="typed-card",
            rationale="Typed cards keep UI adapters stable.",
            state=ConfirmationState.SUGGESTED_BY_SYSTEM,
        )
    )

    cards = CardRenderer().render(state)

    assert [card.card_type for card in cards] == [
        "assumption",
        "scope",
        "decision",
    ]
    assert cards[0].items[0]["id"] == "assumption-1"
    assert cards[1].items[0]["included"] == ["AssumptionCard", "ScopeCard", "DecisionCard"]
    assert cards[2].items[0]["recommended"] == "typed-card"


def test_plan_compiler_reuses_existing_work_items_and_preserves_traceability() -> None:
    project = ProjectState.new(project_id="project-1", name="Taskflow")
    existing = WorkItem(
        id="work-existing",
        project_id="project-1",
        title="Implement review card renderer",
        description="Render TaskflowState into review cards.",
        type=WorkItemType.IMPLEMENTATION,
        status=WorkItemStatus.READY,
        acceptance_refs=["acceptance-1"],
        decision_refs=["decision-1"],
        dedupe_key="project-1:implementation:review-card-renderer",
    )
    project.work_items.reusable_work_items.append(existing)

    state = TaskflowState.new(
        taskflow_id="taskflow-1",
        project_id="project-1",
        goal_id="goal-1",
        goal_statement="Build review card rendering.",
    )
    state.clarification.scenarios.append(
        ScenarioRecord(
            id="scenario-1",
            title="User reviews generated cards",
            given=["TaskflowState has assumptions and decisions"],
            when=["Review cards are requested"],
            then=["Cards include assumptions, scope, and decisions"],
        )
    )
    state.design.local_decisions.append(
        DecisionRecord(
            id="decision-1",
            topic="Use typed review cards",
            chosen="typed-card",
            rationale="Preserves UI contract.",
            state=ConfirmationState.CONFIRMED_BY_USER,
        )
    )
    state.design.risks.append(
        RiskRecord(
            id="risk-1",
            statement="Cards can hide too much context.",
            impact="medium",
            mitigation="Keep trace refs on every card item.",
        )
    )
    state.outputs.work_item_candidates.append(
        WorkItemCandidate(
            id="candidate-1",
            title="Implement review card renderer",
            description="Render TaskflowState into review cards.",
            type=WorkItemType.IMPLEMENTATION,
            acceptance_refs=["acceptance-1"],
            decision_refs=["decision-1"],
            scenario_refs=["scenario-1"],
            risk_refs=["risk-1"],
            dedupe_key="project-1:implementation:review-card-renderer",
        )
    )

    draft = PlanCompiler().compile(state, project)

    assert draft.work_item_candidates[0].action == "reuse"
    assert draft.work_item_candidates[0].reuse_work_item_id == "work-existing"
    assert draft.goal_work_links[0].goal_id == "goal-1"
    assert draft.goal_work_links[0].work_item_id == "work-existing"
    assert draft.decision_work_links[0].source_id == "decision-1"
    assert draft.acceptance_trace_links[0].target_id == "work-existing"
