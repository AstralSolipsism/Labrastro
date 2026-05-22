"""Shared helpers for declarative command actions."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.params import EnumParam, StrParam
from reuleauxcoder.app.commands.specs import (
    TriggerKind,
    TriggerSelectionBehavior,
    TriggerSpec,
    TriggerVisibility,
)
from reuleauxcoder.interfaces.ui_registry import UICapability

UI_TARGETS = frozenset({"cli", "tui", "vscode"})
TEXT_REQUIRED = frozenset({UICapability.TEXT_INPUT})


@dataclass(frozen=True, slots=True)
class EmptyCommand:
    """Marker command object for actions with no parse payload."""


def slash_trigger(
    value: str,
    *,
    supports_args: bool = False,
    args_hint: str = "",
    selection_behavior: TriggerSelectionBehavior | str = TriggerSelectionBehavior.DISPATCH,
    available_during_run: bool = False,
    visibility: TriggerVisibility | str = TriggerVisibility.VISIBLE,
) -> TriggerSpec:
    """Build a slash trigger declaration with text-input capability requirement."""
    selection = (
        selection_behavior
        if isinstance(selection_behavior, TriggerSelectionBehavior)
        else TriggerSelectionBehavior(str(selection_behavior))
    )
    trigger_visibility = (
        visibility
        if isinstance(visibility, TriggerVisibility)
        else TriggerVisibility(str(visibility))
    )
    return TriggerSpec(
        kind=TriggerKind.SLASH,
        value=value,
        ui_targets=UI_TARGETS,
        required_capabilities=TEXT_REQUIRED,
        supports_args=supports_args,
        args_hint=args_hint,
        selection_behavior=selection,
        available_during_run=available_during_run,
        visibility=trigger_visibility,
    )


def non_empty_text(
    *, lower: bool = False, reject: frozenset[str] = frozenset()
) -> StrParam:
    """Common non-empty text parameter parser."""
    return StrParam(non_empty=True, lower=lower, reject=reject)


def enum_text(
    values: set[str] | frozenset[str], *, case_insensitive: bool = True
) -> EnumParam:
    """Common enum text parameter parser."""
    return EnumParam(values=frozenset(values), case_insensitive=case_insensitive)
