"""Formal complexity assessment for Taskflow."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from labrastro_server.taskflow.domain.complexity import (
    ComplexityEstimate,
    ComplexityEstimator,
    ComplexityLevel,
    RecipePolicyRegistry,
)
from labrastro_server.taskflow.domain.complexity_signals import (
    ComplexitySignalExtractor,
)
from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.project_state import ProjectState
from labrastro_server.taskflow.domain.taskflow_state import (
    ComplexityEstimateRecord,
    ComplexityEvidenceRecord,
    TaskflowEventType,
    TaskflowState,
)


class ComplexityAssessmentService:
    """Build the auditable complexity estimate used by recipe and gates."""

    def __init__(
        self,
        *,
        extractor: ComplexitySignalExtractor | None = None,
        estimator: ComplexityEstimator | None = None,
        recipe_registry: RecipePolicyRegistry | None = None,
    ) -> None:
        self.extractor = extractor or ComplexitySignalExtractor()
        self.estimator = estimator or ComplexityEstimator()
        self.recipe_registry = recipe_registry or RecipePolicyRegistry()
        self.dimensions = set(self.estimator.dimensions)

    def record_evidence(
        self,
        state: TaskflowState,
        evidence: Iterable[ComplexityEvidenceRecord | dict[str, object]],
        *,
        actor: str = "agent",
    ) -> list[ComplexityEvidenceRecord]:
        parsed: list[ComplexityEvidenceRecord] = []
        for item in evidence:
            record = (
                item
                if isinstance(item, ComplexityEvidenceRecord)
                else ComplexityEvidenceRecord.from_dict(dict(item))
            )
            record = self._normalize_evidence(record, actor=actor, state=state)
            parsed.append(record)
        self._upsert_evidence(state, parsed)
        for record in parsed:
            append_taskflow_event(
                state,
                TaskflowEventType.COMPLEXITY_EVIDENCE_RECORDED,
                actor=actor,
                payload=record.to_dict(),
            )
        return parsed

    def override(
        self,
        state: TaskflowState,
        *,
        level: str,
        reason: str,
        actor: str,
    ) -> None:
        resolved = self._level(level)
        state.compiler.complexity_override_level = resolved.name
        state.compiler.complexity_overridden_by = actor
        state.compiler.complexity_override_reason = reason
        append_taskflow_event(
            state,
            TaskflowEventType.COMPLEXITY_OVERRIDDEN,
            actor=actor,
            payload={"level": resolved.name, "reason": reason},
        )

    def assess(
        self, state: TaskflowState, project: ProjectState
    ) -> ComplexityEstimateRecord:
        evidence = self._merged_evidence(state, project)
        estimate = self.estimator.estimate(evidence)
        estimate = self._apply_override(state, estimate)
        policy = self.recipe_registry.policy_for(estimate)
        completed_steps = [
            step for step in policy.steps if self._step_completed(state, step)
        ]
        skipped_steps = [
            step
            for step in policy.steps
            if step.endswith("_if_needed") and self._step_completed(state, step)
        ]
        signal_evidence: dict[str, list[str]] = {}
        for item in evidence:
            signal_evidence.setdefault(item.dimension, []).append(item.rationale)
        record = ComplexityEstimateRecord(
            level=estimate.level.name,
            score=estimate.score,
            recipe_id=policy.recipe_id,
            dimension_scores=dict(estimate.dimension_scores),
            signal_evidence=signal_evidence,
            evidence=list(evidence),
            dimension_details=list(estimate.dimension_details),
            confidence=estimate.confidence,
            dominant_dimensions=list(estimate.dominant_dimensions),
            unknown_dimensions=list(estimate.unknown_dimensions),
            needs_more_evidence=estimate.needs_more_evidence,
            explanation=estimate.explanation,
            scan_refs=list(estimate.scan_refs),
            question_packs=list(estimate.question_packs),
            plan_slicing_policy=estimate.plan_slicing_policy,
            dispatch_safety_policy=estimate.dispatch_safety_policy,
            level_floor=estimate.level_floor,
            hard_escalations=list(estimate.hard_escalations),
            recipe_policy_id=policy.policy_id,
            required_gates=list(policy.required_gates),
            required_steps=list(policy.steps),
            completed_steps=completed_steps,
            skipped_steps=skipped_steps,
            required_artifacts=list(policy.required_artifacts),
            rationale=estimate.explanation
            or f"Complexity score {estimate.score} selected {policy.recipe_id}.",
            overridden_by=state.compiler.complexity_overridden_by,
            override_reason=state.compiler.complexity_override_reason,
        )
        state.compiler.complexity_level = record.level
        state.compiler.recipe_id = record.recipe_id
        state.compiler.complexity_score = record.score
        state.compiler.recipe_steps = list(record.required_steps)
        state.compiler.required_steps = list(record.required_steps)
        state.compiler.completed_steps = list(record.completed_steps)
        state.compiler.skipped_steps = list(record.skipped_steps)
        state.compiler.required_artifacts = list(record.required_artifacts)
        state.compiler.complexity_rationale = record.rationale
        state.compiler.complexity_estimate = record
        state.compiler.traceability_index["recipe_steps"] = list(record.required_steps)
        return record

    def _merged_evidence(
        self, state: TaskflowState, project: ProjectState
    ) -> list[ComplexityEvidenceRecord]:
        merged: dict[str, ComplexityEvidenceRecord] = {}
        for record in self.extractor.extract(state, project):
            merged[record.id] = record
        for record in state.compiler.complexity_evidence:
            normalized = self._normalize_evidence(record, actor=record.extracted_by, state=state)
            merged[normalized.id] = normalized
        return list(merged.values())

    def _upsert_evidence(
        self, state: TaskflowState, records: list[ComplexityEvidenceRecord]
    ) -> None:
        existing = {record.id: index for index, record in enumerate(state.compiler.complexity_evidence)}
        for record in records:
            if record.id in existing:
                state.compiler.complexity_evidence[existing[record.id]] = record
            else:
                state.compiler.complexity_evidence.append(record)

    def _normalize_evidence(
        self,
        record: ComplexityEvidenceRecord,
        *,
        actor: str,
        state: TaskflowState,
    ) -> ComplexityEvidenceRecord:
        if record.dimension not in self.dimensions:
            raise ValueError(f"invalid complexity dimension: {record.dimension}")
        if not record.source_type:
            raise ValueError("complexity evidence source_type is required")
        return ComplexityEvidenceRecord(
            id=record.id or f"complexity-evidence-{uuid.uuid4().hex}",
            dimension=record.dimension,
            source_type=record.source_type,
            source_id=record.source_id,
            source_path=record.source_path,
            score_delta=max(0, min(3, int(record.score_delta))),
            confidence=record.confidence,
            rationale=record.rationale,
            brief_version=record.brief_version or state.outputs.current_brief_version,
            extracted_by=record.extracted_by or actor,
            metadata=dict(record.metadata),
            created_at=record.created_at,
        )

    def _apply_override(
        self, state: TaskflowState, estimate: ComplexityEstimate
    ) -> ComplexityEstimate:
        if not state.compiler.complexity_override_level:
            return estimate
        level = self._level(state.compiler.complexity_override_level)
        recipe_id, steps = self.estimator.recipes[level]
        hard_escalations = list(estimate.hard_escalations)
        if "manual-override" not in hard_escalations:
            hard_escalations.append("manual-override")
        return ComplexityEstimate(
            level=level,
            score=estimate.score,
            recipe_id=recipe_id,
            recipe_steps=list(steps),
            dimension_scores=dict(estimate.dimension_scores),
            dimension_details=list(estimate.dimension_details),
            confidence=estimate.confidence,
            dominant_dimensions=list(estimate.dominant_dimensions),
            unknown_dimensions=list(estimate.unknown_dimensions),
            needs_more_evidence=estimate.needs_more_evidence,
            explanation=estimate.explanation,
            scan_refs=list(estimate.scan_refs),
            question_packs=list(estimate.question_packs),
            plan_slicing_policy=estimate.plan_slicing_policy,
            dispatch_safety_policy=estimate.dispatch_safety_policy,
            level_floor=estimate.level_floor,
            hard_escalations=hard_escalations,
        )

    def _level(self, value: str) -> ComplexityLevel:
        normalized = str(value).strip()
        for level in ComplexityLevel:
            if normalized in {level.name, level.value}:
                return level
        raise ValueError(f"invalid complexity level: {value}")

    def _step_completed(self, state: TaskflowState, step: str) -> bool:
        if step in {"goal", "goal_confirm", "prd_lite"}:
            return bool(state.intent.goal_statement)
        if step == "scope":
            return bool(state.intent.scope_in or state.intent.scope_out)
        if step in {"acceptance", "acceptance_check", "acceptance_examples"}:
            return bool(
                state.clarification.acceptance_examples
                or state.clarification.examples
                or any(item.acceptance_refs for item in state.outputs.work_item_candidates)
            )
        if step in {"scenarios", "sbe_bdd"}:
            return bool(state.clarification.scenarios or state.clarification.examples)
        if step in {"decision_if_needed", "adr", "rfc_compare"}:
            return bool(state.design.local_decisions)
        if step in {"tech_impact", "tech_spec"}:
            return bool(state.design.risks or state.design.implementation_notes)
        if step == "api_spec_if_needed":
            return bool(state.design.interfaces)
        if step == "runbook_if_needed":
            return bool(state.delivery.rollout_plan)
        if step == "ddd_lite":
            return bool(state.domain.domain_model_delta or state.domain.ubiquitous_language)
        if step in {"compile_task", "compile_plan"}:
            return bool(state.outputs.work_item_candidates)
        return False


__all__ = ["ComplexityAssessmentService"]
