"""Canonical environment requirement identifiers and parsing helpers."""

from __future__ import annotations

from typing import Any, Literal

EnvironmentPlacement = Literal["server", "peer", "both"]

ENVIRONMENT_REQUIREMENT_KINDS = {
    "executable",
    "runtime",
    "sdk",
    "service",
    "env_var",
    "credential",
    "path",
    "project_file",
    "container",
}

ENVIRONMENT_COMMAND_FIELDS = ("command", "check", "install", "configure")
ENVIRONMENT_PLACEMENTS = {"server", "peer", "both"}


def normalize_environment_requirement_kind(value: Any, *, default: str = "runtime") -> str:
    text = str(value or "").strip().lower()
    if text in ENVIRONMENT_REQUIREMENT_KINDS:
        return text
    return default if default in ENVIRONMENT_REQUIREMENT_KINDS else "runtime"


def environment_requirement_kind_from_id(requirement_id: Any) -> str:
    text = str(requirement_id or "").strip()
    if text.startswith("envreq:"):
        _, _, rest = text.partition(":")
        kind, _, _ = rest.partition(":")
        return kind if kind in ENVIRONMENT_REQUIREMENT_KINDS else ""
    kind, _, _ = text.partition(":")
    return kind if kind in ENVIRONMENT_REQUIREMENT_KINDS else ""


def environment_requirement_name_from_id(requirement_id: Any) -> str:
    text = str(requirement_id or "").strip()
    if text.startswith("envreq:"):
        _, _, rest = text.partition(":")
        _, _, name = rest.partition(":")
        return name
    _, sep, name = text.partition(":")
    return name if sep else text


def normalize_environment_requirement_id(
    requirement_id: Any = "",
    *,
    kind: Any = "",
    name: Any = "",
) -> str:
    raw_id = str(requirement_id or "").strip()
    resolved_kind = normalize_environment_requirement_kind(
        kind or environment_requirement_kind_from_id(raw_id),
    )
    resolved_name = str(name or environment_requirement_name_from_id(raw_id)).strip()
    if not resolved_name:
        return ""
    return f"envreq:{resolved_kind}:{resolved_name}"


def normalize_environment_placement(
    value: Any,
    *,
    default: EnvironmentPlacement = "peer",
) -> EnvironmentPlacement:
    text = str(value or "").strip().lower()
    if text in ENVIRONMENT_PLACEMENTS:
        return text  # type: ignore[return-value]
    if default in ENVIRONMENT_PLACEMENTS:
        return default
    return "peer"
