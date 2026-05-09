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
from labrastro_server.taskflow.domain.complexity import ComplexityEstimator
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
    ReadinessGate,
    TaskflowState,
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
        card_renderer: CardRenderer | None = None,
        dispatcher: TaskflowDispatcher | None = None,
        state_store: TaskflowStateStore | None = None,
    ) -> None:
        self.project_service = project_service or ProjectService()
        self.compiler = compiler or PlanCompiler()
        self.complexity_estimator = complexity_estimator or ComplexityEstimator()
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
        self._apply_complexity_estimate(state)
        self.save_taskflow_state(state)
        return self.get_taskflow_state(state.meta.taskflow_id)

    def get_taskflow_state(self, taskflow_id: str) -> TaskflowState:
        """Return a defensive copy of a TaskflowState snapshot."""

        return self.state_store.get_taskflow_state(taskflow_id)

    def save_taskflow_state(self, state: TaskflowState) -> None:
        """Persist a TaskflowState snapshot."""

        state.touch()
        self.state_store.save_taskflow_state(state)

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
        """Update the clarification and compiler state for the current Goal."""

        state = self.get_taskflow_state(taskflow_id)
        if goal_statement is not None:
            state.intent.goal_statement = goal_statement
        if background_delta is not None:
            state.intent.background_delta = background_delta
        if scope_in is not None:
            state.intent.scope_in = _string_list(scope_in)
        if scope_out is not None:
            state.intent.scope_out = _string_list(scope_out)
        if deferred_scope is not None:
            state.intent.deferred_scope = _string_list(deferred_scope)
        if success_criteria is not None:
            state.intent.success_criteria = _string_list(success_criteria)
        if assumptions is not None:
            state.clarification.assumptions = [
                item if isinstance(item, Assumption) else Assumption.from_dict(dict(item))
                for item in assumptions
            ]
        if work_item_candidates is not None:
            state.outputs.work_item_candidates = [
                item
                if isinstance(item, WorkItemCandidate)
                else WorkItemCandidate.from_dict(dict(item))
                for item in work_item_candidates
            ]
        if readiness_gates is not None:
            state.compiler.readiness_gates = [
                item
                if isinstance(item, ReadinessGate)
                else ReadinessGate.from_dict(dict(item))
                for item in readiness_gates
            ]
        if readiness_score is not None:
            state.compiler.readiness_score = max(0, min(100, int(readiness_score)))
        state.meta.status = TaskflowStatus.CLARIFYING
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def confirm_goal(self, taskflow_id: str, *, confirmed_by: str = "user") -> TaskflowState:
        """Confirm the Goal before plan compilation.

        Logic from ``docs/文档.md`` Section 5.4: runtime tasks are still not
        created here; confirmation only marks the Goal snapshot ready to compile.
        """

        state = self.get_taskflow_state(taskflow_id)
        if state.meta.status == TaskflowStatus.CANCELLED:
            raise ValueError("cancelled taskflow cannot be confirmed")
        state.meta.status = TaskflowStatus.CONFIRMED
        state.compiler.traceability_index.setdefault("confirmed_by", [confirmed_by])
        self.save_taskflow_state(state)
        return self.get_taskflow_state(taskflow_id)

    def compile_goal(self, taskflow_id: str) -> PlanDraft:
        """Compile the confirmed Goal and persist ProjectState deltas."""

        state = self.get_taskflow_state(taskflow_id)
        if state.meta.status != TaskflowStatus.CONFIRMED:
            raise ValueError("taskflow goal must be confirmed before compile")
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
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
        state.meta.status = (
            TaskflowStatus.READY_FOR_DISPATCH
            if not state.failed_required_gates() and state.compiler.readiness_score >= 70
            else TaskflowStatus.COMPILED
        )

        self.project_service.save_project_state(project)
        self.save_taskflow_state(state)
        self._compiled_plans[taskflow_id] = plan
        return plan

    def dispatch_task_run(
        self,
        taskflow_id: str,
        *,
        work_item_id: str,
        executor_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
        executor: TaskRunExecutor | str = TaskRunExecutor.AGENT,
    ) -> TaskRun:
        """Create and optionally dispatch one fresh TaskRun for a WorkItem.

        Logic from ``docs/文档.md`` Section 5.9: TaskRun is never reused by
        default; each dispatch is a fresh execution instance for a WorkItem.
        """

        state = self.get_taskflow_state(taskflow_id)
        failed = state.failed_required_gates()
        if failed:
            names = ", ".join(gate.name for gate in failed)
            raise ValueError(f"readiness gate failed: {names}")
        if state.compiler.readiness_score < 70:
            raise ValueError("readiness score is below dispatch threshold")
        plan = self._compiled_plans.get(taskflow_id)
        if plan is None:
            raise ValueError("taskflow goal must be compiled before dispatch")
        project = self.project_service.get_or_create_project_state(state.meta.project_id)
        compiled = self._compiled_work_item(plan, project, work_item_id)
        run = TaskRun(
            id=_new_id("task-run"),
            project_id=plan.project_id,
            goal_id=plan.goal_id,
            work_item_id=compiled.work_item_id,
            status=TaskRunStatus.PENDING,
            executor=executor,
            metadata={
                "taskflow_id": taskflow_id,
                "candidate_id": compiled.candidate_id,
                "work_item_title": compiled.title,
                "work_item_description": compiled.description,
                "work_item_type": str(getattr(compiled.type, "value", compiled.type)),
                "dispatch_requested_at": utc_now(),
                **dict(compiled.metadata),
                **dict(metadata or {}),
            },
        )
        if self.dispatcher is not None:
            result = self.dispatcher.dispatch_task_run(
                run,
                executor_hint=executor_hint,
                metadata=metadata,
            )
            self._apply_dispatch_result(run, result)
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

        if run.id not in state.outputs.task_run_refs:
            state.outputs.task_run_refs.append(run.id)
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

    def _apply_complexity_estimate(self, state: TaskflowState) -> None:
        signals = {
            "goal_clarity": 2 if len(state.intent.goal_statement.split()) < 8 else 1,
            "business_impact": 1,
            "domain_complexity": 1,
            "technical_risk": 1,
        }
        estimate = self.complexity_estimator.estimate(signals)
        state.compiler.complexity_level = estimate.level.name
        state.compiler.recipe_id = estimate.recipe_id
        state.compiler.traceability_index["recipe_steps"] = list(estimate.recipe_steps)

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
        if result.runtime_task is not None:
            run.metadata["runtime_task"] = dict(result.runtime_task)
        if result.runtime_task_id:
            run.runtime_task_id = result.runtime_task_id
        if result.selected_executor_id:
            run.status = TaskRunStatus.DISPATCHED
            run.metadata["selected_executor_id"] = result.selected_executor_id
        run.updated_at = utc_now()


__all__ = ["TaskflowService"]
