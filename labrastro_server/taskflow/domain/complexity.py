"""Taskflow complexity scoring and recipe selection.

Source: ``docs/方向建议.md`` Section 2.2 and Section 2.3. Complexity is the
first routing decision: light goals should not be dragged through heavy
discovery, and risky goals should not compile before enough clarification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping

from labrastro_server.taskflow.domain.taskflow_state import (
    ComplexityDimensionScore,
    ComplexityEvidenceRecord,
)


class ComplexityLevel(str, Enum):
    """Risk-driven taskflow levels from L0 micro-task to L4 program work."""

    L0 = "micro"
    L1 = "small"
    L2 = "standard"
    L3 = "complex"
    L4 = "program"


@dataclass(frozen=True, slots=True)
class ComplexityEstimate:
    """Result produced by ``ComplexityEstimator``."""

    level: ComplexityLevel
    score: int
    recipe_id: str
    recipe_steps: list[str] = field(default_factory=list)
    dimension_scores: dict[str, int] = field(default_factory=dict)
    dimension_details: list[ComplexityDimensionScore] = field(default_factory=list)
    confidence: float | None = None
    dominant_dimensions: list[str] = field(default_factory=list)
    unknown_dimensions: list[str] = field(default_factory=list)
    needs_more_evidence: bool = False
    explanation: str = ""
    scan_refs: list[str] = field(default_factory=list)
    question_packs: list[str] = field(default_factory=list)
    plan_slicing_policy: str = ""
    dispatch_safety_policy: str = ""
    level_floor: str = ""
    hard_escalations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ComplexityRubric:
    """Versioned scoring rubric for Complexity Control Plane decisions."""

    version: str
    dimensions: tuple[str, ...]
    thresholds: tuple[tuple[int, ComplexityLevel], ...]


class ComplexityRubricRegistry:
    """Provide the active versioned rubric used by the deterministic scorer."""

    def __init__(self, rubric: ComplexityRubric | None = None) -> None:
        self._rubric = rubric or ComplexityRubric(
            version="complexity-rubric.v1",
            dimensions=(
                "goal_clarity",
                "acceptance_quality",
                "business_impact",
                "user_count",
                "domain_complexity",
                "technical_risk",
                "interface_impact",
                "data_impact",
                "ops_impact",
                "reversibility",
                "org_collaboration",
            ),
            thresholds=(
                (5, ComplexityLevel.L0),
                (12, ComplexityLevel.L1),
                (22, ComplexityLevel.L2),
                (35, ComplexityLevel.L3),
            ),
        )

    @property
    def active(self) -> ComplexityRubric:
        return self._rubric


class ComplexityEstimator:
    """Estimate Taskflow complexity and select a discussion recipe.

    Logic from ``docs/方向建议.md`` Sections 2.2 and 2.3:
    each dimension is scored 0..3, then the total routes to L0-L4 with
    recipe steps chosen by risk and ambiguity.
    """

    dimensions: tuple[str, ...] = (
        "goal_clarity",
        "acceptance_quality",
        "business_impact",
        "user_count",
        "domain_complexity",
        "technical_risk",
        "interface_impact",
        "data_impact",
        "ops_impact",
        "reversibility",
        "org_collaboration",
    )

    recipes: dict[ComplexityLevel, tuple[str, list[str]]] = {
        ComplexityLevel.L0: (
            "micro-task",
            ["goal_confirm", "acceptance_check", "compile_task"],
        ),
        ComplexityLevel.L1: (
            "small-task",
            ["goal", "scope", "acceptance", "risk_check", "compile_plan"],
        ),
        ComplexityLevel.L2: (
            "standard-feature",
            [
                "goal",
                "actors",
                "scenarios",
                "acceptance_examples",
                "tech_impact",
                "decision_if_needed",
                "compile_plan",
            ],
        ),
        ComplexityLevel.L3: (
            "complex-project",
            [
                "prd_lite",
                "sbe_bdd",
                "ddd_lite",
                "rfc_compare",
                "adr",
                "tech_spec",
                "api_spec_if_needed",
                "runbook_if_needed",
                "compile_plan",
            ],
        ),
        ComplexityLevel.L4: (
            "program-level",
            [
                "program_discovery",
                "domain_split",
                "epic_map",
                "roadmap",
                "governance",
                "multi_stage_compile",
            ],
        ),
    }

    level_order: tuple[ComplexityLevel, ...] = (
        ComplexityLevel.L0,
        ComplexityLevel.L1,
        ComplexityLevel.L2,
        ComplexityLevel.L3,
        ComplexityLevel.L4,
    )

    def estimate(
        self,
        evidence: Iterable[ComplexityEvidenceRecord] | Mapping[str, int | float | str | bool],
    ) -> ComplexityEstimate:
        """Score supplied evidence and return a recipe selection."""

        if isinstance(evidence, Mapping):
            evidence = [
                ComplexityEvidenceRecord(
                    id=f"complexity-{dimension}",
                    dimension=dimension,
                    source_type="legacy_signal",
                    score_delta=self._score(value),
                    rationale=f"Legacy signal {dimension}.",
                )
                for dimension, value in evidence.items()
            ]
        else:
            evidence = list(evidence)

        evidence_by_dimension: dict[str, list[ComplexityEvidenceRecord]] = {
            dimension: [] for dimension in self.dimensions
        }
        hard_escalations: list[str] = []
        floor = ComplexityLevel.L0
        for item in evidence:
            if item.dimension in evidence_by_dimension:
                evidence_by_dimension[item.dimension].append(item)
            for escalation in self._hard_escalations(item):
                if escalation not in hard_escalations:
                    hard_escalations.append(escalation)
            floor = self._max_level(floor, self._floor_for_evidence(item))

        scores: dict[str, int] = {}
        details: list[ComplexityDimensionScore] = []
        for dimension in self.dimensions:
            items = evidence_by_dimension.get(dimension, [])
            raw = sum(max(0, int(item.score_delta)) for item in items)
            score = self._score(raw)
            scores[dimension] = score
            details.append(
                ComplexityDimensionScore(
                    dimension=dimension,
                    score=score,
                    evidence_ids=[item.id for item in items if item.id],
                    rationale="; ".join(
                        item.rationale for item in items if item.rationale
                    ),
                )
            )

        total = sum(scores.values())
        level = self._max_level(self._level_for_score(total), floor)
        recipe_id, steps = self.recipes[level]
        populated_dimensions = {
            dimension for dimension, items in evidence_by_dimension.items() if items
        }
        unknown_dimensions = [
            dimension for dimension in self.dimensions if dimension not in populated_dimensions
        ]
        dominant_dimensions = [
            dimension
            for dimension, score in sorted(
                scores.items(), key=lambda item: (-item[1], item[0])
            )
            if score >= 2
        ]
        if not dominant_dimensions:
            dominant_dimensions = [
                dimension
                for dimension, score in sorted(
                    scores.items(), key=lambda item: (-item[1], item[0])
                )
                if score > 0
            ][:3]
        confidence = self._confidence(evidence, unknown_dimensions)
        scan_refs = self._scan_refs(evidence)
        explanation = self._explanation(
            total=total,
            recipe_id=recipe_id,
            dominant_dimensions=dominant_dimensions,
            unknown_dimensions=unknown_dimensions,
            has_repo_static_analysis=bool(scan_refs)
            or any(item.source_type == "repo_static_analysis" for item in evidence),
        )
        question_packs = self._question_packs(unknown_dimensions, scores)
        plan_slicing_policy = self._plan_slicing_policy(level)
        dispatch_safety_policy = self._dispatch_safety_policy(
            level, hard_escalations, scores
        )
        return ComplexityEstimate(
            level=level,
            score=total,
            recipe_id=recipe_id,
            recipe_steps=list(steps),
            dimension_scores=scores,
            dimension_details=details,
            confidence=confidence,
            dominant_dimensions=dominant_dimensions,
            unknown_dimensions=unknown_dimensions,
            needs_more_evidence=bool(unknown_dimensions),
            explanation=explanation,
            scan_refs=scan_refs,
            question_packs=question_packs,
            plan_slicing_policy=plan_slicing_policy,
            dispatch_safety_policy=dispatch_safety_policy,
            level_floor=floor.name,
            hard_escalations=hard_escalations,
        )

    def _score(self, raw: int | float | str | bool | None) -> int:
        if isinstance(raw, bool):
            return 1 if raw else 0
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(3, value))

    def _level_for_score(self, score: int) -> ComplexityLevel:
        if score <= 5:
            return ComplexityLevel.L0
        if score <= 12:
            return ComplexityLevel.L1
        if score <= 22:
            return ComplexityLevel.L2
        # The current rubric has eleven 0..3 dimensions, so a fully saturated
        # score is 33. Route the saturated end of the scale to L4 instead of
        # leaving program-level work unreachable.
        if score <= 32:
            return ComplexityLevel.L3
        return ComplexityLevel.L4

    def _confidence(
        self, evidence: list[ComplexityEvidenceRecord], unknown_dimensions: list[str]
    ) -> float:
        if not evidence:
            return 0.0
        values = [
            float(item.confidence)
            for item in evidence
            if item.confidence is not None
        ]
        base = sum(values) / len(values) if values else 0.7
        coverage = 1.0 - (len(unknown_dimensions) / max(1, len(self.dimensions)))
        return round(max(0.0, min(1.0, (base * 0.7) + (coverage * 0.3))), 3)

    def _scan_refs(self, evidence: list[ComplexityEvidenceRecord]) -> list[str]:
        refs: list[str] = []
        for item in evidence:
            scan_id = str(item.metadata.get("scan_id") or "")
            if item.source_type == "repo_static_analysis" and scan_id and scan_id not in refs:
                refs.append(scan_id)
        return refs

    def _explanation(
        self,
        *,
        total: int,
        recipe_id: str,
        dominant_dimensions: list[str],
        unknown_dimensions: list[str],
        has_repo_static_analysis: bool,
    ) -> str:
        parts = [f"Complexity score {total} selected {recipe_id}."]
        if dominant_dimensions:
            parts.append(
                "Dominant dimensions: " + ", ".join(dominant_dimensions) + "."
            )
        if has_repo_static_analysis:
            parts.append("Repo static analysis contributed formal evidence.")
        if unknown_dimensions:
            parts.append(
                "More evidence is needed for: "
                + ", ".join(unknown_dimensions[:5])
                + "."
            )
        return " ".join(parts)

    def _question_packs(
        self, unknown_dimensions: list[str], scores: dict[str, int]
    ) -> list[str]:
        packs: list[str] = []
        if "goal_clarity" in unknown_dimensions or scores.get("goal_clarity", 0) > 0:
            packs.append("goal-clarification")
        if "acceptance_quality" in unknown_dimensions or scores.get("acceptance_quality", 0) > 0:
            packs.append("acceptance-examples")
        if "interface_impact" in unknown_dimensions or scores.get("interface_impact", 0) >= 2:
            packs.append("interface-impact")
        if "data_impact" in unknown_dimensions or scores.get("data_impact", 0) >= 2:
            packs.append("data-migration")
        if "ops_impact" in unknown_dimensions or scores.get("ops_impact", 0) >= 2:
            packs.append("rollout-operations")
        if "domain_complexity" in unknown_dimensions or scores.get("domain_complexity", 0) >= 2:
            packs.append("domain-rules")
        return packs

    def _plan_slicing_policy(self, level: ComplexityLevel) -> str:
        return {
            ComplexityLevel.L0: "single-task",
            ComplexityLevel.L1: "small-plan",
            ComplexityLevel.L2: "feature-work-items",
            ComplexityLevel.L3: "staged-work-items",
            ComplexityLevel.L4: "roadmap-phases",
        }[level]

    def _dispatch_safety_policy(
        self,
        level: ComplexityLevel,
        hard_escalations: list[str],
        scores: dict[str, int],
    ) -> str:
        if level in {ComplexityLevel.L3, ComplexityLevel.L4}:
            return "human-review-required"
        if hard_escalations or any(
            scores.get(dimension, 0) >= 2
            for dimension in ("interface_impact", "data_impact", "ops_impact")
        ):
            return "explicit-dispatch-confirmation"
        return "confirmed-brief"

    def _max_level(
        self, first: ComplexityLevel, second: ComplexityLevel
    ) -> ComplexityLevel:
        return (
            first
            if self.level_order.index(first) >= self.level_order.index(second)
            else second
        )

    def _floor_for_evidence(self, item: ComplexityEvidenceRecord) -> ComplexityLevel:
        text = f"{item.rationale} {item.source_type} {item.source_path}".lower()
        metadata = {str(key): str(value).lower() for key, value in item.metadata.items()}
        if item.dimension == "interface_impact":
            if (
                item.score_delta >= 2
                or "public" in text
                or metadata.get("contract") == "unknown"
                or metadata.get("public") == "true"
                or metadata.get("multiple_consumers") == "true"
            ):
                return ComplexityLevel.L2
            if any(token in text for token in ("plugin", "extension", "integration", "api", "webhook")):
                return ComplexityLevel.L1
        if item.dimension == "data_impact" and (
            item.score_delta >= 2
            or any(token in text for token in ("migration", "schema", "rollback"))
        ):
            return ComplexityLevel.L2
        if item.dimension == "ops_impact" and (
            item.score_delta >= 2
            or any(token in text for token in ("production", "rollout", "runbook", "monitoring", "rollback"))
        ):
            return ComplexityLevel.L2
        if item.dimension == "technical_risk" and metadata.get("multi_repo") == "true":
            return ComplexityLevel.L3
        if item.dimension == "domain_complexity" and metadata.get("core_high_risk") == "true":
            return ComplexityLevel.L3
        return ComplexityLevel.L0

    def _hard_escalations(self, item: ComplexityEvidenceRecord) -> list[str]:
        text = f"{item.rationale} {item.source_type} {item.source_path}".lower()
        metadata = {str(key): str(value).lower() for key, value in item.metadata.items()}
        escalations: list[str] = []
        if item.dimension == "interface_impact":
            if (
                item.score_delta >= 2
                or "public" in text
                or metadata.get("public") == "true"
                or metadata.get("multiple_consumers") == "true"
            ):
                escalations.append("public-interface-floor")
            elif any(token in text for token in ("plugin", "extension", "integration", "api", "webhook")):
                escalations.append("external-integration-floor")
        if item.dimension == "data_impact" and (
            item.score_delta >= 2
            or any(token in text for token in ("migration", "schema", "rollback"))
        ):
            escalations.append("data-migration-floor")
        if item.dimension == "ops_impact" and (
            item.score_delta >= 2
            or any(token in text for token in ("production", "rollout", "runbook", "monitoring", "rollback"))
        ):
            escalations.append("production-ops-floor")
        if item.dimension == "technical_risk" and metadata.get("multi_repo") == "true":
            escalations.append("multi-repo-high-risk-floor")
        if item.dimension == "domain_complexity" and metadata.get("core_high_risk") == "true":
            escalations.append("core-domain-risk-floor")
        return escalations


@dataclass(frozen=True, slots=True)
class RecipePolicy:
    """Recipe policy selected from final complexity level and dimensions."""

    policy_id: str
    recipe_id: str
    level: ComplexityLevel
    steps: list[str]
    required_artifacts: list[str]
    required_gates: list[str]


class RecipePolicyRegistry:
    """Map complexity estimates to recipe policies and required artifacts."""

    base_artifacts: dict[ComplexityLevel, list[str]] = {
        ComplexityLevel.L0: ["brief", "acceptance"],
        ComplexityLevel.L1: ["brief", "scope", "acceptance"],
        ComplexityLevel.L2: [
            "brief",
            "scenarios",
            "acceptance_examples",
            "technical_notes",
        ],
        ComplexityLevel.L3: [
            "brief",
            "prd",
            "rfc",
            "sbe_bdd",
            "ddd_lite",
            "adr",
            "tech_spec",
            "api_spec",
            "runbook",
        ],
        ComplexityLevel.L4: ["brief", "roadmap", "governance"],
    }

    def policy_for(self, estimate: ComplexityEstimate) -> RecipePolicy:
        artifacts = list(self.base_artifacts[estimate.level])
        scores = estimate.dimension_scores
        if scores.get("interface_impact", 0) >= 2:
            self._extend(artifacts, ["api_contract", "consumer_impact"])
        if scores.get("data_impact", 0) >= 2:
            self._extend(artifacts, ["migration_plan", "rollback_plan", "data_validation"])
        if scores.get("ops_impact", 0) >= 2:
            self._extend(artifacts, ["rollout_plan", "runbook", "monitoring_signal"])
        if scores.get("domain_complexity", 0) >= 2:
            self._extend(artifacts, ["domain_model_delta", "ubiquitous_language", "core_rules"])
        if scores.get("technical_risk", 0) >= 2:
            self._extend(artifacts, ["risk_spike"])
        if estimate.level in {ComplexityLevel.L2, ComplexityLevel.L3}:
            self._extend(artifacts, ["gherkin_feature"])
        return RecipePolicy(
            policy_id=f"recipe-policy.v1.{estimate.recipe_id}",
            recipe_id=estimate.recipe_id,
            level=estimate.level,
            steps=list(estimate.recipe_steps),
            required_artifacts=artifacts,
            required_gates=[f"gate-artifact-{artifact}" for artifact in artifacts],
        )

    def _extend(self, artifacts: list[str], values: list[str]) -> None:
        for value in values:
            if value not in artifacts:
                artifacts.append(value)


__all__ = [
    "ComplexityEstimate",
    "ComplexityEstimator",
    "ComplexityLevel",
    "ComplexityRubric",
    "ComplexityRubricRegistry",
    "RecipePolicy",
    "RecipePolicyRegistry",
]
