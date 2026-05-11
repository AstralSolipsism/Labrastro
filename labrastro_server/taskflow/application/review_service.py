"""Review-card answer application."""

from __future__ import annotations

import uuid
from typing import Any

from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.taskflow_state import (
    ConfirmationState,
    ReviewCardAnswer,
    TaskflowEventType,
    TaskflowState,
)


class ReviewService:
    """Apply user answers from review card projections back to state."""

    def answer_card(
        self,
        state: TaskflowState,
        *,
        card_id: str,
        action: str,
        value: Any = None,
        actor: str = "user",
        comment: str = "",
    ) -> ReviewCardAnswer:
        answer = ReviewCardAnswer(
            id=f"review-answer-{uuid.uuid4().hex}",
            card_id=card_id,
            action=action,
            value=value,
            actor=actor,
            comment=comment,
            metadata={"brief_version": state.outputs.current_brief_version},
        )
        if action == "accept_all" and card_id.endswith(":assumptions"):
            for assumption in state.clarification.assumptions:
                if assumption.state in {
                    ConfirmationState.SUGGESTED_BY_SYSTEM,
                    ConfirmationState.INFERRED_FROM_CONTEXT,
                    ConfirmationState.UNRESOLVED,
                }:
                    assumption.state = ConfirmationState.CONFIRMED_BY_USER
        state.outputs.review_card_answers.append(answer)
        append_taskflow_event(
            state,
            TaskflowEventType.REVIEW_CARD_ANSWERED,
            actor=actor,
            payload=answer.to_dict(),
        )
        return answer


__all__ = ["ReviewService"]
