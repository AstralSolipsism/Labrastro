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


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [_dict(item) for item in _list(value)]


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


class QuestionState(str, Enum):
    """Lifecycle state for a structured open question."""

    OPEN = "open"
    ANSWERED = "answered"
    SKIPPED = "skipped"
    DEPRECATED = "deprecated"


class RuleStatus(str, Enum):
    """Rule lifecycle for example-mapping discovery."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ExampleKind(str, Enum):
    """BDD example category."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"


class BriefStatus(str, Enum):
    """Versioned brief state."""

    DRAFT = "draft"
    READY = "ready"
    CONFIRMED = "confirmed"
    REOPENED = "reopened"


class DispatchDecisionStatus(str, Enum):
    """Execution confirmation lifecycle for dispatch decisions."""

    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class TaskflowEventType(str, Enum):
    """Event names for the taskflow plan ledger."""

    DISCOVERY_STARTED = "discovery_started"
    DISCOVERY_TURN_RECORDED = "discovery_turn_recorded"
    DECISION_PROPOSED = "decision_proposed"
    DECISION_ANSWERED = "decision_answered"
    QUESTION_ANSWERED = "question_answered"
    RULE_CONFIRMED = "rule_confirmed"
    EXAMPLE_RECORDED = "example_recorded"
    OPEN_QUESTION_RECORDED = "open_question_recorded"
    BRIEF_VERSIONED = "brief_versioned"
    BRIEF_CONFIRM_REQUESTED = "brief_confirm_requested"
    BRIEF_READY = "brief_ready"
    BRIEF_CONFIRMED = "brief_confirmed"
    GHERKIN_COMPILED = "gherkin_compiled"
    ISSUE_MAP_COMPILED = "issue_map_compiled"
    PLAN_COMPILED = "plan_compiled"
    DISPATCH_CONFIRM_REQUESTED = "dispatch_confirm_requested"
    DISPATCH_CONFIRMED = "dispatch_confirmed"
    DISPATCH_REJECTED = "dispatch_rejected"
    DISPATCH_REQUESTED = "dispatch_requested"
    PLAN_REOPENED = "plan_reopened"
    PLAN_REBASED = "plan_rebased"
    REVIEW_CARD_ANSWERED = "review_card_answered"
    COMPLEXITY_EVIDENCE_RECORDED = "complexity_evidence_recorded"
    COMPLEXITY_ASSESSED = "complexity_assessed"
    COMPLEXITY_OVERRIDDEN = "complexity_overridden"


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
    state: QuestionState | str = QuestionState.OPEN
    priority: int = 0
    blocking_scope: str = ""
    field_bindings: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    answered: bool = False
    answer: str | list[str] | None = None
    answered_by: str = ""
    answered_at: str | None = None
    answer_rationale: str = ""
    confidence: float | None = None
    blocks_compile: bool = False
    blocks_dispatch: bool = False
    artifact_targets: list[str] = field(default_factory=list)
    skip_when: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = QuestionState(_enum_value(self.state))
        if self.answered and self.state == QuestionState.OPEN:
            self.state = QuestionState.ANSWERED

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
            state=str(
                data.get("state")
                or (
                    QuestionState.ANSWERED.value
                    if data.get("answered")
                    else QuestionState.OPEN.value
                )
            ),
            priority=int(data.get("priority") or 0),
            blocking_scope=str(data.get("blocking_scope") or ""),
            field_bindings=_string_list(data.get("field_bindings")),
            source_refs=_string_list(data.get("source_refs")),
            answered=bool(data.get("answered", False)),
            answer=parsed_answer,
            answered_by=str(data.get("answered_by") or ""),
            answered_at=(
                str(data["answered_at"]) if data.get("answered_at") is not None else None
            ),
            answer_rationale=str(data.get("answer_rationale") or ""),
            confidence=(
                float(data["confidence"]) if data.get("confidence") is not None else None
            ),
            blocks_compile=bool(data.get("blocks_compile", False)),
            blocks_dispatch=bool(data.get("blocks_dispatch", False)),
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
            "state": self.state.value,
            "priority": self.priority,
            "blocking_scope": self.blocking_scope,
            "field_bindings": list(self.field_bindings),
            "source_refs": list(self.source_refs),
            "answered": self.answered,
            "answer": self.answer,
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
            "answer_rationale": self.answer_rationale,
            "confidence": self.confidence,
            "blocks_compile": self.blocks_compile,
            "blocks_dispatch": self.blocks_dispatch,
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
class RuleRecord:
    """Example-mapping rule that can drive brief, scenarios, and WorkItems."""

    id: str
    statement: str
    status: RuleStatus | str = RuleStatus.PROPOSED
    source_decision_id: str | None = None
    source_question_id: str | None = None
    example_ids: list[str] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)
    blocking: bool = False
    risk_level: str = "medium"
    confirmation_state: ConfirmationState | str = ConfirmationState.SUGGESTED_BY_SYSTEM
    confirmed_by: str = ""
    confirmed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = RuleStatus(_enum_value(self.status))
        self.confirmation_state = ConfirmationState(_enum_value(self.confirmation_state))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleRecord":
        return cls(
            id=str(data.get("id") or ""),
            statement=str(data.get("statement") or ""),
            status=str(data.get("status") or RuleStatus.PROPOSED.value),
            source_decision_id=(
                str(data["source_decision_id"])
                if data.get("source_decision_id") is not None
                else None
            ),
            source_question_id=(
                str(data["source_question_id"])
                if data.get("source_question_id") is not None
                else None
            ),
            example_ids=_string_list(data.get("example_ids")),
            scenario_ids=_string_list(data.get("scenario_ids")),
            blocking=bool(data.get("blocking", False)),
            risk_level=str(data.get("risk_level") or "medium"),
            confirmation_state=str(
                data.get("confirmation_state")
                or data.get("state")
                or ConfirmationState.SUGGESTED_BY_SYSTEM.value
            ),
            confirmed_by=str(data.get("confirmed_by") or ""),
            confirmed_at=(
                str(data["confirmed_at"]) if data.get("confirmed_at") is not None else None
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "status": self.status.value,
            "source_decision_id": self.source_decision_id,
            "source_question_id": self.source_question_id,
            "example_ids": list(self.example_ids),
            "scenario_ids": list(self.scenario_ids),
            "blocking": self.blocking,
            "risk_level": self.risk_level,
            "confirmation_state": self.confirmation_state.value,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ExampleRecord:
    """BDD example mapped to a rule before or alongside Gherkin compilation."""

    id: str
    title: str
    rule_id: str | None = None
    kind: ExampleKind | str = ExampleKind.POSITIVE
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    observable_outputs: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = ExampleKind(_enum_value(self.kind))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExampleRecord":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            rule_id=str(data["rule_id"]) if data.get("rule_id") is not None else None,
            kind=str(data.get("kind") or ExampleKind.POSITIVE.value),
            given=_string_list(data.get("given")),
            when=_string_list(data.get("when")),
            then=_string_list(data.get("then")),
            observable_outputs=_string_list(data.get("observable_outputs")),
            source_refs=_string_list(data.get("source_refs")),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "rule_id": self.rule_id,
            "kind": self.kind.value,
            "given": list(self.given),
            "when": list(self.when),
            "then": list(self.then),
            "observable_outputs": list(self.observable_outputs),
            "source_refs": list(self.source_refs),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class AcceptanceExample:
    """Acceptance example used by compiler traceability."""

    id: str
    title: str
    scenario_ref: str | None = None
    rule_id: str | None = None
    kind: ExampleKind | str = ExampleKind.POSITIVE
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    state: ConfirmationState | str = ConfirmationState.SUGGESTED_BY_SYSTEM
    definition_of_done: list[str] = field(default_factory=list)
    test_suggestions: list[str] = field(default_factory=list)
    observable_outputs: list[str] = field(default_factory=list)
    gherkin_text: str = ""
    source_brief_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = ExampleKind(_enum_value(self.kind))
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
            rule_id=str(data["rule_id"]) if data.get("rule_id") is not None else None,
            kind=str(data.get("kind") or ExampleKind.POSITIVE.value),
            given=_string_list(data.get("given")),
            when=_string_list(data.get("when")),
            then=_string_list(data.get("then")),
            state=str(data.get("state") or ConfirmationState.SUGGESTED_BY_SYSTEM.value),
            definition_of_done=_string_list(data.get("definition_of_done")),
            test_suggestions=_string_list(data.get("test_suggestions")),
            observable_outputs=_string_list(data.get("observable_outputs")),
            gherkin_text=str(data.get("gherkin_text") or ""),
            source_brief_version=(
                int(data["source_brief_version"])
                if data.get("source_brief_version") is not None
                else None
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "scenario_ref": self.scenario_ref,
            "rule_id": self.rule_id,
            "kind": self.kind.value,
            "given": list(self.given),
            "when": list(self.when),
            "then": list(self.then),
            "state": self.state.value,
            "definition_of_done": list(self.definition_of_done),
            "test_suggestions": list(self.test_suggestions),
            "observable_outputs": list(self.observable_outputs),
            "gherkin_text": self.gherkin_text,
            "source_brief_version": self.source_brief_version,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class TaskflowClarification:
    """Clarification ledger from the TaskflowState schema."""

    assumptions: list[Assumption] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    rules: list[RuleRecord] = field(default_factory=list)
    examples: list[ExampleRecord] = field(default_factory=list)
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
            rules=[
                RuleRecord.from_dict(_dict(item))
                for item in _list(data.get("rules"))
            ],
            examples=[
                ExampleRecord.from_dict(_dict(item))
                for item in _list(data.get("examples"))
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
            "rules": [item.to_dict() for item in self.rules],
            "examples": [item.to_dict() for item in self.examples],
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
class DecisionOption:
    """Structured option shown in a DecisionCard."""

    id: str
    label: str
    tradeoff: str = ""
    impact: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> "DecisionOption":
        if isinstance(data, str):
            return cls(id=data, label=data)
        return cls(
            id=str(data.get("id") or data.get("label") or ""),
            label=str(data.get("label") or data.get("id") or ""),
            tradeoff=str(data.get("tradeoff") or ""),
            impact=_dict(data.get("impact")),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "tradeoff": self.tradeoff,
            "impact": dict(self.impact),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class DecisionAnswer:
    """User or system answer to a DecisionRecord."""

    id: str
    decision_id: str
    selected_option_id: str | None = None
    answer: str | list[str] | None = None
    rationale: str = ""
    actor: str = ""
    source: str = "api"
    source_brief_version: int | None = None
    created_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionAnswer":
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
            decision_id=str(data.get("decision_id") or ""),
            selected_option_id=(
                str(data["selected_option_id"])
                if data.get("selected_option_id") is not None
                else None
            ),
            answer=parsed_answer,
            rationale=str(data.get("rationale") or ""),
            actor=str(data.get("actor") or ""),
            source=str(data.get("source") or "api"),
            source_brief_version=(
                int(data["source_brief_version"])
                if data.get("source_brief_version") is not None
                else None
            ),
            created_at=str(data.get("created_at") or utc_now()),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision_id": self.decision_id,
            "selected_option_id": self.selected_option_id,
            "answer": self.answer,
            "rationale": self.rationale,
            "actor": self.actor,
            "source": self.source,
            "source_brief_version": self.source_brief_version,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class DecisionRecord:
    """Local design/product decision with explicit confirmation state."""

    id: str
    topic: str
    decision_type: str = "general"
    question: str = ""
    why_it_matters: str = ""
    options: list[DecisionOption | dict[str, Any] | str] = field(default_factory=list)
    recommended: str | None = None
    recommendation_rationale: str = ""
    chosen: str | None = None
    rationale: str = ""
    state: ConfirmationState | str = ConfirmationState.SUGGESTED_BY_SYSTEM
    impact: dict[str, Any] = field(default_factory=dict)
    answer_refs: list[str] = field(default_factory=list)
    answered_by: str = ""
    answered_at: str | None = None
    answer_source: str = ""
    source_brief_version: int | None = None
    linked_rule_ids: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state = ConfirmationState(_enum_value(self.state))
        self.options = [
            item if isinstance(item, DecisionOption) else DecisionOption.from_dict(item)
            for item in self.options
        ]
        if not self.question:
            self.question = self.topic

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRecord":
        return cls(
            id=str(data.get("id") or ""),
            topic=str(data.get("topic") or data.get("question") or ""),
            decision_type=str(data.get("decision_type") or data.get("type") or "general"),
            question=str(data.get("question") or data.get("topic") or ""),
            why_it_matters=str(data.get("why_it_matters") or ""),
            options=[
                DecisionOption.from_dict(item)
                for item in _list(data.get("options"))
            ],
            recommended=(
                str(data["recommended"]) if data.get("recommended") is not None else None
            ),
            recommendation_rationale=str(data.get("recommendation_rationale") or ""),
            chosen=str(data["chosen"]) if data.get("chosen") is not None else None,
            rationale=str(data.get("rationale") or data.get("reason") or ""),
            state=str(data.get("state") or ConfirmationState.SUGGESTED_BY_SYSTEM.value),
            impact=_dict(data.get("impact")),
            answer_refs=_string_list(data.get("answer_refs")),
            answered_by=str(data.get("answered_by") or ""),
            answered_at=(
                str(data["answered_at"]) if data.get("answered_at") is not None else None
            ),
            answer_source=str(data.get("answer_source") or ""),
            source_brief_version=(
                int(data["source_brief_version"])
                if data.get("source_brief_version") is not None
                else None
            ),
            linked_rule_ids=_string_list(data.get("linked_rule_ids")),
            supersedes=_string_list(data.get("supersedes")),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "decision_type": self.decision_type,
            "question": self.question,
            "why_it_matters": self.why_it_matters,
            "options": [
                item.to_dict() if isinstance(item, DecisionOption) else _serialize(item)
                for item in self.options
            ],
            "recommended": self.recommended,
            "recommendation_rationale": self.recommendation_rationale,
            "chosen": self.chosen,
            "rationale": self.rationale,
            "state": self.state.value,
            "impact": dict(self.impact),
            "answer_refs": list(self.answer_refs),
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
            "answer_source": self.answer_source,
            "source_brief_version": self.source_brief_version,
            "linked_rule_ids": list(self.linked_rule_ids),
            "supersedes": list(self.supersedes),
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
    phase: str = "both"
    source: str = "manual"
    evidence_ids: list[str] = field(default_factory=list)
    artifact_keys: list[str] = field(default_factory=list)
    blocks_compile: bool | None = None
    blocks_dispatch: bool | None = None

    def __post_init__(self) -> None:
        self.phase = self.phase if self.phase in {"compile", "dispatch", "both"} else "both"
        if self.blocks_compile is None:
            self.blocks_compile = self.phase in {"compile", "both"}
        if self.blocks_dispatch is None:
            self.blocks_dispatch = self.phase in {"dispatch", "both"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReadinessGate":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            passed=bool(data.get("passed", False)),
            rationale=str(data.get("rationale") or ""),
            required=bool(data.get("required", True)),
            severity=str(data.get("severity") or "high"),
            phase=str(data.get("phase") or "both"),
            source=str(data.get("source") or "manual"),
            evidence_ids=_string_list(data.get("evidence_ids")),
            artifact_keys=_string_list(data.get("artifact_keys")),
            blocks_compile=(
                bool(data["blocks_compile"])
                if data.get("blocks_compile") is not None
                else None
            ),
            blocks_dispatch=(
                bool(data["blocks_dispatch"])
                if data.get("blocks_dispatch") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "passed": self.passed,
            "rationale": self.rationale,
            "required": self.required,
            "severity": self.severity,
            "phase": self.phase,
            "source": self.source,
            "evidence_ids": list(self.evidence_ids),
            "artifact_keys": list(self.artifact_keys),
            "blocks_compile": bool(self.blocks_compile),
            "blocks_dispatch": bool(self.blocks_dispatch),
        }


@dataclass(slots=True)
class ComplexityEvidenceRecord:
    """Auditable evidence used by the deterministic complexity scorer."""

    id: str
    dimension: str
    source_type: str
    source_id: str = ""
    source_path: str = ""
    score_delta: int = 0
    confidence: float | None = None
    rationale: str = ""
    brief_version: int | None = None
    extracted_by: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComplexityEvidenceRecord":
        return cls(
            id=str(data.get("id") or ""),
            dimension=str(data.get("dimension") or ""),
            source_type=str(data.get("source_type") or ""),
            source_id=str(data.get("source_id") or ""),
            source_path=str(data.get("source_path") or ""),
            score_delta=int(data.get("score_delta") or 0),
            confidence=(
                float(data["confidence"]) if data.get("confidence") is not None else None
            ),
            rationale=str(data.get("rationale") or ""),
            brief_version=(
                int(data["brief_version"])
                if data.get("brief_version") is not None
                else None
            ),
            extracted_by=str(data.get("extracted_by") or "system"),
            metadata=_dict(data.get("metadata")),
            created_at=str(data.get("created_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dimension": self.dimension,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "score_delta": self.score_delta,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "brief_version": self.brief_version,
            "extracted_by": self.extracted_by,
            "metadata": _serialize(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ComplexityDimensionScore:
    """Final capped score for one complexity dimension."""

    dimension: str
    score: int
    evidence_ids: list[str] = field(default_factory=list)
    rationale: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComplexityDimensionScore":
        return cls(
            dimension=str(data.get("dimension") or ""),
            score=int(data.get("score") or 0),
            evidence_ids=_string_list(data.get("evidence_ids")),
            rationale=str(data.get("rationale") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "score": self.score,
            "evidence_ids": list(self.evidence_ids),
            "rationale": self.rationale,
        }


@dataclass(slots=True)
class ComplexityEstimateRecord:
    """Persisted complexity estimate with evidence, not just level routing."""

    level: str
    score: int
    recipe_id: str
    dimension_scores: dict[str, int] = field(default_factory=dict)
    signal_evidence: dict[str, list[str]] = field(default_factory=dict)
    evidence: list[ComplexityEvidenceRecord] = field(default_factory=list)
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
    recipe_policy_id: str = ""
    required_gates: list[str] = field(default_factory=list)
    compile_blockers: list[str] = field(default_factory=list)
    dispatch_blockers: list[str] = field(default_factory=list)
    required_steps: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    rationale: str = ""
    evaluated_at: str = field(default_factory=utc_now)
    overridden_by: str = ""
    override_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComplexityEstimateRecord":
        return cls(
            level=str(data.get("level") or ""),
            score=int(data.get("score") or 0),
            recipe_id=str(data.get("recipe_id") or ""),
            dimension_scores={
                str(key): int(value)
                for key, value in _dict(data.get("dimension_scores")).items()
            },
            signal_evidence={
                str(key): _string_list(value)
                for key, value in _dict(data.get("signal_evidence")).items()
            },
            evidence=[
                ComplexityEvidenceRecord.from_dict(_dict(item))
                for item in _list(data.get("evidence"))
            ],
            dimension_details=[
                ComplexityDimensionScore.from_dict(_dict(item))
                for item in _list(data.get("dimension_details"))
            ],
            confidence=(
                float(data["confidence"]) if data.get("confidence") is not None else None
            ),
            dominant_dimensions=_string_list(data.get("dominant_dimensions")),
            unknown_dimensions=_string_list(data.get("unknown_dimensions")),
            needs_more_evidence=bool(data.get("needs_more_evidence")),
            explanation=str(data.get("explanation") or ""),
            scan_refs=_string_list(data.get("scan_refs")),
            question_packs=_string_list(data.get("question_packs")),
            plan_slicing_policy=str(data.get("plan_slicing_policy") or ""),
            dispatch_safety_policy=str(data.get("dispatch_safety_policy") or ""),
            level_floor=str(data.get("level_floor") or ""),
            hard_escalations=_string_list(data.get("hard_escalations")),
            recipe_policy_id=str(data.get("recipe_policy_id") or ""),
            required_gates=_string_list(data.get("required_gates")),
            compile_blockers=_string_list(data.get("compile_blockers")),
            dispatch_blockers=_string_list(data.get("dispatch_blockers")),
            required_steps=_string_list(data.get("required_steps")),
            completed_steps=_string_list(data.get("completed_steps")),
            skipped_steps=_string_list(data.get("skipped_steps")),
            required_artifacts=_string_list(data.get("required_artifacts")),
            rationale=str(data.get("rationale") or ""),
            evaluated_at=str(data.get("evaluated_at") or utc_now()),
            overridden_by=str(data.get("overridden_by") or ""),
            override_reason=str(data.get("override_reason") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "score": self.score,
            "recipe_id": self.recipe_id,
            "dimension_scores": dict(self.dimension_scores),
            "signal_evidence": {
                key: list(value) for key, value in self.signal_evidence.items()
            },
            "evidence": [item.to_dict() for item in self.evidence],
            "dimension_details": [item.to_dict() for item in self.dimension_details],
            "confidence": self.confidence,
            "dominant_dimensions": list(self.dominant_dimensions),
            "unknown_dimensions": list(self.unknown_dimensions),
            "needs_more_evidence": bool(self.needs_more_evidence),
            "explanation": self.explanation,
            "scan_refs": list(self.scan_refs),
            "question_packs": list(self.question_packs),
            "plan_slicing_policy": self.plan_slicing_policy,
            "dispatch_safety_policy": self.dispatch_safety_policy,
            "level_floor": self.level_floor,
            "hard_escalations": list(self.hard_escalations),
            "recipe_policy_id": self.recipe_policy_id,
            "required_gates": list(self.required_gates),
            "compile_blockers": list(self.compile_blockers),
            "dispatch_blockers": list(self.dispatch_blockers),
            "required_steps": list(self.required_steps),
            "completed_steps": list(self.completed_steps),
            "skipped_steps": list(self.skipped_steps),
            "required_artifacts": list(self.required_artifacts),
            "rationale": self.rationale,
            "evaluated_at": self.evaluated_at,
            "overridden_by": self.overridden_by,
            "override_reason": self.override_reason,
        }


@dataclass(slots=True)
class BriefVersionRecord:
    """Versioned plan brief snapshot used as the confirmation boundary."""

    id: str
    version: int
    status: BriefStatus | str = BriefStatus.DRAFT
    goal: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, list[str]] = field(default_factory=dict)
    glossary: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)
    acceptance_examples: list[dict[str, Any]] = field(default_factory=list)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    gherkin_features: list[dict[str, Any]] = field(default_factory=list)
    issue_map: list[dict[str, Any]] = field(default_factory=list)
    work_item_candidates: list[dict[str, Any]] = field(default_factory=list)
    task_drafts: list[dict[str, Any]] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    complexity_estimate: dict[str, Any] = field(default_factory=dict)
    readiness_gates: list[dict[str, Any]] = field(default_factory=list)
    readiness_score: int = 0
    compile_readiness_score: int = 0
    dispatch_readiness_score: int = 0
    content_hash: str = ""
    previous_version: int | None = None
    causation_event_id: str | None = None
    source_event_ids: list[str] = field(default_factory=list)
    diff_summary: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    created_by: str = ""
    ready_at: str | None = None
    confirmed_by: str = ""
    confirmed_at: str | None = None
    reopened_from_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = BriefStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BriefVersionRecord":
        return cls(
            id=str(data.get("id") or ""),
            version=int(data.get("version") or 0),
            status=str(data.get("status") or BriefStatus.DRAFT.value),
            goal=_dict(data.get("goal")),
            scope={
                str(key): _string_list(value)
                for key, value in _dict(data.get("scope")).items()
            },
            glossary=_dict_list(data.get("glossary")),
            rules=_dict_list(data.get("rules")),
            examples=_dict_list(data.get("examples")),
            acceptance_examples=_dict_list(data.get("acceptance_examples")),
            scenarios=_dict_list(data.get("scenarios")),
            open_questions=_dict_list(data.get("open_questions")),
            decisions=_dict_list(data.get("decisions")),
            risks=_dict_list(data.get("risks")),
            interfaces=_dict_list(data.get("interfaces")),
            gherkin_features=_dict_list(data.get("gherkin_features")),
            issue_map=_dict_list(data.get("issue_map")),
            work_item_candidates=_dict_list(data.get("work_item_candidates")),
            task_drafts=_dict_list(data.get("task_drafts")),
            quality=_dict(data.get("quality")),
            complexity_estimate=_dict(data.get("complexity_estimate")),
            readiness_gates=_dict_list(data.get("readiness_gates")),
            readiness_score=int(data.get("readiness_score") or 0),
            compile_readiness_score=int(data.get("compile_readiness_score") or 0),
            dispatch_readiness_score=int(data.get("dispatch_readiness_score") or 0),
            content_hash=str(data.get("content_hash") or ""),
            previous_version=(
                int(data["previous_version"])
                if data.get("previous_version") is not None
                else None
            ),
            causation_event_id=(
                str(data["causation_event_id"])
                if data.get("causation_event_id") is not None
                else None
            ),
            source_event_ids=_string_list(data.get("source_event_ids")),
            diff_summary=_dict(data.get("diff_summary")),
            created_at=str(data.get("created_at") or utc_now()),
            created_by=str(data.get("created_by") or ""),
            ready_at=str(data["ready_at"]) if data.get("ready_at") is not None else None,
            confirmed_by=str(data.get("confirmed_by") or ""),
            confirmed_at=(
                str(data["confirmed_at"]) if data.get("confirmed_at") is not None else None
            ),
            reopened_from_version=(
                int(data["reopened_from_version"])
                if data.get("reopened_from_version") is not None
                else None
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "status": self.status.value,
            "goal": dict(self.goal),
            "scope": {key: list(value) for key, value in self.scope.items()},
            "glossary": _serialize(self.glossary),
            "rules": _serialize(self.rules),
            "examples": _serialize(self.examples),
            "acceptance_examples": _serialize(self.acceptance_examples),
            "scenarios": _serialize(self.scenarios),
            "open_questions": _serialize(self.open_questions),
            "decisions": _serialize(self.decisions),
            "risks": _serialize(self.risks),
            "interfaces": _serialize(self.interfaces),
            "gherkin_features": _serialize(self.gherkin_features),
            "issue_map": _serialize(self.issue_map),
            "work_item_candidates": _serialize(self.work_item_candidates),
            "task_drafts": _serialize(self.task_drafts),
            "quality": _serialize(self.quality),
            "complexity_estimate": _serialize(self.complexity_estimate),
            "readiness_gates": _serialize(self.readiness_gates),
            "readiness_score": self.readiness_score,
            "compile_readiness_score": self.compile_readiness_score,
            "dispatch_readiness_score": self.dispatch_readiness_score,
            "content_hash": self.content_hash,
            "previous_version": self.previous_version,
            "causation_event_id": self.causation_event_id,
            "source_event_ids": list(self.source_event_ids),
            "diff_summary": _serialize(self.diff_summary),
            "created_at": self.created_at,
            "created_by": self.created_by,
            "ready_at": self.ready_at,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "reopened_from_version": self.reopened_from_version,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ReviewCardAnswer:
    """Answer submitted against a rendered review card."""

    id: str
    card_id: str
    action: str
    value: Any = None
    comment: str = ""
    actor: str = ""
    answered_at: str = field(default_factory=utc_now)
    creates_event_type: TaskflowEventType | str = TaskflowEventType.REVIEW_CARD_ANSWERED
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.creates_event_type = TaskflowEventType(_enum_value(self.creates_event_type))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewCardAnswer":
        return cls(
            id=str(data.get("id") or ""),
            card_id=str(data.get("card_id") or ""),
            action=str(data.get("action") or ""),
            value=data.get("value"),
            comment=str(data.get("comment") or ""),
            actor=str(data.get("actor") or ""),
            answered_at=str(data.get("answered_at") or utc_now()),
            creates_event_type=str(
                data.get("creates_event_type")
                or TaskflowEventType.REVIEW_CARD_ANSWERED.value
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "card_id": self.card_id,
            "action": self.action,
            "value": _serialize(self.value),
            "comment": self.comment,
            "actor": self.actor,
            "answered_at": self.answered_at,
            "creates_event_type": self.creates_event_type.value,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class DispatchDecisionRecord:
    """User confirmation boundary for turning compiled WorkItems into TaskRuns."""

    id: str
    brief_version: int
    work_item_ids: list[str] = field(default_factory=list)
    status: DispatchDecisionStatus | str = DispatchDecisionStatus.REQUESTED
    requested_by: str = ""
    requested_at: str = field(default_factory=utc_now)
    confirmed_by: str = ""
    confirmed_at: str | None = None
    rejected_by: str = ""
    rejected_at: str | None = None
    rationale: str = ""
    readiness_snapshot: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = DispatchDecisionStatus(_enum_value(self.status))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DispatchDecisionRecord":
        return cls(
            id=str(data.get("id") or ""),
            brief_version=int(data.get("brief_version") or 0),
            work_item_ids=_string_list(data.get("work_item_ids")),
            status=str(data.get("status") or DispatchDecisionStatus.REQUESTED.value),
            requested_by=str(data.get("requested_by") or ""),
            requested_at=str(data.get("requested_at") or utc_now()),
            confirmed_by=str(data.get("confirmed_by") or ""),
            confirmed_at=(
                str(data["confirmed_at"]) if data.get("confirmed_at") is not None else None
            ),
            rejected_by=str(data.get("rejected_by") or ""),
            rejected_at=(
                str(data["rejected_at"]) if data.get("rejected_at") is not None else None
            ),
            rationale=str(data.get("rationale") or ""),
            readiness_snapshot=_dict_list(data.get("readiness_snapshot")),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "brief_version": self.brief_version,
            "work_item_ids": list(self.work_item_ids),
            "status": self.status.value,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "rejected_by": self.rejected_by,
            "rejected_at": self.rejected_at,
            "rationale": self.rationale,
            "readiness_snapshot": _serialize(self.readiness_snapshot),
            "metadata": _serialize(self.metadata),
        }


@dataclass(slots=True)
class TaskflowEvent:
    """Append-only event stored inside the TaskflowState ledger."""

    id: str
    type: TaskflowEventType | str
    taskflow_id: str
    project_id: str
    goal_id: str
    actor: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = TaskflowEventType(_enum_value(self.type))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowEvent":
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or TaskflowEventType.DISCOVERY_TURN_RECORDED.value),
            taskflow_id=str(data.get("taskflow_id") or ""),
            project_id=str(data.get("project_id") or ""),
            goal_id=str(data.get("goal_id") or ""),
            actor=str(data.get("actor") or ""),
            payload=_dict(data.get("payload")),
            created_at=str(data.get("created_at") or utc_now()),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "taskflow_id": self.taskflow_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "actor": self.actor,
            "payload": _serialize(self.payload),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class TaskflowCompilerState:
    """Compiler metadata and readiness state."""

    complexity_level: str = "L1"
    recipe_id: str = "small-task"
    complexity_score: int = 0
    complexity_estimate: ComplexityEstimateRecord | None = None
    complexity_evidence: list[ComplexityEvidenceRecord] = field(default_factory=list)
    complexity_override_level: str = ""
    complexity_overridden_by: str = ""
    complexity_override_reason: str = ""
    recipe_version: str = "recipe.v1"
    recipe_steps: list[str] = field(default_factory=list)
    required_steps: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    complexity_rationale: str = ""
    readiness_score: int = 0
    compile_readiness_score: int = 0
    dispatch_readiness_score: int = 0
    readiness_gates: list[ReadinessGate] = field(default_factory=list)
    blocking_items: list[str] = field(default_factory=list)
    traceability_index: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowCompilerState":
        return cls(
            complexity_level=str(data.get("complexity_level") or "L1"),
            recipe_id=str(data.get("recipe_id") or "small-task"),
            complexity_score=int(data.get("complexity_score") or 0),
            complexity_estimate=(
                ComplexityEstimateRecord.from_dict(
                    _dict(data.get("complexity_estimate"))
                )
                if data.get("complexity_estimate") is not None
                else None
            ),
            complexity_evidence=[
                ComplexityEvidenceRecord.from_dict(_dict(item))
                for item in _list(data.get("complexity_evidence"))
            ],
            complexity_override_level=str(data.get("complexity_override_level") or ""),
            complexity_overridden_by=str(data.get("complexity_overridden_by") or ""),
            complexity_override_reason=str(data.get("complexity_override_reason") or ""),
            recipe_version=str(data.get("recipe_version") or "recipe.v1"),
            recipe_steps=_string_list(data.get("recipe_steps")),
            required_steps=_string_list(data.get("required_steps")),
            completed_steps=_string_list(data.get("completed_steps")),
            skipped_steps=_string_list(data.get("skipped_steps")),
            required_artifacts=_string_list(data.get("required_artifacts")),
            complexity_rationale=str(data.get("complexity_rationale") or ""),
            readiness_score=int(data.get("readiness_score") or 0),
            compile_readiness_score=int(data.get("compile_readiness_score") or 0),
            dispatch_readiness_score=int(data.get("dispatch_readiness_score") or 0),
            readiness_gates=[
                ReadinessGate.from_dict(_dict(item))
                for item in _list(data.get("readiness_gates"))
            ],
            blocking_items=_string_list(data.get("blocking_items")),
            traceability_index=_dict(data.get("traceability_index")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "complexity_level": self.complexity_level,
            "recipe_id": self.recipe_id,
            "complexity_score": self.complexity_score,
            "complexity_estimate": (
                self.complexity_estimate.to_dict()
                if self.complexity_estimate is not None
                else None
            ),
            "complexity_evidence": [
                item.to_dict() for item in self.complexity_evidence
            ],
            "complexity_override_level": self.complexity_override_level,
            "complexity_overridden_by": self.complexity_overridden_by,
            "complexity_override_reason": self.complexity_override_reason,
            "recipe_version": self.recipe_version,
            "recipe_steps": list(self.recipe_steps),
            "required_steps": list(self.required_steps),
            "completed_steps": list(self.completed_steps),
            "skipped_steps": list(self.skipped_steps),
            "required_artifacts": list(self.required_artifacts),
            "complexity_rationale": self.complexity_rationale,
            "readiness_score": self.readiness_score,
            "compile_readiness_score": self.compile_readiness_score,
            "dispatch_readiness_score": self.dispatch_readiness_score,
            "readiness_gates": [item.to_dict() for item in self.readiness_gates],
            "blocking_items": list(self.blocking_items),
            "traceability_index": _serialize(self.traceability_index),
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
    brief_versions: list[BriefVersionRecord] = field(default_factory=list)
    current_brief_version: int | None = None
    confirmed_brief_version: int | None = None
    work_item_candidates: list[WorkItemCandidate] = field(default_factory=list)
    task_run_refs: list[str] = field(default_factory=list)
    artifact_projections: list[dict[str, Any]] = field(default_factory=list)
    gherkin_features: list[dict[str, Any]] = field(default_factory=list)
    issue_map: list[dict[str, Any]] = field(default_factory=list)
    review_card_answers: list[ReviewCardAnswer] = field(default_factory=list)
    dispatch_decisions: list[DispatchDecisionRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskflowOutputs":
        return cls(
            plan_drafts=[_dict(item) for item in _list(data.get("plan_drafts"))],
            brief_versions=[
                BriefVersionRecord.from_dict(_dict(item))
                for item in _list(data.get("brief_versions"))
            ],
            current_brief_version=(
                int(data["current_brief_version"])
                if data.get("current_brief_version") is not None
                else None
            ),
            confirmed_brief_version=(
                int(data["confirmed_brief_version"])
                if data.get("confirmed_brief_version") is not None
                else None
            ),
            work_item_candidates=[
                WorkItemCandidate.from_dict(_dict(item))
                for item in _list(data.get("work_item_candidates"))
            ],
            task_run_refs=_string_list(data.get("task_run_refs")),
            artifact_projections=[
                _dict(item) for item in _list(data.get("artifact_projections"))
            ],
            gherkin_features=[
                _dict(item) for item in _list(data.get("gherkin_features"))
            ],
            issue_map=[_dict(item) for item in _list(data.get("issue_map"))],
            review_card_answers=[
                ReviewCardAnswer.from_dict(_dict(item))
                for item in _list(data.get("review_card_answers"))
            ],
            dispatch_decisions=[
                DispatchDecisionRecord.from_dict(_dict(item))
                for item in _list(data.get("dispatch_decisions"))
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_drafts": [dict(item) for item in self.plan_drafts],
            "brief_versions": [item.to_dict() for item in self.brief_versions],
            "current_brief_version": self.current_brief_version,
            "confirmed_brief_version": self.confirmed_brief_version,
            "work_item_candidates": [
                item.to_dict() for item in self.work_item_candidates
            ],
            "task_run_refs": list(self.task_run_refs),
            "artifact_projections": [
                dict(item) for item in self.artifact_projections
            ],
            "gherkin_features": [dict(item) for item in self.gherkin_features],
            "issue_map": [dict(item) for item in self.issue_map],
            "review_card_answers": [
                item.to_dict() for item in self.review_card_answers
            ],
            "dispatch_decisions": [
                item.to_dict() for item in self.dispatch_decisions
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
    events: list[TaskflowEvent] = field(default_factory=list)

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
            events=[
                TaskflowEvent.from_dict(_dict(item))
                for item in _list(data.get("events"))
            ],
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
            "events": [item.to_dict() for item in self.events],
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
            if not question.answered
            and question.state == QuestionState.OPEN
            and (question.risk_if_unknown == "high" or question.blocks_compile)
        ]

    def failed_required_gates(self) -> list[ReadinessGate]:
        """Return required readiness gates that are not passed."""

        return [
            gate
            for gate in self.compiler.readiness_gates
            if gate.required and not gate.passed
        ]

    def failed_compile_gates(self) -> list[ReadinessGate]:
        """Return required compile-phase gates that are not passed."""

        return [
            gate
            for gate in self.compiler.readiness_gates
            if gate.required and not gate.passed and bool(gate.blocks_compile)
        ]

    def failed_dispatch_gates(self) -> list[ReadinessGate]:
        """Return required dispatch-phase gates that are not passed."""

        return [
            gate
            for gate in self.compiler.readiness_gates
            if gate.required and not gate.passed and bool(gate.blocks_dispatch)
        ]


__all__ = [
    "AcceptanceExample",
    "Assumption",
    "BriefStatus",
    "BriefVersionRecord",
    "ComplexityDimensionScore",
    "ComplexityEvidenceRecord",
    "ComplexityEstimateRecord",
    "ConfirmationState",
    "DecisionAnswer",
    "DecisionOption",
    "DecisionRecord",
    "DispatchDecisionRecord",
    "DispatchDecisionStatus",
    "ExampleKind",
    "ExampleRecord",
    "InterfaceSpec",
    "OpenQuestion",
    "QuestionState",
    "ReadinessGate",
    "RiskRecord",
    "RuleRecord",
    "RuleStatus",
    "ScenarioRecord",
    "Sensitivity",
    "SolutionOption",
    "ReviewCardAnswer",
    "TaskflowEvent",
    "TaskflowEventType",
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
