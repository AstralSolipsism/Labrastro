"""Application facade for the single-mainline Taskflow architecture.

This service implements the finalized ProjectState/TaskflowState compiler flow
from ``docs/文档.md`` Section 5:

1. start_taskflow
2. load_project_context
3. clarify_goal
4. confirm_goal
5. compile_goal
6. create/reuse WorkItem
7. create GoalWorkLink
8. readiness gate
9. dispatch TaskRun
10. update traceability and persist project deltas
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from labrastro_server.taskflow.application.project_service import ProjectService
from labrastro_server.taskflow.application.brief_service import BriefService
from labrastro_server.taskflow.application.complexity_service import (
    ComplexityAssessmentService,
)
from labrastro_server.taskflow.application.discovery_service import DiscoveryService
from labrastro_server.taskflow.application.readiness_service import ReadinessService
from labrastro_server.taskflow.application.review_service import ReviewService
from labrastro_server.taskflow.domain.complexity import ComplexityEstimator
from labrastro_server.taskflow.domain.complexity_signals import ComplexitySignalExtractor
from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.taskflow.compiler.plan_compiler import (
    CompiledWorkItemCandidate,
    PlanCompiler,
    PlanDraft,
)
from labrastro_server.taskflow.domain.project_state import (
    ProjectState,
    TaskRun,
    TaskRunExecutor,
    TaskRunStatus,
    TraceEntityType,
    TraceLink,
    TraceRelationType,
    WorkItem,
    WorkItemStatus,
)
from labrastro_server.taskflow.interaction.review_cards import CardRenderer, ReviewCard
from labrastro_server.taskflow.domain.taskflow_state import (
    Assumption,
    AcceptanceExample,
    BriefStatus,
    ComplexityEvidenceRecord,
    DecisionRecord,
    DispatchDecisionRecord,
    DispatchDecisionStatus,
    ExampleRecord,
    OpenQuestion,
    ReadinessGate,
    ReviewCardAnswer,
    RuleRecord,
    ScenarioRecord,
    TaskflowState,
    TaskflowEventType,
    TaskflowStatus,
    WorkItemCandidate,
)
from labrastro_server.taskflow.ports.dispatch import (
    TaskflowDispatcher,
    TaskflowDispatchResult,
)
from labrastro_server.taskflow.ports.state_store import (
    InMemoryTaskflowStateStore,
    TaskflowStateStore,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _string_list(value: Sequence[str] | None) -> list[str]:
    return [str(item) for item in value or [] if str(item).strip()]


class TaskflowService:
    """State-machine facade for the finalized Taskflow compiler flow."""

    def __init__(
        self,
        *,
        project_service: ProjectService | None = None,
        compiler: PlanCompiler | None = None,
        complexity_estimator: ComplexityEstimator | None = None,
        complexity_signal_extractor: ComplexitySignalExtractor | None = None,
        complexity_service: ComplexityAssessmentService | None = None,
        discovery_service: DiscoveryService | None = None,
        brief_service: BriefService | None = None,
        readiness_service: ReadinessService | None = None,
        review_service: ReviewService | None = None,
        card_renderer: CardRenderer | None = None,
        dispatcher: TaskflowDispatcher | None = None,
        state_store: TaskflowStateStore | None = None,
    ) -> None:
        self.project_service = project_service or ProjectService()
        self.compiler = compiler or PlanCompiler()
        self.complexity_estimator = complexity_estimator or ComplexityEstimator()
        self.complexity_signal_extractor = (
            complexity_signal_extractor or ComplexitySignalExtractor()
        )
        self.complexity_service = complexity_service or ComplexityAssessmentService(
            extractor=self.complexity_signal_extractor,
            estimator=self.complexity_estimator,
        )
        self.discovery_service = discovery_service or DiscoveryService()
        self.brief_service = brief_service or BriefService()
        self.readiness_service = readiness_service or ReadinessService()
        self.review_service = review_service or ReviewService()
        self.card_renderer = card_renderer or CardRenderer()
        self.dispatcher = dispatcher
        self.state_store = state_store or InMemoryTaskflowStateStore()
        self._compiled_plans: dict[str, PlanDraft] = {}

    def start_taskflow(
        self,
        *,
        project_id: str,
        raw_goal: str,
        session_id: str | None = None,
        peer_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        related_goal_id: str | None = None,
        related_work_item_id: str | None = None,
        taskflow_id: str | None = None,
        goal_id: str | None = None,
        sensitivity: str = "internal",
    ) -> TaskflowState:
        """Create a TaskflowState for one Goal conversation.

        Logic from ``docs/文档.md`` Section 5.1 and 5.2: create the compiler
        session, then load only refs/deltas from ProjectState instead of copying
        the whole project graph.
        """

        project = self.project_service.get_or_create_project_state(project_id)
        state = TaskflowState.new(
            taskflow_id=taskflow_id or _new_id("taskflow"),
            project_id=project_id,
            goal_id=goal_id or _new_id("goal"),
            goal_statement=raw_goal.strip(),
            sensitivity=sensitivity,
        )
        state.compiler.traceability_index["request_context"] = {
            "session_id": session_id,
            "peer_id": peer_id,
            "metadata": dict(metadata or {}),
        }
        if related_goal_id:
            state.refs.related_goal_refs.append(related_goal_id)
        if related_work_item_id:
            state.refs.reused_work_item_refs.append(related_work_item_id)
        self._load_project_context(state, project)
        append_taskflow_event(
            state,
            TaskflowEventType.DISCOVERY_STARTED,
            actor=peer_id or "",
            payload={"raw_goal": raw_goal.strip()},
        )
        self._refresh_derived_state(state, project)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(state.meta.taskflow_id)

    def get_taskflow_state(self, taskflow_id: str) -> TaskflowState:
        """Return a defensive copy of a TaskflowState snapshot."""

        return self.state_store.get_taskflow_state(taskflow_id)

    def save_taskflow_state(self, state: TaskflowState) -> None:
        """Persist a TaskflowState snapshot."""

        state.touch()
        self.state_store.save_taskflow_state(state)

    def record_discovery_turn(
        self,
        taskflow_id: str,
        *,
        actor: str = "agent",
        goal_statement: str | None = None,
        background_delta: str | None = None,
        scope_in: Sequence[str] | None = None,
        scope_out: Sequence[str] | None = None,
        deferred_scope: Sequence[str] | None = None,
        success_criteria: Sequence[str] | None = None,
        assumptions: Sequence[Assumption | dict[str, Any]] | None = None,
        questions: Sequence[OpenQuestion | dict[str, Any]] | None = None,
        rules: Sequence[RuleRecord | dict[str, Any]] | None = None,
        examples: Sequence[ExampleRecord | dict[str, Any]] | None = None,
        scenarios: Sequence[ScenarioRecord | dict[str, Any]] | None = None,
        acceptance_examples: Sequence[AcceptanceExample | dict[str, Any]] | None = None,
        decisions: Sequence[DecisionRecord | dict[str, Any]] | None = None,
        work_item_candidates: Sequence[WorkItemCandidate | dict[str, Any]] | None = None,
        complexity_evidence: Sequence[ComplexityEvidenceRecord | dict[str, Any]] | None = None,
    ) -> TaskflowState:
        """Record one structured discovery turn and version a draft brief."""

        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.discovery_service.record_turn(
            state,
            actor=actor,
            goal_statement=goal_statement,
            background_delta=background_delta,
            scope_in=None if scope_in is None else _string_list(scope_in),
            scope_out=None if scope_out is None else _string_list(scope_out),
            deferred_scope=(
                None if deferred_scope is None else _string_list(deferred_scope)
            ),
            success_criteria=(
                None if success_criteria is None else _string_list(success_criteria)
            ),
            assumptions=list(assumptions or []),
            questions=list(questions or []),
            rules=list(rules or []),
            examples=list(examples or []),
            scenarios=list(scenarios or []),
            acceptance_examples=list(acceptance_examples or []),
            decisions=list(decisions or []),
            work_item_candidates=list(work_item_candidates or []),
        )
        if complexity_evidence is not None:
            self.complexity_service.record_evidence(
                state,
                complexity_evidence,
                actor=actor,
            )
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        state.meta.status = TaskflowStatus.CLARIFYING
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def answer_question(
        self,
        taskflow_id: str,
        *,
        question_id: str,
        answer: str | list[str],
        actor: str = "user",
        rationale: str = "",
        confidence: float | None = None,
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.discovery_service.answer_question(
            state,
            question_id=question_id,
            answer=answer,
            actor=actor,
            rationale=rationale,
            confidence=confidence,
        )
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def answer_decision(
        self,
        taskflow_id: str,
        *,
        decision_id: str,
        selected_option_id: str | None = None,
        answer: str | list[str] | None = None,
        rationale: str = "",
        actor: str = "user",
        source: str = "api",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.discovery_service.answer_decision(
            state,
            decision_id=decision_id,
            selected_option_id=selected_option_id,
            answer=answer,
            rationale=rationale,
            actor=actor,
            source=source,
        )
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def record_complexity_evidence(
        self,
        taskflow_id: str,
        *,
        evidence: Sequence[ComplexityEvidenceRecord | dict[str, Any]],
        actor: str = "agent",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.complexity_service.record_evidence(state, evidence, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def override_complexity(
        self,
        taskflow_id: str,
        *,
        level: str,
        reason: str,
        actor: str = "user",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.complexity_service.override(
            state,
            level=level,
            reason=reason,
            actor=actor,
        )
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def refresh_complexity_assessment(self, taskflow_id: str) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def record_rule_example(
        self,
        taskflow_id: str,
        *,
        actor: str = "agent",
        rules: Sequence[RuleRecord | dict[str, Any]] | None = None,
        examples: Sequence[ExampleRecord | dict[str, Any]] | None = None,
        acceptance_examples: Sequence[AcceptanceExample | dict[str, Any]] | None = None,
    ) -> TaskflowState:
        return self.record_discovery_turn(
            taskflow_id,
            actor=actor,
            rules=rules,
            examples=examples,
            acceptance_examples=acceptance_examples,
        )

    def compile_brief_draft(
        self, taskflow_id: str, *, actor: str = "agent"
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def mark_brief_ready(
        self,
        taskflow_id: str,
        *,
        version: int | None = None,
        actor: str = "agent",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.brief_service.mark_ready(state, version=version, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def confirm_brief(
        self,
        taskflow_id: str,
        *,
        version: int | None = None,
        actor: str = "user",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        if state.meta.status == TaskflowStatus.CANCELLED:
            raise ValueError("cancelled taskflow cannot be confirmed")
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self.brief_service.confirm(state, version=version, actor=actor)
        state.meta.status = TaskflowStatus.CONFIRMED
        state.compiler.traceability_index["confirmed_by"] = [actor]
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def answer_review_card(
        self,
        taskflow_id: str,
        *,
        card_id: str,
        action: str,
        value: Any = None,
        actor: str = "user",
        comment: str = "",
    ) -> ReviewCardAnswer:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        answer = self.review_service.answer_card(
            state,
            card_id=card_id,
            action=action,
            value=value,
            actor=actor,
            comment=comment,
        )
        if action in {"accept_recommendation", "choose_option"} and ":decision:" in card_id:
            decision_id = card_id.rsplit(":", 1)[-1]
            selected = value
            if selected is None:
                for decision in state.design.local_decisions:
                    if decision.id == decision_id:
                        selected = decision.recommended
                        break
            self.discovery_service.answer_decision(
                state,
                decision_id=decision_id,
                selected_option_id=str(selected) if selected is not None else None,
                answer=str(selected) if selected is not None else None,
                rationale=comment,
                actor=actor,
                source="review_card",
            )
        self._refresh_derived_state(state, project)
        self.brief_service.compile_draft(state, actor=actor)
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return answer

    def clarify_goal(
        self,
        taskflow_id: str,
        *,
        goal_statement: str | None = None,
        background_delta: str | None = None,
        scope_in: Sequence[str] | None = None,
        scope_out: Sequence[str] | None = None,
        deferred_scope: Sequence[str] | None = None,
        success_criteria: Sequence[str] | None = None,
        assumptions: Sequence[Assumption | dict[str, Any]] | None = None,
        work_item_candidates: Sequence[WorkItemCandidate | dict[str, Any]] | None = None,
        readiness_gates: Sequence[ReadinessGate | dict[str, Any]] | None = None,
        readiness_score: int | None = None,
    ) -> TaskflowState:
        """Compatibility entrypoint that delegates structured writes."""

        self.record_discovery_turn(
            taskflow_id,
            actor="agent",
            goal_statement=goal_statement,
            background_delta=background_delta,
            scope_in=scope_in,
            scope_out=scope_out,
            deferred_scope=deferred_scope,
            success_criteria=success_criteria,
            assumptions=assumptions,
            work_item_candidates=work_item_candidates,
        )
        state = self.get_taskflow_state(taskflow_id)
        if readiness_gates is not None:
            state.compiler.readiness_gates = [
                item
                if isinstance(item, ReadinessGate)
                else ReadinessGate.from_dict(dict(item))
                for item in readiness_gates
            ]
        if readiness_score is not None:
            state.compiler.readiness_score = max(0, min(100, int(readiness_score)))
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def confirm_goal(self, taskflow_id: str, *, confirmed_by: str = "user") -> TaskflowState:
        """Confirm the current brief before plan compilation."""

        state = self.get_taskflow_state(taskflow_id)
        if state.outputs.current_brief_version is None:
            self.compile_brief_draft(taskflow_id, actor=confirmed_by)
        state = self.get_taskflow_state(taskflow_id)
        current = state.outputs.current_brief_version
        self.mark_brief_ready(taskflow_id, version=current, actor=confirmed_by)
        return self.confirm_brief(
            taskflow_id,
            version=current,
            actor=confirmed_by,
        )

    def compile_goal(self, taskflow_id: str) -> PlanDraft:
        """Compile the confirmed Goal and persist ProjectState deltas."""

        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self._refresh_derived_state(state, project)
        if state.outputs.confirmed_brief_version is None:
            raise ValueError("taskflow goal requires a confirmed brief before compile")
        self._assert_current_brief_confirmed(state)
        unresolved = state.unresolved_high_risk_questions()
        if unresolved:
            ids = ", ".join(question.id for question in unresolved)
            raise ValueError(f"unresolved high-risk questions block compile: {ids}")
        compile_blockers = state.failed_compile_gates()
        if compile_blockers:
            names = ", ".join(gate.name for gate in compile_blockers)
            raise ValueError(f"readiness gate failed: {names}")
        if state.meta.status != TaskflowStatus.CONFIRMED:
            raise ValueError("taskflow goal must be confirmed before compile")
        plan = self.compiler.compile(state, project)

        # Logic from docs/文档.md Sections 5.7 and 5.8: create or reuse
        # WorkItem first, then create explicit GoalWorkLink and TraceLinks.
        compiled_by_id = {
            candidate.candidate_id: candidate for candidate in plan.work_item_candidates
        }
        for compiled in plan.work_item_candidates:
            if compiled.action == "create":
                project.upsert_work_item(self._work_item_from_compiled(plan, compiled))
            elif compiled.work_item_id not in state.refs.reused_work_item_refs:
                state.refs.reused_work_item_refs.append(compiled.work_item_id)
        for link in plan.goal_work_links:
            project.add_goal_work_link(link)
        for link in [*plan.decision_work_links, *plan.acceptance_trace_links]:
            project.add_trace_link(link)

        state.relations.goal_work_links = list(plan.goal_work_links)
        state.relations.decision_work_links = list(plan.decision_work_links)
        state.relations.acceptance_trace_links = list(plan.acceptance_trace_links)
        state.outputs.plan_drafts.append(plan.to_dict())
        state.compiler.traceability_index["compiled_work_items"] = [
            item.work_item_id for item in compiled_by_id.values()
        ]
        append_taskflow_event(
            state,
            TaskflowEventType.PLAN_COMPILED,
            payload={
                "plan_id": plan.id,
                "work_item_ids": [item.work_item_id for item in compiled_by_id.values()],
            },
        )
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        state.meta.status = (
            TaskflowStatus.READY_FOR_DISPATCH
            if not state.failed_dispatch_gates()
            and state.compiler.dispatch_readiness_score >= 70
            else TaskflowStatus.COMPILED
        )

        self.project_service.save_project_state(project)
        self.save_taskflow_state(state)
        self._compiled_plans[taskflow_id] = plan
        return plan

    def request_dispatch_decision(
        self,
        taskflow_id: str,
        *,
        work_item_ids: Sequence[str] | None = None,
        actor: str = "user",
        rationale: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DispatchDecisionRecord:
        """Request explicit user confirmation before creating TaskRuns."""

        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        self._refresh_derived_state(state, project)
        self._assert_current_brief_confirmed(state)
        if not state.outputs.plan_drafts:
            raise ValueError("taskflow goal must be compiled before dispatch confirmation")
        blockers = state.failed_compile_gates()
        if blockers:
            names = ", ".join(gate.name for gate in blockers)
            raise ValueError(f"readiness gate failed: {names}")
        covered = _string_list(work_item_ids) or list(
            state.compiler.traceability_index.get("compiled_work_items", [])
        )
        if not covered:
            raise ValueError("dispatch decision requires at least one work item")
        decision = DispatchDecisionRecord(
            id=_new_id("dispatch-decision"),
            brief_version=int(state.outputs.confirmed_brief_version or 0),
            work_item_ids=covered,
            status=DispatchDecisionStatus.REQUESTED,
            requested_by=actor,
            rationale=rationale,
            readiness_snapshot=[gate.to_dict() for gate in state.compiler.readiness_gates],
            metadata=dict(metadata or {}),
        )
        state.outputs.dispatch_decisions.append(decision)
        append_taskflow_event(
            state,
            TaskflowEventType.DISPATCH_CONFIRM_REQUESTED,
            actor=actor,
            payload=decision.to_dict(),
        )
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        self.save_taskflow_state(state)
        return decision

    def confirm_dispatch_decision(
        self,
        taskflow_id: str,
        *,
        decision_id: str,
        actor: str = "user",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        decision = self._dispatch_decision(state, decision_id)
        if decision.brief_version != state.outputs.confirmed_brief_version:
            raise ValueError("dispatch decision brief version is no longer current")
        decision.status = DispatchDecisionStatus.CONFIRMED
        decision.confirmed_by = actor
        decision.confirmed_at = decision.confirmed_at or utc_now()
        append_taskflow_event(
            state,
            TaskflowEventType.DISPATCH_CONFIRMED,
            actor=actor,
            payload=decision.to_dict(),
        )
        self._refresh_derived_state(state, project)
        self.brief_service.sync_diagnostics(state)
        if not state.failed_dispatch_gates() and state.compiler.dispatch_readiness_score >= 70:
            state.meta.status = TaskflowStatus.READY_FOR_DISPATCH
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def reject_dispatch_decision(
        self,
        taskflow_id: str,
        *,
        decision_id: str,
        actor: str = "user",
    ) -> TaskflowState:
        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        decision = self._dispatch_decision(state, decision_id)
        decision.status = DispatchDecisionStatus.REJECTED
        decision.rejected_by = actor
        decision.rejected_at = decision.rejected_at or utc_now()
        append_taskflow_event(
            state,
            TaskflowEventType.DISPATCH_REJECTED,
            actor=actor,
            payload=decision.to_dict(),
        )
        self._refresh_derived_state(state, project)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def dispatch_task_run(
        self,
        taskflow_id: str,
        *,
        work_item_id: str,
        dispatch_decision_id: str | None = None,
        executor_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
        executor: TaskRunExecutor | str = TaskRunExecutor.AGENT,
    ) -> TaskRun:
        """Create and optionally dispatch one fresh TaskRun for a WorkItem.

        Logic from ``docs/文档.md`` Section 5.9: TaskRun is never reused by
        default; each dispatch is a fresh execution instance for a WorkItem.
        """

        state = self.get_taskflow_state(taskflow_id)
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        if not dispatch_decision_id:
            raise ValueError("dispatch_decision_id is required before dispatch")
        dispatch_decision = self._confirmed_dispatch_decision(
            state,
            dispatch_decision_id,
            work_item_id=work_item_id,
        )
        self._refresh_derived_state(state, project)
        failed = state.failed_dispatch_gates()
        if failed:
            names = ", ".join(gate.name for gate in failed)
            raise ValueError(f"readiness gate failed: {names}")
        if state.compiler.dispatch_readiness_score < 70:
            raise ValueError("readiness score is below dispatch threshold")
        plan = self._compiled_plans.get(taskflow_id)
        if plan is None and state.outputs.plan_drafts:
            plan = self.compiler.compile(state, project)
            self._compiled_plans[taskflow_id] = plan
        if plan is None:
            raise ValueError("taskflow goal must be compiled before dispatch")
        compiled = self._compiled_work_item(plan, project, work_item_id)
        run = TaskRun(
            id=_new_id("task-run"),
            project_id=plan.project_id,
            goal_id=plan.goal_id,
            work_item_id=compiled.work_item_id,
            status=TaskRunStatus.PENDING,
            executor=executor,
            dispatch_ref_id=dispatch_decision.id,
            metadata={
                "taskflow_id": taskflow_id,
                "dispatch_decision_id": dispatch_decision.id,
                "source_brief_version": dispatch_decision.brief_version,
                "candidate_id": compiled.candidate_id,
                "work_item_title": compiled.title,
                "work_item_description": compiled.description,
                "work_item_type": str(getattr(compiled.type, "value", compiled.type)),
                "dispatch_requested_at": utc_now(),
                **dict(compiled.metadata),
                **dict(metadata or {}),
            },
        )
        dispatch_result: TaskflowDispatchResult | None = None
        if self.dispatcher is not None:
            dispatch_result = self.dispatcher.dispatch_task_run(
                run,
                executor_hint=executor_hint,
                metadata=metadata,
            )
            self._apply_dispatch_result(run, dispatch_result)
        project.add_task_run(run)
        project.add_trace_link(
            TraceLink(
                id=_new_id("trace"),
                project_id=plan.project_id,
                source_type=TraceEntityType.WORK_ITEM,
                source_id=compiled.work_item_id,
                target_type=TraceEntityType.TASK_RUN,
                target_id=run.id,
                relation_type=TraceRelationType.PRODUCES,
                rationale="Taskflow dispatch created a fresh TaskRun for this WorkItem.",
            )
        )
        project.add_trace_link(
            TraceLink(
                id=_new_id("trace"),
                project_id=plan.project_id,
                source_type=TraceEntityType.DISPATCH_DECISION,
                source_id=dispatch_decision.id,
                target_type=TraceEntityType.TASK_RUN,
                target_id=run.id,
                relation_type=TraceRelationType.DISPATCHES,
                rationale="Confirmed dispatch decision authorized this TaskRun.",
            )
        )
        acceptance = compiled.metadata.get("acceptance", {})
        brief_version = acceptance.get("source_brief_version")
        if brief_version is not None:
            project.add_trace_link(
                TraceLink(
                    id=_new_id("trace"),
                    project_id=plan.project_id,
                    source_type=TraceEntityType.BRIEF,
                    source_id=f"brief-v{brief_version}",
                    target_type=TraceEntityType.TASK_RUN,
                    target_id=run.id,
                    relation_type=TraceRelationType.EXPLAINS,
                    rationale=f"Brief v{brief_version} explains this TaskRun.",
                )
            )
        for rule_id in acceptance.get("rule_ids", []):
            project.add_trace_link(
                TraceLink(
                    id=_new_id("trace"),
                    project_id=plan.project_id,
                    source_type=TraceEntityType.RULE,
                    source_id=str(rule_id),
                    target_type=TraceEntityType.TASK_RUN,
                    target_id=run.id,
                    relation_type=TraceRelationType.VALIDATES,
                    rationale=f"Rule {rule_id} validates this TaskRun.",
                )
            )
        for scenario_id in acceptance.get("scenario_ids", []):
            project.add_trace_link(
                TraceLink(
                    id=_new_id("trace"),
                    project_id=plan.project_id,
                    source_type=TraceEntityType.SCENARIO,
                    source_id=str(scenario_id),
                    target_type=TraceEntityType.TASK_RUN,
                    target_id=run.id,
                    relation_type=TraceRelationType.VALIDATES,
                    rationale=f"Scenario {scenario_id} validates this TaskRun.",
                )
            )
        agent_run_ref = (
            dispatch_result.agent_run_ref
            if dispatch_result is not None and isinstance(dispatch_result.agent_run_ref, dict)
            else None
        )
        agent_run_id = str(agent_run_ref.get("id") or "") if agent_run_ref else ""
        if agent_run_id:
            project.add_trace_link(
                TraceLink(
                    id=_new_id("trace"),
                    project_id=plan.project_id,
                    source_type=TraceEntityType.TASK_RUN,
                    source_id=run.id,
                    target_type=TraceEntityType.AGENT_RUN,
                    target_id=agent_run_id,
                    relation_type=TraceRelationType.DISPATCHES,
                    rationale="TaskRun dispatched execution to an AgentRun.",
                )
            )

        if run.id not in state.outputs.task_run_refs:
            state.outputs.task_run_refs.append(run.id)
        append_taskflow_event(
            state,
            TaskflowEventType.DISPATCH_REQUESTED,
            payload={
                "task_run_id": run.id,
                "work_item_id": compiled.work_item_id,
                "dispatch_decision_id": dispatch_decision.id,
            },
        )
        if run.status == TaskRunStatus.DISPATCHED:
            state.meta.status = TaskflowStatus.DISPATCHED
        self.project_service.save_project_state(project)
        self.save_taskflow_state(state)
        return run

    def render_review_cards(self, taskflow_id: str) -> list[ReviewCard]:
        """Render review cards for the current TaskflowState."""

        return self.card_renderer.render(self.get_taskflow_state(taskflow_id))

    def _load_project_context(self, state: TaskflowState, project: ProjectState) -> None:
        state.refs.project_context_refs = [
            "project_profile",
            "knowledge_base",
            "decisions",
            "work_items",
            "traceability",
            "projections",
        ]
        state.refs.reused_work_item_refs.extend(
            item.id for item in project.work_items.reusable_work_items
        )
        state.refs.shared_decision_refs.extend(
            item.id
            for item in [
                *project.decisions.project_decisions,
                *project.decisions.architecture_decisions,
                *project.decisions.policy_decisions,
            ]
            if getattr(item, "id", "")
        )
        state.refs.related_artifact_refs.extend(
            item.id for item in project.projections.artifacts
        )

    def _refresh_derived_state(
        self, state: TaskflowState, project: ProjectState
    ) -> None:
        assessment = self.complexity_service.assess(state, project)
        self.readiness_service.refresh(state, assessment)

    def _assert_current_brief_confirmed(self, state: TaskflowState) -> None:
        if state.outputs.current_brief_version != state.outputs.confirmed_brief_version:
            raise ValueError("current brief version must be confirmed before compile")
        for brief in state.outputs.brief_versions:
            if brief.version == state.outputs.confirmed_brief_version:
                if brief.status != BriefStatus.CONFIRMED:
                    raise ValueError("current brief version must be confirmed before compile")
                return
        raise ValueError("confirmed brief version not found")

    def _dispatch_decision(
        self, state: TaskflowState, decision_id: str
    ) -> DispatchDecisionRecord:
        for decision in state.outputs.dispatch_decisions:
            if decision.id == decision_id:
                return decision
        raise ValueError(f"dispatch decision not found: {decision_id}")

    def _confirmed_dispatch_decision(
        self,
        state: TaskflowState,
        decision_id: str,
        *,
        work_item_id: str,
    ) -> DispatchDecisionRecord:
        decision = self._dispatch_decision(state, decision_id)
        if decision.status != DispatchDecisionStatus.CONFIRMED:
            raise ValueError("dispatch decision must be confirmed before dispatch")
        if decision.brief_version != state.outputs.confirmed_brief_version:
            raise ValueError("dispatch decision brief version is no longer current")
        if work_item_id not in decision.work_item_ids:
            raise ValueError("dispatch decision does not cover work item")
        return decision

    def _work_item_from_compiled(
        self, plan: PlanDraft, compiled: CompiledWorkItemCandidate
    ) -> WorkItem:
        derived_from = compiled.metadata.get("derived_from")
        return WorkItem(
            id=compiled.work_item_id,
            project_id=plan.project_id,
            title=compiled.title,
            description=compiled.description,
            type=compiled.type,
            status=WorkItemStatus.READY,
            acceptance_refs=list(compiled.acceptance_refs),
            decision_refs=list(compiled.decision_refs),
            artifact_refs=list(compiled.artifact_refs),
            dedupe_key=compiled.dedupe_key,
            derived_from=str(derived_from) if derived_from else None,
            depends_on=list(compiled.depends_on),
            metadata={
                "created_from_plan_draft_id": plan.id,
                "created_from_taskflow_id": plan.taskflow_id,
                "candidate_id": compiled.candidate_id,
                **dict(compiled.metadata),
            },
        )

    def _compiled_work_item(
        self, plan: PlanDraft, project: ProjectState, work_item_id: str
    ) -> CompiledWorkItemCandidate:
        for compiled in plan.work_item_candidates:
            if compiled.work_item_id == work_item_id:
                return compiled
        for item in [
            *project.work_items.active_work_items,
            *project.work_items.reusable_work_items,
        ]:
            if item.id == work_item_id:
                return CompiledWorkItemCandidate(
                    candidate_id=work_item_id,
                    title=item.title,
                    description=item.description,
                    type=item.type,
                    action="reuse",
                    work_item_id=item.id,
                    reuse_work_item_id=item.id,
                    acceptance_refs=list(item.acceptance_refs),
                    decision_refs=list(item.decision_refs),
                    artifact_refs=list(item.artifact_refs),
                    dedupe_key=item.dedupe_key,
                    depends_on=list(item.depends_on),
                    metadata=dict(item.metadata),
                )
        raise KeyError(f"compiled work item not found: {work_item_id}")

    def _apply_dispatch_result(
        self, run: TaskRun, result: TaskflowDispatchResult
    ) -> None:
        run.metadata["dispatch_result"] = {
            "selected_executor_id": result.selected_executor_id,
            "reason": result.reason,
            "manual_override": result.manual_override,
            "candidates": list(result.candidates),
            "filtered": list(result.filtered),
            "score_summary": dict(result.score_summary),
        }
        if result.selected_executor_id:
            run.status = TaskRunStatus.DISPATCHED
            run.metadata["selected_executor_id"] = result.selected_executor_id
        run.updated_at = utc_now()


__all__ = ["TaskflowService"]
