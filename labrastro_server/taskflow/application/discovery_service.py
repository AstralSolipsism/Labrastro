"""Structured discovery writes for Taskflow."""

from __future__ import annotations

import uuid
from typing import Any, Iterable, TypeVar

from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.taskflow.domain.taskflow_state import (
    AcceptanceExample,
    Assumption,
    ConfirmationState,
    DecisionAnswer,
    DecisionRecord,
    ExampleRecord,
    OpenQuestion,
    QuestionState,
    RuleRecord,
    RuleStatus,
    ScenarioRecord,
    TaskflowEventType,
    TaskflowState,
    WorkItemCandidate,
)


T = TypeVar("T")


def _upsert(items: list[T], incoming: Iterable[T], key: str = "id") -> None:
    by_id = {str(getattr(item, key)): index for index, item in enumerate(items)}
    for item in incoming:
        item_id = str(getattr(item, key))
        if item_id in by_id:
            items[by_id[item_id]] = item
        else:
            items.append(item)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class DiscoveryService:
    """Owns writes from conversational discovery into TaskflowState."""

    def record_turn(
        self,
        state: TaskflowState,
        *,
        actor: str = "agent",
        goal_statement: str | None = None,
        background_delta: str | None = None,
        scope_in: list[str] | None = None,
        scope_out: list[str] | None = None,
        deferred_scope: list[str] | None = None,
        success_criteria: list[str] | None = None,
        assumptions: list[Assumption | dict[str, Any]] | None = None,
        questions: list[OpenQuestion | dict[str, Any]] | None = None,
        rules: list[RuleRecord | dict[str, Any]] | None = None,
        examples: list[ExampleRecord | dict[str, Any]] | None = None,
        scenarios: list[ScenarioRecord | dict[str, Any]] | None = None,
        acceptance_examples: list[AcceptanceExample | dict[str, Any]] | None = None,
        decisions: list[DecisionRecord | dict[str, Any]] | None = None,
        work_item_candidates: list[WorkItemCandidate | dict[str, Any]] | None = None,
    ) -> TaskflowState:
        if goal_statement is not None:
            state.intent.goal_statement = goal_statement
        if background_delta is not None:
            state.intent.background_delta = background_delta
        if scope_in is not None:
            state.intent.scope_in = [str(item) for item in scope_in if str(item).strip()]
        if scope_out is not None:
            state.intent.scope_out = [str(item) for item in scope_out if str(item).strip()]
        if deferred_scope is not None:
            state.intent.deferred_scope = [
                str(item) for item in deferred_scope if str(item).strip()
            ]
        if success_criteria is not None:
            state.intent.success_criteria = [
                str(item) for item in success_criteria if str(item).strip()
            ]
        if assumptions is not None:
            _upsert(
                state.clarification.assumptions,
                [
                    item if isinstance(item, Assumption) else Assumption.from_dict(dict(item))
                    for item in assumptions
                ],
            )
        if questions is not None:
            parsed_questions = [
                item if isinstance(item, OpenQuestion) else OpenQuestion.from_dict(dict(item))
                for item in questions
            ]
            _upsert(state.clarification.open_questions, parsed_questions)
            for question in parsed_questions:
                append_taskflow_event(
                    state,
                    TaskflowEventType.OPEN_QUESTION_RECORDED,
                    actor=actor,
                    payload={"question_id": question.id},
                )
        if rules is not None:
            parsed_rules = [
                item if isinstance(item, RuleRecord) else RuleRecord.from_dict(dict(item))
                for item in rules
            ]
            _upsert(state.clarification.rules, parsed_rules)
            for rule in parsed_rules:
                if rule.status == RuleStatus.CONFIRMED:
                    append_taskflow_event(
                        state,
                        TaskflowEventType.RULE_CONFIRMED,
                        actor=actor,
                        payload={"rule_id": rule.id},
                    )
        if examples is not None:
            parsed_examples = [
                item if isinstance(item, ExampleRecord) else ExampleRecord.from_dict(dict(item))
                for item in examples
            ]
            _upsert(state.clarification.examples, parsed_examples)
            for example in parsed_examples:
                append_taskflow_event(
                    state,
                    TaskflowEventType.EXAMPLE_RECORDED,
                    actor=actor,
                    payload={"example_id": example.id, "rule_id": example.rule_id},
                )
        if scenarios is not None:
            _upsert(
                state.clarification.scenarios,
                [
                    item
                    if isinstance(item, ScenarioRecord)
                    else ScenarioRecord.from_dict(dict(item))
                    for item in scenarios
                ],
            )
        if acceptance_examples is not None:
            parsed_acceptance = [
                item
                if isinstance(item, AcceptanceExample)
                else AcceptanceExample.from_dict(dict(item))
                for item in acceptance_examples
            ]
            _upsert(state.clarification.acceptance_examples, parsed_acceptance)
            for example in parsed_acceptance:
                append_taskflow_event(
                    state,
                    TaskflowEventType.EXAMPLE_RECORDED,
                    actor=actor,
                    payload={"example_id": example.id, "rule_id": example.rule_id},
                )
        if decisions is not None:
            parsed_decisions = [
                item if isinstance(item, DecisionRecord) else DecisionRecord.from_dict(dict(item))
                for item in decisions
            ]
            _upsert(state.design.local_decisions, parsed_decisions)
            for decision in parsed_decisions:
                append_taskflow_event(
                    state,
                    TaskflowEventType.DECISION_PROPOSED,
                    actor=actor,
                    payload={"decision_id": decision.id},
                )
        if work_item_candidates is not None:
            _upsert(
                state.outputs.work_item_candidates,
                [
                    item
                    if isinstance(item, WorkItemCandidate)
                    else WorkItemCandidate.from_dict(dict(item))
                    for item in work_item_candidates
                ],
            )

        append_taskflow_event(
            state,
            TaskflowEventType.DISCOVERY_TURN_RECORDED,
            actor=actor,
            payload={
                "rules": len(rules or []),
                "examples": len(examples or []),
                "questions": len(questions or []),
                "decisions": len(decisions or []),
            },
        )
        return state

    def answer_question(
        self,
        state: TaskflowState,
        *,
        question_id: str,
        answer: str | list[str],
        actor: str = "user",
        rationale: str = "",
        confidence: float | None = None,
    ) -> TaskflowState:
        for question in state.clarification.open_questions:
            if question.id == question_id:
                question.answer = answer
                question.answered = True
                question.state = QuestionState.ANSWERED
                question.answered_by = actor
                question.answered_at = question.answered_at or utc_now()
                question.answer_rationale = rationale
                question.confidence = confidence
                append_taskflow_event(
                    state,
                    TaskflowEventType.QUESTION_ANSWERED,
                    actor=actor,
                    payload={"question_id": question_id, "answer": answer},
                )
                return state
        raise ValueError(f"question not found: {question_id}")

    def answer_decision(
        self,
        state: TaskflowState,
        *,
        decision_id: str,
        selected_option_id: str | None = None,
        answer: str | list[str] | None = None,
        rationale: str = "",
        actor: str = "user",
        source: str = "api",
    ) -> DecisionAnswer:
        for decision in state.design.local_decisions:
            if decision.id == decision_id:
                decision.chosen = selected_option_id or (
                    str(answer) if answer is not None and not isinstance(answer, list) else None
                )
                decision.rationale = rationale or decision.rationale
                decision.state = ConfirmationState.CONFIRMED_BY_USER
                decision.answered_by = actor
                decision.answered_at = utc_now()
                decision.answer_source = source
                answer_record = DecisionAnswer(
                    id=_new_id("decision-answer"),
                    decision_id=decision_id,
                    selected_option_id=selected_option_id,
                    answer=answer,
                    rationale=rationale,
                    actor=actor,
                    source=source,
                    source_brief_version=state.outputs.current_brief_version,
                )
                decision.answer_refs.append(answer_record.id)
                decision.metadata.setdefault("answers", []).append(answer_record.to_dict())
                append_taskflow_event(
                    state,
                    TaskflowEventType.DECISION_ANSWERED,
                    actor=actor,
                    payload=answer_record.to_dict(),
                )
                for rule in state.clarification.rules:
                    if rule.id in decision.linked_rule_ids:
                        rule.status = RuleStatus.CONFIRMED
                        rule.confirmation_state = ConfirmationState.CONFIRMED_BY_USER
                        rule.confirmed_by = actor
                        rule.confirmed_at = utc_now()
                        append_taskflow_event(
                            state,
                            TaskflowEventType.RULE_CONFIRMED,
                            actor=actor,
                            payload={"rule_id": rule.id, "decision_id": decision_id},
                        )
                return answer_record
        raise ValueError(f"decision not found: {decision_id}")


__all__ = ["DiscoveryService"]
