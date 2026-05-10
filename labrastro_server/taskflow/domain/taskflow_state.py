"""Single-goal Taskflow compiler state.

This module implements ``TaskflowState`` from ``docs/文档.md`` Section 4.
The state is deliberately scoped to one Goal conversation. Long-lived project
knowledge belongs in ``ProjectState``; this state keeps refs and deltas needed
to compile the current Goal into reusable WorkItems and concrete TaskRuns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.taskflow.domain.project_state import (
    Constraint,
    GoalWorkLink,
    GoalWorkRelationType,
    Stakeholder,
    TraceLink,
    WorkItemType,
)


def _enum_value(value: Enum | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


class TaskflowStatus(str, Enum):
    """Taskflow compiler session status from the Section 5 flow."""

    DRAFT = "draft"
    CLARIFYING = "clarifying"
    CONFIRMED = "confirmed"
    COMPILED = "compiled"
    READY_FOR_DISPATCH = "ready_for_dispatch"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"


class Sensitivity(str, Enum):
    """Sensitivity marker for TaskflowState data."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


class ConfirmationState(str, Enum):
    """Explicit confirmation ledger required by ``docs/方向建议.md`` Section 11.7."""

    SUGGESTED_BY_SYSTEM = "suggested_by_system"
    INFERRED_FROM_CONTEXT = "inferred_from_context"
    CONFIRMED_BY_USER = "confirmed_by_user"
    REJECTED_BY_USER = "rejected_by_user"
    UNRESOLVED = "unresolved"
    DEPRECATED = "deprecated"


@dataclass(slots=True)
class TaskflowMeta:
    """Identity and lifecycle metadata for one compiler session."""

    taskflow_id: str
    project_id: str
    goal_id: str
    schema_version: str = "taskflow.state.v1"
    state_version: int = 1
    status: TaskflowStatus | str = TaskflowStatus.DRAFT
    sensitivity: Sensitivity | str = Sensitivity.INTERNAL
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.status = TaskflowStatus(_enum_value(self.status))
        self.sensitivity = Sensitivity(_enum_value(self.sensitivity))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowMeta":
        return cls(
            taskflow_id=str(data.get("taskflow_id") or ""),
            project_id=str(data.get("project_id") or ""),
            goal_id=str(data.get("goal_id") or ""),
            schema_version=str(data.get("schema_version") or "taskflow.state.v1"),
            state_version=int(data.get("state_version") or 1),
            status=str(data.get("status") or TaskflowStatus.DRAFT.value),
            sensitivity=str(data.get("sensitivity") or Sensitivity.INTERNAL.value),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "taskflow_id": self.taskflow_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "schema_version": self.schema_version,
            "state_version": self.state_version,
            "status": self.status.value,
            "sensitivity": self.sensitivity.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class TaskflowRefs:
    """References into ProjectState instead of copying full long-lived context."""

    project_context_refs: list[str] = field(default_factory=list)
    prior_goal_refs: list[str] = field(default_factory=list)
    related_goal_refs: list[str] = field(default_factory=list)
    reused_work_item_refs: list[str] = field(default_factory=list)
    shared_decision_refs: list[str] = field(default_factory=list)
    related_artifact_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowRefs":
        return cls(
            project_context_refs=_string_list(data.get("project_context_refs")),
            prior_goal_refs=_string_list(data.get("prior_goal_refs")),
            related_goal_refs=_string_list(data.get("related_goal_refs")),
            reused_work_item_refs=_string_list(data.get("reused_work_item_refs")),
            shared_decision_refs=_string_list(data.get("shared_decision_refs")),
            related_artifact_refs=_string_list(data.get("related_artifact_refs")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_context_refs": list(self.project_context_refs),
            "prior_goal_refs": list(self.prior_goal_refs),
            "related_goal_refs": list(self.related_goal_refs),
            "reused_work_item_refs": list(self.reused_work_item_refs),
            "shared_decision_refs": list(self.shared_decision_refs),
            "related_artifact_refs": list(self.related_artifact_refs),
        }


@dataclass(slots=True)
class TaskflowIntent:
    """Current Goal intent, scope, success criteria, and project deltas."""

    goal_statement: str = ""
    background_delta: str = ""
    stakeholders_delta: list[Stakeholder] = field(default_factory=list)
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)
    deferred_scope: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    constraints_delta: list[Constraint] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowIntent":
        return cls(
            goal_statement=str(data.get("goal_statement") or ""),
            background_delta=str(data.get("background_delta") or ""),
            stakeholders_delta=[
                Stakeholder.from_dict(_dict(item))
                for item in _list(data.get("stakeholders_delta"))
            ],
            scope_in=_string_list(data.get("scope_in")),
            scope_out=_string_list(data.get("scope_out")),
            deferred_scope=_string_list(data.get("deferred_scope")),
            success_criteria=_string_list(data.get("success_criteria")),
            constraints_delta=[
                Constraint.from_dict(_dict(item))
                for item in _list(data.get("constraints_delta"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_statement": self.goal_statement,
            "background_delta": self.background_delta,
            "stakeholders_delta": [item.to_dict() for item in self.stakeholders_delta],
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "deferred_scope": list(self.deferred_scope),
            "success_criteria": list(self.success_criteria),
            "constraints_delta": [item.to_dict() for item in self.constraints_delta],
        }


@dataclass(slots=True)
class Assumption:
    """Assumption ledger item shown in AssumptionCard."""

    id: str
    statement: str
    impact: str = "medium"
    state: ConfirmationState | str = ConfirmationState.SUGGESTED_BY_SYSTEM
    reason: str = ""
    source_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = ConfirmationState(_enum_value(self.state))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assumption":
        return cls(
            id=str(data.get("id") or ""),
            statement=str(data.get("statement") or ""),
            impact=str(data.get("impact") or "medium"),
            state=str(data.get("state") or ConfirmationState.SUGGESTED_BY_SYSTEM.value),
            reason=str(data.get("reason") or ""),
            source_refs=_string_list(data.get("source_refs")),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "impact": self.impact,
            "state": self.state.value,
            "reason": self.reason,
            "source_refs": list(self.source_refs),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class OpenQuestion:
    """Structured question metadata from ``docs/方向建议.md`` Section 5."""

    id: str
    stage: str
    question: str
    why_needed: str
    answer_type: str = "free_text"
    options: list[str] = field(default_factory=list)
    default_suggestion: str | None = None
    risk_if_unknown: str = "medium"
    answered: bool = False
    answer: str | list[str] | None = None
    artifact_targets: list[str] = field(default_factory=list)
    skip_when: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpenQuestion":
        answer = data.get("answer")
        parsed_answer: str | list[str] | None
        if isinstance(answer, list):
            parsed_answer = _string_list(answer)
        elif answer is None:
            parsed_answer = None
        else:
            parsed_answer = str(answer)
        return cls(
            id=str(data.get("id") or ""),
            stage=str(data.get("stage") or ""),
            question=str(data.get("question") or ""),
            why_needed=str(data.get("why_needed") or ""),
            answer_type=str(data.get("answer_type") or "free_text"),
            options=_string_list(data.get("options")),
            default_suggestion=(
                str(data["default_suggestion"])
                if data.get("default_suggestion") is not None
                else None
            ),
            risk_if_unknown=str(data.get("risk_if_unknown") or "medium"),
            answered=bool(data.get("answered", False)),
            answer=parsed_answer,
            artifact_targets=_string_list(data.get("artifact_targets")),
            skip_when=_string_list(data.get("skip_when")),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "question": self.question,
            "why_needed": self.why_needed,
            "answer_type": self.answer_type,
            "options": list(self.options),
            "default_suggestion": self.default_suggestion,
            "risk_if_unknown": self.risk_if_unknown,
            "answered": self.answered,
            "answer": self.answer,
            "artifact_targets": list(self.artifact_targets),
            "skip_when": list(self.skip_when),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ScenarioRecord:
    """SBE/BDD-style scenario kept in natural language form."""

    id: str
    title: str
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    observable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScenarioRecord":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            given=_string_list(data.get("given")),
            when=_string_list(data.get("when")),
            then=_string_list(data.get("then")),
            observable=bool(data.get("observable", True)),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "given": list(self.given),
            "when": list(self.when),
            "then": list(self.then),
            "observable": self.observable,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class AcceptanceExample:
    """Acceptance example used by compiler traceability."""

    id: str
    title: str
    scenario_ref: str | None = None
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    state: ConfirmationState | str = ConfirmationState.SUGGESTED_BY_SYSTEM
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = ConfirmationState(_enum_value(self.state))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AcceptanceExample":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            scenario_ref=(
                str(data["scenario_ref"])
                if data.get("scenario_ref") is not None
                else None
            ),
            given=_string_list(data.get("given")),
            when=_string_list(data.get("when")),
            then=_string_list(data.get("then")),
            state=str(data.get("state") or ConfirmationState.SUGGESTED_BY_SYSTEM.value),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "scenario_ref": self.scenario_ref,
            "given": list(self.given),
            "when": list(self.when),
            "then": list(self.then),
            "state": self.state.value,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class TaskflowClarification:
    """Clarification ledger from the TaskflowState schema."""

    assumptions: list[Assumption] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    scenarios: list[ScenarioRecord] = field(default_factory=list)
    acceptance_examples: list[AcceptanceExample] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowClarification":
        return cls(
            assumptions=[
                Assumption.from_dict(_dict(item))
                for item in _list(data.get("assumptions"))
            ],
            open_questions=[
                OpenQuestion.from_dict(_dict(item))
                for item in _list(data.get("open_questions"))
            ],
            scenarios=[
                ScenarioRecord.from_dict(_dict(item))
                for item in _list(data.get("scenarios"))
            ],
            acceptance_examples=[
                AcceptanceExample.from_dict(_dict(item))
                for item in _list(data.get("acceptance_examples"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumptions": [item.to_dict() for item in self.assumptions],
            "open_questions": [item.to_dict() for item in self.open_questions],
            "scenarios": [item.to_dict() for item in self.scenarios],
            "acceptance_examples": [
                item.to_dict() for item in self.acceptance_examples
            ],
        }


@dataclass(slots=True)
class TaskflowDomain:
    """Domain deltas and DDD-lite references."""

    bounded_context_refs: list[str] = field(default_factory=list)
    domain_model_delta: dict[str, Any] = field(default_factory=dict)
    ubiquitous_language: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowDomain":
        return cls(
            bounded_context_refs=_string_list(data.get("bounded_context_refs")),
            domain_model_delta=_dict(data.get("domain_model_delta")),
            ubiquitous_language={
                str(key): str(value)
                for key, value in _dict(data.get("ubiquitous_language")).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bounded_context_refs": list(self.bounded_context_refs),
            "domain_model_delta": dict(self.domain_model_delta),
            "ubiquitous_language": dict(self.ubiquitous_language),
        }


@dataclass(slots=True)
class SolutionOption:
    """Candidate solution option for a DecisionCard."""

    id: str
    title: str
    summary: str = ""
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    recommended: bool = False
    decision_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SolutionOption":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            pros=_string_list(data.get("pros")),
            cons=_string_list(data.get("cons")),
            recommended=bool(data.get("recommended", False)),
            decision_ref=(
                str(data["decision_ref"]) if data.get("decision_ref") is not None else None
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "pros": list(self.pros),
            "cons": list(self.cons),
            "recommended": self.recommended,
            "decision_ref": self.decision_ref,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class DecisionRecord:
    """Local design/product decision with explicit confirmation state."""

    id: str
    topic: str
    options: list[str] = field(default_factory=list)
    recommended: str | None = None
    chosen: str | None = None
    rationale: str = ""
    state: ConfirmationState | str = ConfirmationState.SUGGESTED_BY_SYSTEM
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = ConfirmationState(_enum_value(self.state))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRecord":
        return cls(
            id=str(data.get("id") or ""),
            topic=str(data.get("topic") or data.get("question") or ""),
            options=_string_list(data.get("options")),
            recommended=(
                str(data["recommended"]) if data.get("recommended") is not None else None
            ),
            chosen=str(data["chosen"]) if data.get("chosen") is not None else None,
            rationale=str(data.get("rationale") or data.get("reason") or ""),
            state=str(data.get("state") or ConfirmationState.SUGGESTED_BY_SYSTEM.value),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "options": list(self.options),
            "recommended": self.recommended,
            "chosen": self.chosen,
            "rationale": self.rationale,
            "state": self.state.value,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class RiskRecord:
    """Risk and mitigation that can compile into spike/research work."""

    id: str
    statement: str
    impact: str = "medium"
    mitigation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskRecord":
        return cls(
            id=str(data.get("id") or ""),
            statement=str(data.get("statement") or ""),
            impact=str(data.get("impact") or "medium"),
            mitigation=str(data.get("mitigation") or ""),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "impact": self.impact,
            "mitigation": self.mitigation,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class InterfaceSpec:
    """API/event/data contract note from the design section."""

    id: str
    name: str
    kind: str = "api"
    contract: dict[str, Any] = field(default_factory=dict)
    producer: str = ""
    consumers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InterfaceSpec":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or "api"),
            contract=_dict(data.get("contract")),
            producer=str(data.get("producer") or ""),
            consumers=_string_list(data.get("consumers")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "contract": dict(self.contract),
            "producer": self.producer,
            "consumers": list(self.consumers),
        }


@dataclass(slots=True)
class TaskflowDesign:
    """Design section containing options, decisions, risks, and implementation notes."""

    solution_options: list[SolutionOption] = field(default_factory=list)
    local_decisions: list[DecisionRecord] = field(default_factory=list)
    risks: list[RiskRecord] = field(default_factory=list)
    interfaces: list[InterfaceSpec] = field(default_factory=list)
    implementation_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowDesign":
        return cls(
            solution_options=[
                SolutionOption.from_dict(_dict(item))
                for item in _list(data.get("solution_options"))
            ],
            local_decisions=[
                DecisionRecord.from_dict(_dict(item))
                for item in _list(data.get("local_decisions"))
            ],
            risks=[
                RiskRecord.from_dict(_dict(item)) for item in _list(data.get("risks"))
            ],
            interfaces=[
                InterfaceSpec.from_dict(_dict(item))
                for item in _list(data.get("interfaces"))
            ],
            implementation_notes=_string_list(data.get("implementation_notes")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "solution_options": [item.to_dict() for item in self.solution_options],
            "local_decisions": [item.to_dict() for item in self.local_decisions],
            "risks": [item.to_dict() for item in self.risks],
            "interfaces": [item.to_dict() for item in self.interfaces],
            "implementation_notes": list(self.implementation_notes),
        }


@dataclass(slots=True)
class TaskflowDelivery:
    """Rollout plan and future Goal candidates."""

    rollout_plan: list[str] = field(default_factory=list)
    future_goal_candidates: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowDelivery":
        return cls(
            rollout_plan=_string_list(data.get("rollout_plan")),
            future_goal_candidates=_string_list(data.get("future_goal_candidates")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollout_plan": list(self.rollout_plan),
            "future_goal_candidates": list(self.future_goal_candidates),
        }


@dataclass(slots=True)
class TaskflowRelations:
    """Compiled relations attached to this Goal session."""

    goal_work_links: list[GoalWorkLink] = field(default_factory=list)
    work_item_dependencies: list[TraceLink] = field(default_factory=list)
    decision_work_links: list[TraceLink] = field(default_factory=list)
    acceptance_trace_links: list[TraceLink] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowRelations":
        return cls(
            goal_work_links=[
                GoalWorkLink.from_dict(_dict(item))
                for item in _list(data.get("goal_work_links"))
            ],
            work_item_dependencies=[
                TraceLink.from_dict(_dict(item))
                for item in _list(data.get("work_item_dependencies"))
            ],
            decision_work_links=[
                TraceLink.from_dict(_dict(item))
                for item in _list(data.get("decision_work_links"))
            ],
            acceptance_trace_links=[
                TraceLink.from_dict(_dict(item))
                for item in _list(data.get("acceptance_trace_links"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_work_links": [item.to_dict() for item in self.goal_work_links],
            "work_item_dependencies": [
                item.to_dict() for item in self.work_item_dependencies
            ],
            "decision_work_links": [
                item.to_dict() for item in self.decision_work_links
            ],
            "acceptance_trace_links": [
                item.to_dict() for item in self.acceptance_trace_links
            ],
        }


@dataclass(slots=True)
class ReadinessGate:
    """Execution readiness gate from ``docs/方向建议.md`` Section 7."""

    id: str
    name: str
    passed: bool
    rationale: str = ""
    required: bool = True
    severity: str = "high"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReadinessGate":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            passed=bool(data.get("passed", False)),
            rationale=str(data.get("rationale") or ""),
            required=bool(data.get("required", True)),
            severity=str(data.get("severity") or "high"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "passed": self.passed,
            "rationale": self.rationale,
            "required": self.required,
            "severity": self.severity,
        }


@dataclass(slots=True)
class TaskflowCompilerState:
    """Compiler metadata and readiness state."""

    complexity_level: str = "L1"
    recipe_id: str = "small-task"
    readiness_score: int = 0
    readiness_gates: list[ReadinessGate] = field(default_factory=list)
    blocking_items: list[str] = field(default_factory=list)
    traceability_index: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowCompilerState":
        return cls(
            complexity_level=str(data.get("complexity_level") or "L1"),
            recipe_id=str(data.get("recipe_id") or "small-task"),
            readiness_score=int(data.get("readiness_score") or 0),
            readiness_gates=[
                ReadinessGate.from_dict(_dict(item))
                for item in _list(data.get("readiness_gates"))
            ],
            blocking_items=_string_list(data.get("blocking_items")),
            traceability_index={
                str(key): _string_list(value)
                for key, value in _dict(data.get("traceability_index")).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "complexity_level": self.complexity_level,
            "recipe_id": self.recipe_id,
            "readiness_score": self.readiness_score,
            "readiness_gates": [item.to_dict() for item in self.readiness_gates],
            "blocking_items": list(self.blocking_items),
            "traceability_index": {
                key: list(value) for key, value in self.traceability_index.items()
            },
        }


@dataclass(slots=True)
class WorkItemCandidate:
    """Goal-local candidate that can compile into or reuse a WorkItem."""

    id: str
    title: str
    description: str
    type: WorkItemType | str = WorkItemType.IMPLEMENTATION
    acceptance_refs: list[str] = field(default_factory=list)
    decision_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    scenario_refs: list[str] = field(default_factory=list)
    risk_refs: list[str] = field(default_factory=list)
    repo_ref: str | None = None
    workspace_ref: str | None = None
    dedupe_key: str | None = None
    derived_from: str | None = None
    depends_on: list[str] = field(default_factory=list)
    relation_type: GoalWorkRelationType | str = GoalWorkRelationType.DIRECT_DELIVERY
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = WorkItemType(_enum_value(self.type))
        self.relation_type = GoalWorkRelationType(_enum_value(self.relation_type))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkItemCandidate":
        removed_fields = [
            key
            for key in ("required_dispatch_tags", "preferred_dispatch_tags")
            if key in data
        ]
        if removed_fields:
            raise ValueError(
                "work item legacy task-matching fields were removed; select an Agent explicitly"
            )
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or data.get("prompt") or ""),
            type=str(data.get("type") or WorkItemType.IMPLEMENTATION.value),
            acceptance_refs=_string_list(data.get("acceptance_refs")),
            decision_refs=_string_list(data.get("decision_refs")),
            artifact_refs=_string_list(data.get("artifact_refs")),
            scenario_refs=_string_list(data.get("scenario_refs")),
            risk_refs=_string_list(data.get("risk_refs")),
            repo_ref=str(data["repo_ref"]) if data.get("repo_ref") is not None else None,
            workspace_ref=(
                str(data["workspace_ref"]) if data.get("workspace_ref") is not None else None
            ),
            dedupe_key=(
                str(data["dedupe_key"]) if data.get("dedupe_key") is not None else None
            ),
            derived_from=(
                str(data["derived_from"]) if data.get("derived_from") is not None else None
            ),
            depends_on=_string_list(data.get("depends_on")),
            relation_type=str(
                data.get("relation_type") or GoalWorkRelationType.DIRECT_DELIVERY.value
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "type": self.type.value,
            "acceptance_refs": list(self.acceptance_refs),
            "decision_refs": list(self.decision_refs),
            "artifact_refs": list(self.artifact_refs),
            "scenario_refs": list(self.scenario_refs),
            "risk_refs": list(self.risk_refs),
            "repo_ref": self.repo_ref,
            "workspace_ref": self.workspace_ref,
            "dedupe_key": self.dedupe_key,
            "derived_from": self.derived_from,
            "depends_on": list(self.depends_on),
            "relation_type": self.relation_type.value,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class TaskflowOutputs:
    """Compiler outputs and projections from the TaskflowState schema."""

    plan_drafts: list[dict[str, Any]] = field(default_factory=list)
    work_item_candidates: list[WorkItemCandidate] = field(default_factory=list)
    task_run_refs: list[str] = field(default_factory=list)
    artifact_projections: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowOutputs":
        return cls(
            plan_drafts=[_dict(item) for item in _list(data.get("plan_drafts"))],
            work_item_candidates=[
                WorkItemCandidate.from_dict(_dict(item))
                for item in _list(data.get("work_item_candidates"))
            ],
            task_run_refs=_string_list(data.get("task_run_refs")),
            artifact_projections=[
                _dict(item) for item in _list(data.get("artifact_projections"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_drafts": [dict(item) for item in self.plan_drafts],
            "work_item_candidates": [
                item.to_dict() for item in self.work_item_candidates
            ],
            "task_run_refs": list(self.task_run_refs),
            "artifact_projections": [
                dict(item) for item in self.artifact_projections
            ],
        }


@dataclass(slots=True)
class TaskflowState:
    """Single-Goal Taskflow conversation and compiler snapshot.

    Source: ``docs/文档.md`` Section 4. The top-level sections mirror the
    finalized schema exactly: ``meta``, ``refs``, ``intent``,
    ``clarification``, ``domain``, ``design``, ``delivery``, ``relations``,
    ``compiler``, and ``outputs``.
    """

    meta: TaskflowMeta
    refs: TaskflowRefs = field(default_factory=TaskflowRefs)
    intent: TaskflowIntent = field(default_factory=TaskflowIntent)
    clarification: TaskflowClarification = field(default_factory=TaskflowClarification)
    domain: TaskflowDomain = field(default_factory=TaskflowDomain)
    design: TaskflowDesign = field(default_factory=TaskflowDesign)
    delivery: TaskflowDelivery = field(default_factory=TaskflowDelivery)
    relations: TaskflowRelations = field(default_factory=TaskflowRelations)
    compiler: TaskflowCompilerState = field(default_factory=TaskflowCompilerState)
    outputs: TaskflowOutputs = field(default_factory=TaskflowOutputs)

    @classmethod
    def new(
        cls,
        *,
        taskflow_id: str,
        project_id: str,
        goal_id: str,
        goal_statement: str,
        sensitivity: Sensitivity | str = Sensitivity.INTERNAL,
    ) -> "TaskflowState":
        """Create a new TaskflowState for ``start_taskflow``."""

        return cls(
            meta=TaskflowMeta(
                taskflow_id=taskflow_id,
                project_id=project_id,
                goal_id=goal_id,
                status=TaskflowStatus.CLARIFYING,
                sensitivity=sensitivity,
            ),
            intent=TaskflowIntent(goal_statement=goal_statement),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowState":
        return cls(
            meta=TaskflowMeta.from_dict(_dict(data.get("meta"))),
            refs=TaskflowRefs.from_dict(_dict(data.get("refs"))),
            intent=TaskflowIntent.from_dict(_dict(data.get("intent"))),
            clarification=TaskflowClarification.from_dict(
                _dict(data.get("clarification"))
            ),
            domain=TaskflowDomain.from_dict(_dict(data.get("domain"))),
            design=TaskflowDesign.from_dict(_dict(data.get("design"))),
            delivery=TaskflowDelivery.from_dict(_dict(data.get("delivery"))),
            relations=TaskflowRelations.from_dict(_dict(data.get("relations"))),
            compiler=TaskflowCompilerState.from_dict(_dict(data.get("compiler"))),
            outputs=TaskflowOutputs.from_dict(_dict(data.get("outputs"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta.to_dict(),
            "refs": self.refs.to_dict(),
            "intent": self.intent.to_dict(),
            "clarification": self.clarification.to_dict(),
            "domain": self.domain.to_dict(),
            "design": self.design.to_dict(),
            "delivery": self.delivery.to_dict(),
            "relations": self.relations.to_dict(),
            "compiler": self.compiler.to_dict(),
            "outputs": self.outputs.to_dict(),
        }

    def touch(self) -> None:
        """Increment state version and refresh update timestamp."""

        self.meta.state_version += 1
        self.meta.updated_at = utc_now()

    def unresolved_high_risk_questions(self) -> list[OpenQuestion]:
        """Return high-risk open questions that still block readiness."""

        return [
            question
            for question in self.clarification.open_questions
            if not question.answered and question.risk_if_unknown == "high"
        ]

    def failed_required_gates(self) -> list[ReadinessGate]:
        """Return required readiness gates that are not passed."""

        return [
            gate
            for gate in self.compiler.readiness_gates
            if gate.required and not gate.passed
        ]


__all__ = [
    "AcceptanceExample",
    "Assumption",
    "ConfirmationState",
    "DecisionRecord",
    "InterfaceSpec",
    "OpenQuestion",
    "ReadinessGate",
    "RiskRecord",
    "ScenarioRecord",
    "Sensitivity",
    "SolutionOption",
    "TaskflowClarification",
    "TaskflowCompilerState",
    "TaskflowDelivery",
    "TaskflowDesign",
    "TaskflowDomain",
    "TaskflowIntent",
    "TaskflowMeta",
    "TaskflowOutputs",
    "TaskflowRefs",
    "TaskflowRelations",
    "TaskflowState",
    "TaskflowStatus",
    "WorkItemCandidate",
]
