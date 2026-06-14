"""Debug critical tool model-visible content at the final LLM boundary."""

from __future__ import annotations

import hashlib
from typing import Any

from reuleauxcoder.domain.files import APPLY_PATCH_CONTRACT_TEXT


class CriticalToolVisibilityError(RuntimeError):
    """Raised when a critical tool loses required model-visible guidance."""

    def __init__(self, snapshot: dict[str, Any]):
        self.snapshot = dict(snapshot)
        reasons = snapshot.get("failure_reasons")
        reason_text = "; ".join(str(item) for item in reasons or [])
        super().__init__(
            "critical tool visibility check failed"
            + (f": {reason_text}" if reason_text else "")
        )


_APPLY_PATCH_CONTRACT_MARKERS: tuple[tuple[str, str], ...] = (
    ("json_function_wrapper", "JSON function wrapper"),
    ("begin_patch", "*** Begin Patch"),
    ("end_patch", "*** End Patch"),
    ("add_file", "*** Add File:"),
    ("update_file", "*** Update File:"),
    ("delete_file", "*** Delete File:"),
    ("move_to", "*** Move to:"),
    ("add_file_plus", "Add File content lines must start with +"),
    ("update_hunk", "Update File must contain @@"),
    ("workspace_relative", "workspace-relative"),
    ("forbid_file_header", "Do not use *** File:"),
    ("forbid_action_header", "*** Action:"),
    ("forbid_unified_diff", "unified diff"),
    ("draft_document_begin", "draft_document_begin"),
)


def build_critical_tool_visibility_snapshot(
    provider_type: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Build a redacted visibility snapshot for critical model-visible tools."""

    normalized = _normalize_provider_payload(provider_type, params)
    tools = normalized["tools"]
    prompt_text = normalized["prompt_text"]
    apply_patch_tool = _find_apply_patch_tool(tools)
    apply_patch_exposed = apply_patch_tool is not None
    tool_description = _tool_description(apply_patch_tool)
    patch_parameter_description = _patch_parameter_description(apply_patch_tool)

    prompt_missing = (
        _missing_contract_markers(prompt_text) if apply_patch_exposed else []
    )
    tool_description_missing = (
        _missing_contract_markers(tool_description) if apply_patch_exposed else []
    )
    patch_parameter_missing = (
        _missing_contract_markers(patch_parameter_description)
        if apply_patch_exposed
        else []
    )

    failure_reasons: list[str] = []
    if apply_patch_exposed:
        if prompt_missing:
            failure_reasons.append(
                "prompt missing apply_patch contract markers: "
                + ", ".join(prompt_missing)
            )
        if tool_description_missing:
            failure_reasons.append(
                "apply_patch tool description missing contract markers: "
                + ", ".join(tool_description_missing)
            )
        if patch_parameter_missing:
            failure_reasons.append(
                "apply_patch patch parameter missing contract markers: "
                + ", ".join(patch_parameter_missing)
            )

    return {
        "schema": "llm_critical_tool_visibility.v1",
        "scope": "critical_tools_only",
        "provider_type": str(provider_type or ""),
        "tool_count": len(tools),
        "checked_tools": ["apply_patch"],
        "apply_patch_exposed": apply_patch_exposed,
        "prompt_contract_visible": apply_patch_exposed and not prompt_missing,
        "tool_description_contract_visible": apply_patch_exposed
        and not tool_description_missing,
        "patch_parameter_contract_visible": apply_patch_exposed
        and not patch_parameter_missing,
        "critical_contract_hashes": {"apply_patch": _apply_patch_contract_hash()},
        "missing_contract_markers": sorted(
            set(prompt_missing + tool_description_missing + patch_parameter_missing)
        ),
        "surface_missing_contract_markers": {
            "prompt": prompt_missing,
            "tool_description": tool_description_missing,
            "patch_parameter": patch_parameter_missing,
        },
        "failure_reasons": failure_reasons,
    }


def validate_critical_tool_visibility_snapshot(snapshot: dict[str, Any]) -> None:
    """Fail fast when a critical exposed tool lost required visible guidance."""

    if not snapshot.get("apply_patch_exposed"):
        return
    if snapshot.get("failure_reasons"):
        raise CriticalToolVisibilityError(snapshot)


def _apply_patch_contract_hash() -> str:
    return hashlib.sha256(
        APPLY_PATCH_CONTRACT_TEXT.encode("utf-8")
    ).hexdigest()


def _missing_contract_markers(text: str) -> list[str]:
    return [
        name
        for name, marker in _APPLY_PATCH_CONTRACT_MARKERS
        if marker not in text
    ]


def _normalize_provider_payload(
    provider_type: str, params: dict[str, Any]
) -> dict[str, Any]:
    normalized = str(provider_type or "").strip()
    if normalized == "openai_responses":
        return {
            "prompt_text": _content_list_text(params.get("input")),
            "tools": _list_value(params.get("tools")),
        }
    if normalized == "anthropic_messages":
        return {
            "prompt_text": "\n".join(
                part
                for part in (
                    _content_text(params.get("system")),
                    _content_list_text(params.get("messages")),
                )
                if part
            ),
            "tools": _list_value(params.get("tools")),
        }
    return {
        "prompt_text": _content_list_text(params.get("messages")),
        "tools": _list_value(params.get("tools")),
    }


def _find_apply_patch_tool(tools: list[Any]) -> dict[str, Any] | None:
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = ""
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
        else:
            name = str(tool.get("name") or "")
        if name == "apply_patch":
            return tool
    return None


def _tool_description(tool: dict[str, Any] | None) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("description") or "")
    return str(tool.get("description") or "")


def _patch_parameter_description(tool: dict[str, Any] | None) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    parameters = function.get("parameters") if isinstance(function, dict) else None
    if parameters is None:
        parameters = tool.get("parameters")
    if parameters is None:
        parameters = tool.get("input_schema")
    if not isinstance(parameters, dict):
        return ""
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return ""
    patch_property = properties.get("patch")
    if not isinstance(patch_property, dict):
        return ""
    return str(patch_property.get("description") or "")


def _content_list_text(value: Any) -> str:
    if not isinstance(value, list):
        return _content_text(value)
    return "\n".join(_content_text(item) for item in value if item is not None)


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("content", "text", "input", "output", "thinking"):
            if key in value:
                parts.append(_content_text(value.get(key)))
        return "\n".join(part for part in parts if part)
    return str(value)


def _list_value(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []
