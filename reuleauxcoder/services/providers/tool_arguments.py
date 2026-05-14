"""Provider-side tool argument parsing diagnostics."""

from __future__ import annotations

from typing import Any
import json

from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.providers.models import ProviderDiagnostic


def parse_provider_tool_arguments(
    *,
    index: int,
    tool_call_id: str,
    tool_name: str,
    raw_arguments: str | None,
) -> tuple[ToolCall, dict[str, Any] | None, ProviderDiagnostic | None]:
    """Parse provider raw function arguments without silently degrading errors."""
    raw_text = raw_arguments or ""
    argument_error: str | None = None
    try:
        if not raw_text:
            raise ValueError("missing tool arguments")
        arguments = json.loads(raw_text)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must decode to an object")
    except json.JSONDecodeError as exc:
        argument_error = f"invalid JSON arguments: {exc.msg}"
        arguments = {}
    except ValueError as exc:
        argument_error = str(exc)
        arguments = {}

    diagnostic: dict[str, Any] | None = None
    provider_diagnostic: ProviderDiagnostic | None = None
    if argument_error:
        diagnostic = tool_arguments_diagnostic(
            index=index,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            raw_arguments=raw_text,
            message=argument_error,
        )
        provider_diagnostic = ProviderDiagnostic(
            code="invalid_tool_arguments",
            message=(
                f"Tool call '{tool_name or tool_call_id}' has invalid arguments: "
                f"{argument_error}"
            ),
            level="error",
        )

    return (
        ToolCall(
            id=tool_call_id,
            name=tool_name,
            arguments=arguments,
            argument_error=argument_error,
            argument_diagnostics=[diagnostic] if diagnostic else [],
        ),
        diagnostic,
        provider_diagnostic,
    )


def tool_arguments_diagnostic(
    *,
    index: int,
    tool_call_id: str,
    tool_name: str,
    raw_arguments: str,
    message: str,
) -> dict[str, Any]:
    return {
        "code": "invalid_tool_arguments",
        "index": index,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "message": message,
        "raw_arguments": raw_arguments[:1000],
    }
