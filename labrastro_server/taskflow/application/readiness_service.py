"""Readiness gate generation for Taskflow."""

from __future__ import annotations

from labrastro_server.taskflow.domain.taskflow_state import (
    BriefStatus,
    BriefVersionRecord,
    ComplexityEstimateRecord,
    DispatchDecisionStatus,
    ReadinessGate,
    TaskflowState,
)


class ReadinessService:
    """Evaluate compile and dispatch readiness from structured state."""

    severity_penalty: dict[str, int] = {
        "critical": 35,
        "high": 20,
        "medium": 10,
        "low": 5,
    }

    def refresh(
        self,
        state: TaskflowState,
        assessment: ComplexityEstimateRecord | None = None,
    ) -> TaskflowState:
        """Regenerate derived gates while preserving explicit manual gates."""

        estimate = assessment or state.compiler.complexity_estimate
        manual = {
            gate.id: gate
            for gate in state.compiler.readiness_gates
            if gate.source == "manual"
        }
        generated: list[ReadinessGate] = []

        confirmed = self._confirmed_brief(state)
        current = self._current_brief(state)
        fresh_confirmed = (
            confirmed is not None
            and current is not None
            and confirmed.version == current.version
        )
        generated.append(
            self._gate(
                "gate-confirmed-brief",
                "confirmed-brief",
                confirmed is not None,
                "Brief is confirmed."
                if confirmed is not None
                else "A confirmed brief is required before plan compilation.",
                phase="compile",
                source="brief",
            )
        )
        generated.append(
            self._gate(
                "gate-fresh-brief-confirmation",
                "fresh-brief-confirmation",
                fresh_confirmed,
                "Current brief version is confirmed."
                if fresh_confirmed
                else "The current brief version must be confirmed before compile.",
                phase="both",
                source="brief",
            )
        )

        high_risk_questions = state.unresolved_high_risk_questions()
        generated.append(
            self._gate(
                "gate-high-risk-questions",
                "high-risk-questions",
                not high_risk_questions,
                "No unresolved high-risk questions."
                if not high_risk_questions
                else "Unresolved high-risk questions block compilation.",
                phase="both",
                source="complexity",
                evidence_ids=self._evidence_ids(estimate, "goal_clarity"),
            )
        )

        acceptance = self._acceptance_check(state, current)
        generated.append(
            self._gate(
                "gate-acceptance-contract",
                "acceptance-coverage",
                acceptance["passed"],
                acceptance["rationale"],
                phase="compile",
                source="trace",
                severity="medium",
            )
        )
        trace = self._trace_check(state, current)
        generated.append(
            self._gate(
                "gate-trace-integrity",
                "trace-integrity",
                trace["passed"],
                trace["rationale"],
                phase="compile",
                source="trace",
            )
        )

        if estimate is not None:
            for artifact in estimate.required_artifacts:
                if artifact == "brief":
                    continue
                generated.append(self._artifact_gate(state, estimate, artifact))

        generated.append(
            self._gate(
                "gate-compiled-plan",
                "compiled-plan",
                bool(state.outputs.plan_drafts),
                "Plan has been compiled."
                if state.outputs.plan_drafts
                else "A compiled plan is required before dispatch.",
                phase="dispatch",
                source="dispatch",
            )
        )
        dispatch_confirmed = any(
            decision.status == DispatchDecisionStatus.CONFIRMED
            and decision.brief_version == state.outputs.confirmed_brief_version
            for decision in state.outputs.dispatch_decisions
        )
        generated.append(
            self._gate(
                "gate-dispatch-confirmation",
                "dispatch-confirmation",
                dispatch_confirmed,
                "A dispatch decision is confirmed."
                if dispatch_confirmed
                else "A confirmed dispatch decision is required before TaskRun creation.",
                phase="dispatch",
                source="dispatch",
            )
        )

        by_id = {gate.id: gate for gate in generated}
        by_id.update(manual)
        state.compiler.readiness_gates = list(by_id.values())

        compile_failed = [
            gate
            for gate in state.compiler.readiness_gates
            if gate.required and not gate.passed and bool(gate.blocks_compile)
        ]
        dispatch_failed = [
            gate
            for gate in state.compiler.readiness_gates
            if gate.required and not gate.passed and bool(gate.blocks_dispatch)
        ]
        state.compiler.compile_readiness_score = self._score(compile_failed)
        state.compiler.dispatch_readiness_score = self._score(dispatch_failed)
        state.compiler.readiness_score = min(
            state.compiler.compile_readiness_score,
            state.compiler.dispatch_readiness_score,
        )
        state.compiler.blocking_items = [
            gate.id
            for gate in state.compiler.readiness_gates
            if gate.required and not gate.passed
        ]
        if state.compiler.complexity_estimate is not None:
            state.compiler.complexity_estimate.required_gates = [
                gate.id
                for gate in state.compiler.readiness_gates
                if gate.required and gate.source != "manual"
            ]
            state.compiler.complexity_estimate.compile_blockers = [
                gate.id for gate in compile_failed
            ]
            state.compiler.complexity_estimate.dispatch_blockers = [
                gate.id for gate in dispatch_failed
            ]
        return state

    def _gate(
        self,
        id: str,
        name: str,
        passed: bool,
        rationale: str,
        *,
        required: bool = True,
        severity: str = "high",
        phase: str = "both",
        source: str = "manual",
        evidence_ids: list[str] | None = None,
        artifact_keys: list[str] | None = None,
    ) -> ReadinessGate:
        return ReadinessGate(
            id=id,
            name=name,
            passed=passed,
            rationale=rationale,
            required=required,
            severity=severity,
            phase=phase,
            source=source,
            evidence_ids=list(evidence_ids or []),
            artifact_keys=list(artifact_keys or []),
        )

    def _artifact_gate(
        self,
        state: TaskflowState,
        estimate: ComplexityEstimateRecord,
        artifact: str,
    ) -> ReadinessGate:
        passed = self._artifact_complete(state, artifact)
        name = artifact.replace("_", "-")
        return self._gate(
            f"gate-artifact-{artifact}",
            name,
            passed,
            f"Required artifact {artifact} is complete."
            if passed
            else f"Required artifact {artifact} is missing.",
            phase="compile",
            source="complexity",
            severity=self._artifact_severity(artifact),
            evidence_ids=self._artifact_evidence_ids(estimate, artifact),
            artifact_keys=[artifact],
        )

    def _artifact_complete(self, state: TaskflowState, artifact: str) -> bool:
        if artifact in {"scope", "prd", "prd_lite"}:
            return bool(state.intent.goal_statement or state.intent.scope_in or state.intent.scope_out)
        if artifact in {"acceptance", "acceptance_examples", "sbe_bdd"}:
            return bool(
                state.clarification.examples
                or state.clarification.acceptance_examples
                or state.clarification.scenarios
            )
        if artifact == "scenarios":
            return bool(state.clarification.scenarios or state.clarification.examples)
        if artifact in {"technical_notes", "tech_spec"}:
            return bool(
                state.design.risks
                or state.design.implementation_notes
                or state.outputs.work_item_candidates
            )
        if artifact in {"api_contract", "api_spec"}:
            return bool(state.design.interfaces) and all(
                interface.contract for interface in state.design.interfaces
            )
        if artifact == "consumer_impact":
            return any(interface.consumers for interface in state.design.interfaces)
        if artifact == "migration_plan":
            return any(risk.mitigation for risk in state.design.risks)
        if artifact == "rollback_plan":
            text = " ".join(state.delivery.rollout_plan).lower()
            return "rollback" in text or any(risk.mitigation for risk in state.design.risks)
        if artifact == "data_validation":
            return any(
                example.observable_outputs or example.test_suggestions
                for example in state.clarification.acceptance_examples
            ) or any(example.observable_outputs for example in state.clarification.examples)
        if artifact == "rollout_plan":
            return bool(state.delivery.rollout_plan)
        if artifact == "runbook":
            return any("runbook" in item.lower() for item in state.delivery.rollout_plan)
        if artifact == "monitoring_signal":
            return any("monitor" in item.lower() for item in state.delivery.rollout_plan)
        if artifact == "risk_spike":
            return any(
                candidate.type.value in {"spike", "research", "test"}
                for candidate in state.outputs.work_item_candidates
            ) or any(risk.mitigation for risk in state.design.risks)
        if artifact == "gherkin_feature":
            return bool(
                state.outputs.gherkin_features
                or state.clarification.scenarios
                or state.clarification.examples
            )
        if artifact == "domain_model_delta":
            return bool(state.domain.domain_model_delta or state.clarification.rules)
        if artifact == "ubiquitous_language":
            return bool(state.domain.ubiquitous_language or state.clarification.rules)
        if artifact == "core_rules":
            return bool(state.clarification.rules)
        if artifact in {"rfc", "adr"}:
            return bool(state.design.local_decisions)
        if artifact in {"roadmap", "governance"}:
            return bool(state.delivery.future_goal_candidates)
        return True

    def _artifact_severity(self, artifact: str) -> str:
        if artifact in {
            "api_contract",
            "consumer_impact",
            "migration_plan",
            "rollback_plan",
            "rollout_plan",
            "runbook",
            "monitoring_signal",
        }:
            return "high"
        return "medium"

    def _artifact_evidence_ids(
        self, estimate: ComplexityEstimateRecord, artifact: str
    ) -> list[str]:
        dimension_by_artifact = {
            "api_contract": "interface_impact",
            "consumer_impact": "interface_impact",
            "migration_plan": "data_impact",
            "rollback_plan": "data_impact",
            "data_validation": "data_impact",
            "rollout_plan": "ops_impact",
            "runbook": "ops_impact",
            "monitoring_signal": "ops_impact",
            "domain_model_delta": "domain_complexity",
            "ubiquitous_language": "domain_complexity",
            "core_rules": "domain_complexity",
            "risk_spike": "technical_risk",
            "gherkin_feature": "acceptance_quality",
        }
        dimension = dimension_by_artifact.get(artifact)
        if not dimension:
            return []
        return self._evidence_ids(estimate, dimension)

    def _evidence_ids(
        self, estimate: ComplexityEstimateRecord | None, dimension: str
    ) -> list[str]:
        if estimate is None:
            return []
        return [
            item.id
            for item in estimate.evidence
            if item.dimension == dimension and item.id
        ]

    def _score(self, failed: list[ReadinessGate]) -> int:
        penalty = sum(
            self.severity_penalty.get(gate.severity, 20) for gate in failed
        )
        return max(0, 100 - penalty)

    def _current_brief(self, state: TaskflowState) -> BriefVersionRecord | None:
        return self._brief(state, state.outputs.current_brief_version)

    def _confirmed_brief(self, state: TaskflowState) -> BriefVersionRecord | None:
        version = state.outputs.confirmed_brief_version
        brief = self._brief(state, version)
        if brief is not None and brief.status == BriefStatus.CONFIRMED:
            return brief
        return None

    def _brief(
        self, state: TaskflowState, version: int | None
    ) -> BriefVersionRecord | None:
        if version is None:
            return None
        for brief in state.outputs.brief_versions:
            if brief.version == version:
                return brief
        return None

    def _acceptance_check(
        self, state: TaskflowState, brief: BriefVersionRecord | None
    ) -> dict[str, object]:
        examples, scenarios, candidates = self._snapshot_refs(state, brief)
        candidate_refs = {
            ref
            for candidate in candidates
            for ref in [
                *candidate.get("acceptance_refs", []),
                *candidate.get("scenario_refs", []),
            ]
        }
        known = examples | scenarios
        dangling = sorted(ref for ref in candidate_refs if ref not in known)
        passed = bool(known) and not dangling
        if passed:
            return {"passed": True, "rationale": "Acceptance refs resolve to examples or scenarios."}
        if dangling:
            return {
                "passed": False,
                "rationale": f"Dangling acceptance refs: {', '.join(dangling)}.",
            }
        return {
            "passed": False,
            "rationale": "At least one acceptance example or scenario is required.",
        }

    def _trace_check(
        self, state: TaskflowState, brief: BriefVersionRecord | None
    ) -> dict[str, object]:
        _, _, candidates = self._snapshot_refs(state, brief)
        decision_ids = self._ids(brief.decisions if brief else [
            item.to_dict() for item in state.design.local_decisions
        ])
        risk_ids = self._ids(brief.risks if brief else [
            item.to_dict() for item in state.design.risks
        ])
        dangling: list[str] = []
        for candidate in candidates:
            for ref in candidate.get("decision_refs", []):
                if ref not in decision_ids:
                    dangling.append(f"decision:{ref}")
            for ref in candidate.get("risk_refs", []):
                if ref not in risk_ids:
                    dangling.append(f"risk:{ref}")
        return {
            "passed": not dangling,
            "rationale": (
                "Candidate decision and risk refs resolve."
                if not dangling
                else f"Dangling trace refs: {', '.join(sorted(dangling))}."
            ),
        }

    def _snapshot_refs(
        self, state: TaskflowState, brief: BriefVersionRecord | None
    ) -> tuple[set[str], set[str], list[dict[str, object]]]:
        if brief is not None:
            examples = self._ids([*brief.examples, *brief.acceptance_examples])
            scenarios = self._ids(brief.scenarios)
            candidates = [dict(item) for item in brief.work_item_candidates]
            return examples, scenarios, candidates
        examples = {
            *[item.id for item in state.clarification.examples],
            *[item.id for item in state.clarification.acceptance_examples],
        }
        scenarios = {item.id for item in state.clarification.scenarios}
        candidates = [item.to_dict() for item in state.outputs.work_item_candidates]
        return examples, scenarios, candidates

    def _ids(self, items: list[dict[str, object]]) -> set[str]:
        return {str(item.get("id")) for item in items if str(item.get("id") or "").strip()}


__all__ = ["ReadinessService"]
