"""Versioned brief operations for Taskflow."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.taskflow.domain.taskflow_state import (
    BriefStatus,
    BriefVersionRecord,
    TaskflowEventType,
    TaskflowState,
)


def _brief_id(taskflow_id: str, version: int) -> str:
    return f"brief-{taskflow_id}-{version}"


class BriefService:
    """Compile, ready, and confirm plan brief versions."""

    def compile_draft(self, state: TaskflowState, *, actor: str = "") -> BriefVersionRecord:
        version = (state.outputs.current_brief_version or 0) + 1
        previous = state.outputs.brief_versions[-1] if state.outputs.brief_versions else None
        snapshot = self._snapshot(state)
        source_event_ids = [event.id for event in state.events]
        brief = BriefVersionRecord(
            id=_brief_id(state.meta.taskflow_id, version),
            version=version,
            status=BriefStatus.DRAFT,
            goal=snapshot["goal"],
            scope=snapshot["scope"],
            glossary=snapshot["glossary"],
            rules=snapshot["rules"],
            examples=snapshot["examples"],
            acceptance_examples=snapshot["acceptance_examples"],
            scenarios=snapshot["scenarios"],
            open_questions=snapshot["open_questions"],
            decisions=snapshot["decisions"],
            risks=snapshot["risks"],
            interfaces=snapshot["interfaces"],
            gherkin_features=snapshot["gherkin_features"],
            issue_map=snapshot["issue_map"],
            work_item_candidates=snapshot["work_item_candidates"],
            task_drafts=snapshot["work_item_candidates"],
            quality=self._quality(state),
            complexity_estimate=snapshot["complexity_estimate"],
            readiness_gates=snapshot["readiness_gates"],
            readiness_score=snapshot["readiness_score"],
            compile_readiness_score=snapshot["compile_readiness_score"],
            dispatch_readiness_score=snapshot["dispatch_readiness_score"],
            content_hash=self._content_hash(snapshot),
            previous_version=previous.version if previous is not None else None,
            causation_event_id=source_event_ids[-1] if source_event_ids else None,
            source_event_ids=source_event_ids,
            diff_summary=self._diff_summary(snapshot, previous),
            created_by=actor,
        )
        state.outputs.brief_versions.append(brief)
        state.outputs.current_brief_version = version
        append_taskflow_event(
            state,
            TaskflowEventType.BRIEF_VERSIONED,
            actor=actor,
            payload={"brief_version": version, "brief_id": brief.id},
        )
        return brief

    def sync_diagnostics(self, state: TaskflowState) -> None:
        """Update the current draft brief with derived complexity/readiness snapshots."""

        if state.outputs.current_brief_version is None:
            return
        brief = self._brief(state, state.outputs.current_brief_version)
        snapshot = self._snapshot(state)
        brief.complexity_estimate = snapshot["complexity_estimate"]
        brief.readiness_gates = snapshot["readiness_gates"]
        brief.readiness_score = snapshot["readiness_score"]
        brief.compile_readiness_score = snapshot["compile_readiness_score"]
        brief.dispatch_readiness_score = snapshot["dispatch_readiness_score"]
        brief.content_hash = self._content_hash(snapshot)

    def mark_ready(
        self, state: TaskflowState, *, version: int | None = None, actor: str = ""
    ) -> BriefVersionRecord:
        brief = self._brief(state, version or state.outputs.current_brief_version)
        if brief.version != state.outputs.current_brief_version:
            raise ValueError("only the current brief version can be marked ready")
        brief.status = BriefStatus.READY
        brief.ready_at = brief.ready_at or utc_now()
        append_taskflow_event(
            state,
            TaskflowEventType.BRIEF_CONFIRM_REQUESTED,
            actor=actor,
            payload={"brief_version": brief.version, "brief_id": brief.id},
        )
        append_taskflow_event(
            state,
            TaskflowEventType.BRIEF_READY,
            actor=actor,
            payload={"brief_version": brief.version, "brief_id": brief.id},
        )
        return brief

    def confirm(
        self, state: TaskflowState, *, version: int | None = None, actor: str = "user"
    ) -> BriefVersionRecord:
        brief = self._brief(state, version or state.outputs.current_brief_version)
        if brief.version != state.outputs.current_brief_version:
            raise ValueError("only the current brief version can be confirmed")
        brief.status = BriefStatus.CONFIRMED
        brief.confirmed_by = actor
        brief.confirmed_at = brief.confirmed_at or utc_now()
        state.outputs.confirmed_brief_version = brief.version
        append_taskflow_event(
            state,
            TaskflowEventType.BRIEF_CONFIRMED,
            actor=actor,
            payload={"brief_version": brief.version, "brief_id": brief.id},
        )
        return brief

    def _brief(self, state: TaskflowState, version: int | None) -> BriefVersionRecord:
        if version is None:
            raise ValueError("no brief version exists")
        for brief in state.outputs.brief_versions:
            if brief.version == version:
                return brief
        raise ValueError(f"brief version not found: {version}")

    def _snapshot(self, state: TaskflowState) -> dict[str, Any]:
        return {
            "goal": {
                "id": state.meta.goal_id,
                "statement": state.intent.goal_statement,
                "desired_outcome": list(state.intent.success_criteria),
            },
            "scope": {
                "in": list(state.intent.scope_in),
                "out": list(state.intent.scope_out),
                "deferred": list(state.intent.deferred_scope),
                "success_criteria": list(state.intent.success_criteria),
            },
            "glossary": [
                {"term": key, "meaning": value}
                for key, value in sorted(state.domain.ubiquitous_language.items())
            ],
            "rules": [rule.to_dict() for rule in state.clarification.rules],
            "examples": [example.to_dict() for example in state.clarification.examples],
            "acceptance_examples": [
                example.to_dict() for example in state.clarification.acceptance_examples
            ],
            "scenarios": [scenario.to_dict() for scenario in state.clarification.scenarios],
            "open_questions": [
                question.to_dict() for question in state.clarification.open_questions
            ],
            "decisions": [decision.to_dict() for decision in state.design.local_decisions],
            "risks": [risk.to_dict() for risk in state.design.risks],
            "interfaces": [interface.to_dict() for interface in state.design.interfaces],
            "gherkin_features": [dict(item) for item in state.outputs.gherkin_features],
            "issue_map": [dict(item) for item in state.outputs.issue_map],
            "work_item_candidates": [
                candidate.to_dict() for candidate in state.outputs.work_item_candidates
            ],
            "complexity_estimate": (
                state.compiler.complexity_estimate.to_dict()
                if state.compiler.complexity_estimate is not None
                else {}
            ),
            "readiness_gates": [
                gate.to_dict() for gate in state.compiler.readiness_gates
            ],
            "readiness_score": state.compiler.readiness_score,
            "compile_readiness_score": state.compiler.compile_readiness_score,
            "dispatch_readiness_score": state.compiler.dispatch_readiness_score,
        }

    def _content_hash(self, snapshot: dict[str, Any]) -> str:
        payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _diff_summary(
        self, snapshot: dict[str, Any], previous: BriefVersionRecord | None
    ) -> dict[str, Any]:
        if previous is None:
            return {"previous_version": None, "changed_sections": list(snapshot.keys())}
        previous_snapshot = {
            "goal": previous.goal,
            "scope": previous.scope,
            "glossary": previous.glossary,
            "rules": previous.rules,
            "examples": previous.examples,
            "acceptance_examples": previous.acceptance_examples,
            "scenarios": previous.scenarios,
            "open_questions": previous.open_questions,
            "decisions": previous.decisions,
            "risks": previous.risks,
            "interfaces": previous.interfaces,
            "gherkin_features": previous.gherkin_features,
            "issue_map": previous.issue_map,
            "work_item_candidates": previous.work_item_candidates,
            "complexity_estimate": previous.complexity_estimate,
            "readiness_gates": previous.readiness_gates,
            "readiness_score": previous.readiness_score,
            "compile_readiness_score": previous.compile_readiness_score,
            "dispatch_readiness_score": previous.dispatch_readiness_score,
        }
        changed = [
            key
            for key, value in snapshot.items()
            if value != previous_snapshot.get(key)
        ]
        return {"previous_version": previous.version, "changed_sections": changed}

    def _quality(self, state: TaskflowState) -> dict[str, object]:
        decision_total = len(state.design.local_decisions)
        decision_confirmed = len(
            [
                decision
                for decision in state.design.local_decisions
                if decision.chosen or decision.answer_refs
            ]
        )
        scenario_total = len(state.clarification.examples) + len(
            state.clarification.acceptance_examples
        )
        unresolved = len(
            [question for question in state.clarification.open_questions if not question.answered]
        )
        return {
            "decision_coverage": (
                decision_confirmed / decision_total if decision_total else 1.0
            ),
            "scenario_coverage": 1.0 if scenario_total else 0.0,
            "unresolved_questions": unresolved,
            "dispatch_readiness": "blocked" if unresolved else "ready",
        }


__all__ = ["BriefService"]
