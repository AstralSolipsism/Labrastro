from __future__ import annotations

import pytest

from labrastro_server.taskflow.domain.complexity import (
    ComplexityEstimator,
    ComplexityLevel,
)
from labrastro_server.taskflow.domain.taskflow_state import ComplexityEvidenceRecord


GOLDEN_CASES = [
    ("copy tweak", ComplexityLevel.L0, {"goal_clarity": 1, "acceptance_quality": 1}),
    ("config flag", ComplexityLevel.L0, {"business_impact": 1, "technical_risk": 1}),
    ("button loading", ComplexityLevel.L0, {"goal_clarity": 1, "user_count": 1}),
    ("small validation", ComplexityLevel.L0, {"acceptance_quality": 1, "technical_risk": 1}),
    ("docs wording", ComplexityLevel.L0, {"business_impact": 1}),
    ("local feature", ComplexityLevel.L1, {"goal_clarity": 2, "acceptance_quality": 2, "business_impact": 2}),
    ("single module refactor", ComplexityLevel.L1, {"technical_risk": 3, "business_impact": 2, "acceptance_quality": 2}),
    ("internal form", ComplexityLevel.L1, {"user_count": 2, "goal_clarity": 2, "business_impact": 2}),
    ("small integration", ComplexityLevel.L1, {"interface_impact": 1, "technical_risk": 2, "business_impact": 2, "acceptance_quality": 1}),
    ("workflow polish", ComplexityLevel.L1, {"goal_clarity": 2, "acceptance_quality": 2, "domain_complexity": 2}),
    ("standard api feature", ComplexityLevel.L2, {"interface_impact": 2, "technical_risk": 3, "business_impact": 3, "acceptance_quality": 3, "user_count": 2}),
    ("schema feature", ComplexityLevel.L2, {"data_impact": 2, "technical_risk": 3, "business_impact": 3, "reversibility": 2, "acceptance_quality": 3}),
    ("role workflow", ComplexityLevel.L2, {"user_count": 3, "domain_complexity": 3, "business_impact": 3, "goal_clarity": 2, "acceptance_quality": 3}),
    ("external integration", ComplexityLevel.L2, {"interface_impact": 2, "ops_impact": 2, "technical_risk": 3, "business_impact": 3, "goal_clarity": 3}),
    ("module redesign", ComplexityLevel.L2, {"domain_complexity": 3, "technical_risk": 3, "interface_impact": 2, "acceptance_quality": 3, "business_impact": 3}),
    ("multi system rollout", ComplexityLevel.L3, {"goal_clarity": 3, "acceptance_quality": 3, "business_impact": 3, "user_count": 3, "domain_complexity": 3, "technical_risk": 3, "interface_impact": 3, "data_impact": 3, "ops_impact": 3}),
    ("platform migration", ComplexityLevel.L3, {"technical_risk": 3, "data_impact": 3, "ops_impact": 3, "reversibility": 3, "org_collaboration": 3, "business_impact": 3, "interface_impact": 3, "acceptance_quality": 3}),
    ("core domain rewrite", ComplexityLevel.L3, {"domain_complexity": 3, "technical_risk": 3, "business_impact": 3, "user_count": 3, "interface_impact": 3, "data_impact": 2, "ops_impact": 2, "goal_clarity": 3, "acceptance_quality": 3}),
    ("public api revamp", ComplexityLevel.L3, {"interface_impact": 3, "data_impact": 3, "ops_impact": 3, "technical_risk": 3, "org_collaboration": 3, "business_impact": 3, "acceptance_quality": 3, "user_count": 3}),
    ("security sensitive flow", ComplexityLevel.L3, {"technical_risk": 3, "domain_complexity": 3, "business_impact": 3, "user_count": 3, "interface_impact": 2, "ops_impact": 3, "reversibility": 3, "acceptance_quality": 3}),
    ("program governance", ComplexityLevel.L4, {"goal_clarity": 3, "acceptance_quality": 3, "business_impact": 3, "user_count": 3, "domain_complexity": 3, "technical_risk": 3, "interface_impact": 3, "data_impact": 3, "ops_impact": 3, "reversibility": 3, "org_collaboration": 3}),
    ("multi phase platform", ComplexityLevel.L4, {"goal_clarity": 3, "acceptance_quality": 3, "business_impact": 3, "user_count": 3, "domain_complexity": 3, "technical_risk": 3, "interface_impact": 3, "data_impact": 3, "ops_impact": 3, "reversibility": 3, "org_collaboration": 3}),
    ("enterprise rollout", ComplexityLevel.L4, {"goal_clarity": 3, "acceptance_quality": 3, "business_impact": 3, "user_count": 3, "domain_complexity": 3, "technical_risk": 3, "interface_impact": 3, "data_impact": 3, "ops_impact": 3, "reversibility": 3, "org_collaboration": 3}),
    ("architecture roadmap", ComplexityLevel.L4, {"goal_clarity": 3, "acceptance_quality": 3, "business_impact": 3, "user_count": 3, "domain_complexity": 3, "technical_risk": 3, "interface_impact": 3, "data_impact": 3, "ops_impact": 3, "reversibility": 3, "org_collaboration": 3}),
    ("long governance program", ComplexityLevel.L4, {"goal_clarity": 3, "acceptance_quality": 3, "business_impact": 3, "user_count": 3, "domain_complexity": 3, "technical_risk": 3, "interface_impact": 3, "data_impact": 3, "ops_impact": 3, "reversibility": 3, "org_collaboration": 3}),
]


@pytest.mark.parametrize(("name", "expected", "signals"), GOLDEN_CASES)
def test_complexity_golden_cases_route_to_expected_level(name, expected, signals) -> None:
    estimate = ComplexityEstimator().estimate(signals)

    assert estimate.level == expected, name
    assert estimate.plan_slicing_policy
    assert estimate.dispatch_safety_policy
    assert estimate.question_packs


def test_repo_static_analysis_evidence_explains_control_plane_outputs() -> None:
    estimate = ComplexityEstimator().estimate(
        [
            ComplexityEvidenceRecord(
                id="repo-api",
                dimension="interface_impact",
                source_type="repo_static_analysis",
                source_id="api",
                score_delta=2,
                confidence=0.8,
                rationale="Repo static analysis found API surface.",
                metadata={"scan_id": "repo-scan-1", "public": True},
            )
        ]
    )

    assert estimate.level == ComplexityLevel.L2
    assert estimate.scan_refs == ["repo-scan-1"]
    assert "interface_impact" in estimate.dominant_dimensions
    assert "interface-impact" in estimate.question_packs
    assert estimate.dispatch_safety_policy == "explicit-dispatch-confirmation"
