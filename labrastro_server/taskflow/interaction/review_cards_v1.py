"""Taskflow V1 review-card projections.

V1 cards are stable UI DTOs. They normalize questions, assumptions, and
decisions into one action surface while keeping TaskflowState as the source of
truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from labrastro_server.taskflow.domain.taskflow_state import (
    Assumption,
    ConfirmationState,
    DecisionRecord,
    OpenQuestion,
    QuestionState,
    TaskflowState,
)


@dataclass(frozen=True, slots=True)
class ReviewCardActionV1:
    id: str
    label: str
    requires_value: bool = False
    requires_reason: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "requires_value": self.requires_value,
            "requires_reason": self.requires_reason,
        }


@dataclass(frozen=True, slots=True)
class ReviewCardV1:
    id: str
    kind: str
    title: str
    prompt: str
    why_needed: str
    recommended_answer: Any = None
    risk: str = "medium"
    skip_consequence: str = ""
    status: str = "open"
    source_refs: list[str] = field(default_factory=list)
    actions: list[ReviewCardActionV1] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "prompt": self.prompt,
            "why_needed": self.why_needed,
            "recommended_answer": self.recommended_answer,
            "risk": self.risk,
            "skip_consequence": self.skip_consequence,
            "status": self.status,
            "source_refs": list(self.source_refs),
            "actions": [action.to_dict() for action in self.actions],
            "metadata": dict(self.metadata),
        }


class ReviewCardV1Renderer:
    """Render V1 cards from TaskflowState."""

    def render(self, state: TaskflowState) -> list[ReviewCardV1]:
        cards: list[ReviewCardV1] = []
        cards.extend(self._question_cards(state))
        cards.extend(self._assumption_cards(state))
        cards.extend(self._decision_cards(state))
        return cards

    def _question_cards(self, state: TaskflowState) -> list[ReviewCardV1]:
        return [
            self._question_card(state, question)
            for question in state.clarification.open_questions
            if question.state in {QuestionState.OPEN, QuestionState.SKIPPED}
        ]

    def _question_card(
        self, state: TaskflowState, question: OpenQuestion
    ) -> ReviewCardV1:
        blocking = question.blocking_scope or (
            "dispatch" if question.blocks_dispatch else "compile" if question.blocks_compile else "review"
        )
        return ReviewCardV1(
            id=f"{state.meta.taskflow_id}:question:{question.id}",
            kind="question",
            title=question.question or question.id,
            prompt=question.question,
            why_needed=question.why_needed,
            recommended_answer=question.default_suggestion,
            risk=question.risk_if_unknown,
            skip_consequence=(
                f"Skipping leaves the {blocking} boundary less certain."
            ),
            status=question.state.value,
            source_refs=[question.id, *question.source_refs],
            actions=_standard_actions(
                accept_requires_value=question.default_suggestion is None,
                edit_requires_value=True,
            ),
            metadata={
                "answer_type": question.answer_type,
                "options": list(question.options),
                "blocks_compile": question.blocks_compile,
                "blocks_dispatch": question.blocks_dispatch,
            },
        )

    def _assumption_cards(self, state: TaskflowState) -> list[ReviewCardV1]:
        visible_states = {
            ConfirmationState.SUGGESTED_BY_SYSTEM,
            ConfirmationState.INFERRED_FROM_CONTEXT,
            ConfirmationState.UNRESOLVED,
            ConfirmationState.CONFIRMED_BY_USER,
        }
        return [
            self._assumption_card(state, assumption)
            for assumption in state.clarification.assumptions
            if assumption.state in visible_states
        ]

    def _assumption_card(
        self, state: TaskflowState, assumption: Assumption
    ) -> ReviewCardV1:
        return ReviewCardV1(
            id=f"{state.meta.taskflow_id}:assumption:{assumption.id}",
            kind="assumption",
            title=f"Assumption: {assumption.statement}",
            prompt=assumption.statement,
            why_needed=assumption.reason or "Assumptions must stay visible before compile.",
            recommended_answer="accept",
            risk=assumption.impact,
            skip_consequence="Skipping keeps this assumption unconfirmed in the brief.",
            status=assumption.state.value,
            source_refs=[assumption.id, *assumption.source_refs],
            actions=_standard_actions(),
            metadata={"impact": assumption.impact},
        )

    def _decision_cards(self, state: TaskflowState) -> list[ReviewCardV1]:
        return [
            self._decision_card(state, decision)
            for decision in state.design.local_decisions
        ]

    def _decision_card(
        self, state: TaskflowState, decision: DecisionRecord
    ) -> ReviewCardV1:
        options = [
            option.to_dict() if hasattr(option, "to_dict") else {"id": str(option), "label": str(option)}
            for option in decision.options
        ]
        return ReviewCardV1(
            id=f"{state.meta.taskflow_id}:decision:{decision.id}",
            kind="decision",
            title=decision.topic or decision.question or decision.id,
            prompt=decision.question or decision.topic,
            why_needed=decision.why_it_matters or decision.rationale,
            recommended_answer=decision.recommended,
            risk=str(decision.impact.get("risk") or decision.impact.get("level") or "medium"),
            skip_consequence="Skipping leaves this decision unresolved and may stale the plan.",
            status=decision.state.value,
            source_refs=[decision.id],
            actions=_standard_actions(
                accept_requires_value=decision.recommended is None,
                edit_requires_value=True,
            ),
            metadata={
                "decision_type": decision.decision_type,
                "options": options,
                "chosen": decision.chosen,
            },
        )


def _standard_actions(
    *, accept_requires_value: bool = False, edit_requires_value: bool = True
) -> list[ReviewCardActionV1]:
    return [
        ReviewCardActionV1("accept", "Accept", requires_value=accept_requires_value),
        ReviewCardActionV1("edit", "Edit", requires_value=edit_requires_value),
        ReviewCardActionV1("skip", "Skip", requires_reason=True),
        ReviewCardActionV1("reopen", "Reopen", requires_reason=True),
        ReviewCardActionV1("discuss", "Discuss"),
    ]


__all__ = ["ReviewCardActionV1", "ReviewCardV1", "ReviewCardV1Renderer"]
