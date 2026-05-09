"""Taskflow complexity scoring and recipe selection.

Source: ``docs/方向建议.md`` Section 2.2 and Section 2.3. Complexity is the
first routing decision: light goals should not be dragged through heavy
discovery, and risky goals should not compile before enough clarification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


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


class ComplexityEstimator:
    """Estimate Taskflow complexity and select a discussion recipe.

    Logic from ``docs/方向建议.md`` Sections 2.2 and 2.3:
    each dimension is scored 0..3, then the total routes to L0-L4 with
    recipe steps chosen by risk and ambiguity.
    """

    dimensions: tuple[str, ...] = (
        "goal_clarity",
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

    def estimate(self, signals: Mapping[str, int | float | str | bool]) -> ComplexityEstimate:
        """Score supplied signals and return a recipe selection."""

        scores: dict[str, int] = {}
        for dimension in self.dimensions:
            scores[dimension] = self._score(signals.get(dimension, 0))
        total = sum(scores.values())
        level = self._level_for_score(total)
        recipe_id, steps = self.recipes[level]
        return ComplexityEstimate(
            level=level,
            score=total,
            recipe_id=recipe_id,
            recipe_steps=list(steps),
            dimension_scores=scores,
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
        if score <= 35:
            return ComplexityLevel.L3
        return ComplexityLevel.L4


__all__ = ["ComplexityEstimate", "ComplexityEstimator", "ComplexityLevel"]
