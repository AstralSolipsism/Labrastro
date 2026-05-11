"""Extract complexity signals from TaskflowState and ProjectState."""

from __future__ import annotations

from dataclasses import dataclass, field

from labrastro_server.taskflow.domain.project_state import ProjectState
from labrastro_server.taskflow.domain.taskflow_state import (
    ComplexityEvidenceRecord,
    TaskflowState,
)


@dataclass(frozen=True, slots=True)
class ComplexitySignals:
    """Signals passed into ComplexityEstimator plus evidence for persistence."""

    scores: dict[str, int]
    evidence: dict[str, list[str]] = field(default_factory=dict)


class ComplexitySignalExtractor:
    """Derive risk and ambiguity scores from structured Taskflow facts."""

    def extract(self, state: TaskflowState, project: ProjectState) -> list[ComplexityEvidenceRecord]:
        evidence: list[ComplexityEvidenceRecord] = []

        def add(
            dimension: str,
            source_type: str,
            source_id: str,
            score_delta: int,
            rationale: str,
            *,
            source_path: str = "",
            confidence: float | None = None,
            metadata: dict[str, object] | None = None,
        ) -> None:
            evidence.append(
                ComplexityEvidenceRecord(
                    id=f"complexity-{dimension}-{len(evidence) + 1}",
                    dimension=dimension,
                    source_type=source_type,
                    source_id=source_id,
                    source_path=source_path,
                    score_delta=score_delta,
                    confidence=confidence,
                    rationale=rationale,
                    brief_version=state.outputs.current_brief_version,
                    extracted_by="system",
                    metadata={str(key): value for key, value in (metadata or {}).items()},
                )
            )

        unresolved_high = state.unresolved_high_risk_questions()
        if not state.intent.success_criteria:
            add(
                "goal_clarity",
                "goal",
                state.meta.goal_id,
                1,
                "Missing success criteria.",
                source_path="intent.success_criteria",
            )
        if unresolved_high:
            for question in unresolved_high:
                add(
                    "goal_clarity",
                    "open_question",
                    question.id,
                    2,
                    "Unresolved high-risk question.",
                    source_path="clarification.open_questions",
                )
        unresolved_decisions = [
            decision
            for decision in state.design.local_decisions
            if not decision.chosen and not decision.answer_refs
        ]
        for decision in unresolved_decisions:
            add(
                "goal_clarity",
                "decision",
                decision.id,
                1,
                "Unresolved decision.",
                source_path="design.local_decisions",
            )

        if not (
            state.clarification.examples
            or state.clarification.acceptance_examples
            or state.clarification.scenarios
        ):
            add(
                "acceptance_quality",
                "goal",
                state.meta.goal_id,
                1,
                "No acceptance examples or scenarios recorded.",
                source_path="clarification",
            )

        goal_text = state.intent.goal_statement.lower()
        if any(
            token in goal_text
            for token in ("plugin", "插件", "extension", "integration", "api", "webhook")
        ):
            add(
                "interface_impact",
                "goal",
                state.meta.goal_id,
                1,
                "Plugin or integration goal touches an external interface.",
                source_path="intent.goal_statement",
                confidence=0.6,
                metadata={"heuristic": "integration-token"},
            )

        if state.domain.bounded_context_refs:
            add(
                "domain_complexity",
                "domain",
                state.meta.taskflow_id,
                1,
                "Bounded context refs are present.",
                source_path="domain.bounded_context_refs",
            )
        if state.domain.domain_model_delta or state.clarification.rules:
            add(
                "domain_complexity",
                "domain",
                state.meta.taskflow_id,
                1,
                "Domain delta or rules are present.",
                source_path="domain",
            )
        if len(state.clarification.rules) >= 3:
            add(
                "domain_complexity",
                "rule",
                "rules",
                1,
                "Multiple rules increase domain complexity.",
                source_path="clarification.rules",
            )
        if any(rule.risk_level in {"high", "critical"} for rule in state.clarification.rules):
            add(
                "domain_complexity",
                "rule",
                "rules",
                1,
                "High-risk rule.",
                source_path="clarification.rules",
                metadata={"core_high_risk": len(state.clarification.rules) >= 3},
            )

        high_risks = [
            risk
            for risk in state.design.risks
            if str(risk.impact).lower() in {"high", "critical"}
        ]
        for risk in high_risks:
            add(
                "technical_risk",
                "risk",
                risk.id,
                2,
                "High impact risk.",
                source_path="design.risks",
                metadata={"multi_repo": len(project.project_profile.repositories) > 1},
            )
        for decision in unresolved_decisions:
            add(
                "technical_risk",
                "decision",
                decision.id,
                1,
                "Decision is pending.",
                source_path="design.local_decisions",
            )
        for item in state.outputs.work_item_candidates:
            if item.type.value in {"migration", "ops"}:
                add(
                    "technical_risk",
                    "work_item_candidate",
                    item.id,
                    1,
                    "Migration or ops work item.",
                    source_path="outputs.work_item_candidates",
                )

        interface_consumers = sum(len(interface.consumers) for interface in state.design.interfaces)
        for interface in state.design.interfaces:
            add(
                "interface_impact",
                "interface",
                interface.id,
                1,
                "Interface spec is present.",
                source_path="design.interfaces",
                metadata={
                    "contract": "present" if interface.contract else "unknown",
                    "multiple_consumers": len(interface.consumers) > 1,
                },
            )
        if interface_consumers > 1:
            add(
                "interface_impact",
                "interface",
                "consumers",
                1,
                "Multiple interface consumers.",
                source_path="design.interfaces.consumers",
                metadata={"multiple_consumers": True},
            )

        for constraint in state.intent.constraints_delta:
            if "schema" in constraint.statement.lower() or "data" in constraint.statement.lower():
                add(
                    "data_impact",
                    "constraint",
                    constraint.id,
                    2,
                    "Data or schema constraint.",
                    source_path="intent.constraints_delta",
                )
        for risk in state.design.risks:
            if any(token in risk.statement.lower() for token in ("data", "schema", "migration")):
                add(
                    "data_impact",
                    "risk",
                    risk.id,
                    2,
                    "Data, schema, or migration risk.",
                    source_path="design.risks",
                )
            if any(token in risk.statement.lower() for token in ("ops", "rollout", "deploy", "runbook")):
                add(
                    "ops_impact",
                    "risk",
                    risk.id,
                    1,
                    "Operational risk.",
                    source_path="design.risks",
                )
        for item in state.outputs.work_item_candidates:
            if item.type.value == "migration":
                add(
                    "data_impact",
                    "work_item_candidate",
                    item.id,
                    1,
                    "Migration work item.",
                    source_path="outputs.work_item_candidates",
                )
            if item.type.value == "ops":
                add(
                    "ops_impact",
                    "work_item_candidate",
                    item.id,
                    1,
                    "Ops work item.",
                    source_path="outputs.work_item_candidates",
                )
        if state.delivery.rollout_plan:
            add(
                "ops_impact",
                "delivery",
                state.meta.taskflow_id,
                1,
                "Rollout plan is present.",
                source_path="delivery.rollout_plan",
            )

        add(
            "business_impact",
            "goal",
            state.meta.goal_id,
            1,
            "Baseline business impact for a requested goal.",
            source_path="intent.goal_statement",
        )
        if len(state.intent.stakeholders_delta) + len(project.project_profile.stakeholders) > 1:
            add(
                "business_impact",
                "project",
                project.meta.project_id,
                1,
                "Multiple stakeholders.",
                source_path="project.project_profile.stakeholders",
            )

        if state.intent.stakeholders_delta:
            add(
                "user_count",
                "goal",
                state.meta.goal_id,
                1,
                "Stakeholders are recorded.",
                source_path="intent.stakeholders_delta",
            )
        if len(state.intent.stakeholders_delta) > 2:
            add(
                "user_count",
                "goal",
                state.meta.goal_id,
                1,
                "More than two stakeholders.",
                source_path="intent.stakeholders_delta",
            )

        if len(project.project_profile.repositories) > 1:
            add(
                "org_collaboration",
                "project",
                project.meta.project_id,
                1,
                "Multiple repositories.",
                source_path="project.project_profile.repositories",
            )

        if any(item.dimension == "data_impact" and item.score_delta >= 2 for item in evidence) or any(
            item.dimension == "ops_impact" and item.score_delta >= 2 for item in evidence
        ):
            add(
                "reversibility",
                "assessment",
                state.meta.taskflow_id,
                2,
                "Data or ops impact can make rollback harder.",
                source_path="compiler.complexity_estimate",
            )

        return evidence


__all__ = ["ComplexitySignalExtractor", "ComplexitySignals"]
