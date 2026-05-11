"""Review Card rendering for Taskflow confirmation.

Source: ``docs/文档.md`` Section 9 and ``docs/方向建议.md`` Section 9.
Review cards are projections from TaskflowState. They keep user confirmation
focused on assumptions, scope, and decisions without exposing the internal
Project/Goal/WorkItem/TaskRun graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from labrastro_server.taskflow.domain.taskflow_state import (
    ConfirmationState,
    DecisionRecord,
    TaskflowState,
)


@dataclass(frozen=True, slots=True)
class ReviewCard:
    """Structured card payload for UI adapters."""

    card_id: str
    card_type: str
    title: str
    summary: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "card_type": self.card_type,
            "title": self.title,
            "summary": self.summary,
            "items": [dict(item) for item in self.items],
            "actions": list(self.actions),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class AssumptionCard(ReviewCard):
    """Card for system assumptions that require user awareness or confirmation."""


@dataclass(frozen=True, slots=True)
class ScopeCard(ReviewCard):
    """Card for in-scope, out-of-scope, and deferred scope."""


@dataclass(frozen=True, slots=True)
class DecisionCard(ReviewCard):
    """Card for a decision point or confirmed decision."""


class CardRenderer:
    """Render TaskflowState into assumption, scope, and decision cards."""

    def render(self, state: TaskflowState) -> list[ReviewCard]:
        """Return the ordered card set for conversational review."""

        cards: list[ReviewCard] = []
        assumption = self.render_assumption_card(state)
        if assumption.items:
            cards.append(assumption)
        scope = self.render_scope_card(state)
        if scope.items:
            cards.append(scope)
        cards.extend(self.render_decision_cards(state))
        return cards

    def render_assumption_card(self, state: TaskflowState) -> AssumptionCard:
        """Build the AssumptionCard from ``clarification.assumptions``."""

        items = [
            {
                "id": item.id,
                "statement": item.statement,
                "impact": item.impact,
                "state": item.state.value,
                "reason": item.reason,
                "source_refs": list(item.source_refs),
            }
            for item in state.clarification.assumptions
            if item.state
            in {
                ConfirmationState.SUGGESTED_BY_SYSTEM,
                ConfirmationState.INFERRED_FROM_CONTEXT,
                ConfirmationState.UNRESOLVED,
            }
        ]
        return AssumptionCard(
            card_id=f"{state.meta.taskflow_id}:assumptions",
            card_type="assumption",
            title="Assumptions",
            summary="System assumptions that should stay explicit before compile.",
            items=items,
            actions=["accept_all", "modify", "discuss"],
            source_refs=[item["id"] for item in items],
        )

    def render_scope_card(self, state: TaskflowState) -> ScopeCard:
        """Build the ScopeCard from ``intent.scope_*`` fields."""

        has_scope = any(
            [
                state.intent.scope_in,
                state.intent.scope_out,
                state.intent.deferred_scope,
            ]
        )
        items: list[dict[str, Any]] = []
        if has_scope:
            items.append(
                {
                    "goal_id": state.meta.goal_id,
                    "included": list(state.intent.scope_in),
                    "excluded": list(state.intent.scope_out),
                    "deferred": list(state.intent.deferred_scope),
                    "success_criteria": list(state.intent.success_criteria),
                }
            )
        return ScopeCard(
            card_id=f"{state.meta.taskflow_id}:scope",
            card_type="scope",
            title="Scope",
            summary="What this Goal includes, excludes, and defers.",
            items=items,
            actions=["confirm_scope", "edit_scope", "defer_items"],
            source_refs=[state.meta.goal_id],
        )

    def render_decision_cards(self, state: TaskflowState) -> list[DecisionCard]:
        """Build one DecisionCard per decision or unresolved high-value question."""

        decisions = list(state.design.local_decisions)
        for question in state.clarification.open_questions:
            if question.answer_type in {"single_choice", "multi_choice"}:
                decisions.append(
                    DecisionRecord(
                        id=question.id,
                        topic=question.question,
                        options=question.options,
                        recommended=question.default_suggestion,
                        chosen=(
                            str(question.answer)
                            if question.answered and question.answer is not None
                            else None
                        ),
                        rationale=question.why_needed,
                        state=(
                            ConfirmationState.CONFIRMED_BY_USER
                            if question.answered
                            else ConfirmationState.UNRESOLVED
                        ),
                    )
                )

        cards: list[DecisionCard] = []
        for decision in decisions:
            cards.append(
                DecisionCard(
                    card_id=f"{state.meta.taskflow_id}:decision:{decision.id}",
                    card_type="decision",
                    title=decision.topic,
                    summary=decision.rationale,
                    items=[
                        {
                            "id": decision.id,
                            "topic": decision.topic,
                            "options": [
                                option.to_dict()
                                if hasattr(option, "to_dict")
                                else {"id": str(option), "label": str(option)}
                                for option in decision.options
                            ],
                            "recommended": decision.recommended,
                            "chosen": decision.chosen,
                            "rationale": decision.rationale,
                            "state": decision.state.value,
                        }
                    ],
                    actions=["accept_recommendation", "choose_option", "discuss"],
                    source_refs=[decision.id],
                )
            )
        return cards


__all__ = [
    "AssumptionCard",
    "CardRenderer",
    "DecisionCard",
    "ReviewCard",
    "ScopeCard",
]
