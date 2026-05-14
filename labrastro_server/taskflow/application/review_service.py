"""Review-card answer application."""

from __future__ import annotations

import uuid
from typing import Any

from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.taskflow.domain.taskflow_state import (
    ConfirmationState,
    QuestionState,
    ReviewCardAnswer,
    TaskflowEventType,
    TaskflowState,
)


class ReviewService:
    """Apply user answers from review card projections back to state."""

    def answer_card_v1(
        self,
        state: TaskflowState,
        *,
        card_id: str,
        action: str,
        value: Any = None,
        actor: str = "user",
        comment: str = "",
    ) -> ReviewCardAnswer:
        """Apply a V1 review-card action to TaskflowState."""

        action = str(action or "").strip()
        if action not in {"accept", "edit", "skip", "reopen", "discuss"}:
            raise ValueError(f"unsupported review card action: {action}")
        answer = ReviewCardAnswer(
            id=f"review-answer-{uuid.uuid4().hex}",
            card_id=card_id,
            action=action,
            value=value,
            actor=actor,
            comment=comment,
            metadata={"brief_version": state.outputs.current_brief_version, "v1": True},
        )
        kind, source_id = self._parse_v1_card_id(card_id)
        if kind == "question":
            self._apply_question_action(state, source_id, action, value, actor, comment)
        elif kind == "assumption":
            self._apply_assumption_action(state, source_id, action, value, comment)
        elif kind == "decision":
            self._apply_decision_action(state, source_id, action, value, actor, comment, answer.id)
        state.outputs.review_card_answers.append(answer)
        event_type = (
            TaskflowEventType.REVIEW_CARD_REOPENED
            if action == "reopen"
            else TaskflowEventType.REVIEW_CARD_ANSWERED
        )
        append_taskflow_event(
            state,
            event_type,
            actor=actor,
            payload=answer.to_dict(),
        )
        return answer

    def _parse_v1_card_id(self, card_id: str) -> tuple[str, str]:
        parts = card_id.rsplit(":", 2)
        if len(parts) != 3:
            raise ValueError(f"invalid review card id: {card_id}")
        return parts[1], parts[2]

    def _apply_question_action(
        self,
        state: TaskflowState,
        question_id: str,
        action: str,
        value: Any,
        actor: str,
        comment: str,
    ) -> None:
        question = next(
            (item for item in state.clarification.open_questions if item.id == question_id),
            None,
        )
        if question is None:
            raise ValueError(f"question not found: {question_id}")
        if action in {"accept", "edit"}:
            answer = value if action == "edit" else question.default_suggestion
            if answer is None:
                answer = value
            if answer is None:
                raise ValueError("question action requires an answer")
            question.answer = str(answer)
            question.answered = True
            question.answered_by = actor
            question.answered_at = utc_now()
            question.answer_rationale = comment
            question.state = QuestionState.ANSWERED
        elif action == "skip":
            question.answered = True
            question.answer = None
            question.answered_by = actor
            question.answered_at = utc_now()
            question.answer_rationale = comment or "Skipped by user."
            question.state = QuestionState.SKIPPED
            question.metadata["skip_consequence"] = (
                f"Skipped with risk {question.risk_if_unknown}."
            )
        elif action == "reopen":
            question.answered = False
            question.answer = None
            question.answered_at = None
            question.answer_rationale = comment
            question.state = QuestionState.OPEN

    def _apply_assumption_action(
        self,
        state: TaskflowState,
        assumption_id: str,
        action: str,
        value: Any,
        comment: str,
    ) -> None:
        assumption = next(
            (item for item in state.clarification.assumptions if item.id == assumption_id),
            None,
        )
        if assumption is None:
            raise ValueError(f"assumption not found: {assumption_id}")
        if action == "accept":
            assumption.state = ConfirmationState.CONFIRMED_BY_USER
        elif action == "edit":
            if value is None:
                raise ValueError("assumption edit requires value")
            assumption.statement = str(value)
            assumption.state = ConfirmationState.CONFIRMED_BY_USER
        elif action == "skip":
            assumption.state = ConfirmationState.DEPRECATED
            assumption.metadata["skip_consequence"] = comment or "Skipped by user."
        elif action == "reopen":
            assumption.state = ConfirmationState.UNRESOLVED

    def _apply_decision_action(
        self,
        state: TaskflowState,
        decision_id: str,
        action: str,
        value: Any,
        actor: str,
        comment: str,
        answer_id: str,
    ) -> None:
        decision = next(
            (item for item in state.design.local_decisions if item.id == decision_id),
            None,
        )
        if decision is None:
            raise ValueError(f"decision not found: {decision_id}")
        if action in {"accept", "edit"}:
            chosen = value if action == "edit" else decision.recommended
            if chosen is None:
                chosen = value
            if chosen is None:
                raise ValueError("decision action requires a chosen value")
            decision.chosen = str(chosen)
            decision.rationale = comment
            decision.state = ConfirmationState.CONFIRMED_BY_USER
            decision.answered_by = actor
            decision.answered_at = utc_now()
            decision.answer_source = "review_card_v1"
            if answer_id not in decision.answer_refs:
                decision.answer_refs.append(answer_id)
        elif action == "skip":
            decision.state = ConfirmationState.UNRESOLVED
            decision.metadata["skip_consequence"] = comment or "Skipped by user."
        elif action == "reopen":
            decision.chosen = None
            decision.state = ConfirmationState.UNRESOLVED


__all__ = ["ReviewService"]
