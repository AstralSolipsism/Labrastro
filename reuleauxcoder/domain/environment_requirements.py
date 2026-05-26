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
    if text:
        raise ValueError(f"invalid environment requirement kind: {text}")
    return default if default in ENVIRONMENT_REQUIREMENT_KINDS else "runtime"


def environment_requirement_kind_from_id(requirement_id: Any) -> str:
    text = str(requirement_id or "").strip()
    if text.startswith("envreq:"):
        _, _, rest = text.partition(":")
        kind, _, _ = rest.partition(":")
        if not kind:
            return ""
        if kind in ENVIRONMENT_REQUIREMENT_KINDS:
            return kind
        raise ValueError(f"invalid environment requirement kind in id: {kind}")
    kind, _, _ = text.partition(":")
    if kind in ENVIRONMENT_REQUIREMENT_KINDS:
        return kind
    if ":" in text and kind:
        raise ValueError(f"invalid environment requirement kind in id: {kind}")
    return ""


def resolve_environment_requirement_kind(
    requirement_id: Any = "",
    *,
    candidates: tuple[Any, ...] = (),
    command: Any = "",
    default: str = "runtime",
) -> str:
    id_kind = environment_requirement_kind_from_id(requirement_id)
    for candidate in candidates:
        text = str(candidate or "").strip().lower()
        if text:
            return normalize_environment_requirement_kind(text, default=default)
    if id_kind:
        return id_kind
    if str(command or "").strip():
        return "executable"
    return normalize_environment_requirement_kind("", default=default)


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
    id_kind = environment_requirement_kind_from_id(raw_id)
    resolved_kind = normalize_environment_requirement_kind(
        kind or id_kind,
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
