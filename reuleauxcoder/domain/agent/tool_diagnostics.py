"""Unified diagnostics for the tool-call lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any


class ToolDiagnosticStage(str, Enum):
    ARGUMENT_VALIDATION = "argument_validation"
    PREFLIGHT = "preflight"
    APPROVAL = "approval"
    PREVIEW = "preview"
    EXECUTION = "execution"
    PROTOCOL = "protocol"
    CHAT = "chat"


class ToolDiagnosticKind(str, Enum):
    SCHEMA_ISSUE = "schema_issue"
    REPAIR_APPLIED = "repair_applied"
    TOOL_RESULT_ERROR = "tool_result_error"
    APPROVAL_DENIED = "approval_denied"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    CHAT_TERMINAL_ERROR = "chat_terminal_error"


@dataclass(slots=True)
class ToolDiagnostic:
    """One normalized diagnostic emitted by tool lifecycle code."""

    stage: str
    kind: str
    severity: str
    code: str
    message: str
    path: str | None = None
    field: str | None = None
    expected: str | None = None
    actual: str | None = None
    repairable: bool = False
    tool_name: str | None = None
    tool_call_id: str | None = None
    action: str | None = None
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self.stage,
            "kind": self.kind,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "repairable": self.repairable,
        }
        optional = {
            "path": self.path,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "action": self.action,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def diagnostic_to_dict(diagnostic: ToolDiagnostic | dict[str, Any]) -> dict[str, Any]:
    if isinstance(diagnostic, ToolDiagnostic):
        return diagnostic.to_dict()
    if isinstance(diagnostic, dict):
        return dict(diagnostic)
    return {
        "stage": ToolDiagnosticStage.EXECUTION.value,
        "kind": ToolDiagnosticKind.TOOL_RESULT_ERROR.value,
        "severity": "error",
        "code": "invalid_diagnostic",
        "message": str(diagnostic),
        "repairable": False,
    }


def tool_diagnostic_from_failure(
    *,
    stage: ToolDiagnosticStage | str,
    kind: ToolDiagnosticKind | str,
    code: str,
    message: str,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    severity: str = "error",
    repairable: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ToolDiagnostic:
    return ToolDiagnostic(
        stage=_enum_value(stage),
        kind=_enum_value(kind),
        severity=severity,
        code=str(code or "tool_error"),
        message=str(message or code or "tool lifecycle diagnostic"),
        repairable=repairable,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        metadata=dict(metadata or {}),
    )


def diagnostics_from_argument_validation(
    validation: Any,
    *,
    tool_call_id: str | None = None,
) -> list[ToolDiagnostic]:
    payload = validation.to_dict() if hasattr(validation, "to_dict") else dict(validation)
    tool_name = str(payload.get("tool_name") or "")
    diagnostics: list[ToolDiagnostic] = []

    initial_issues = _dict_items(payload.get("initial_issues"))
    final_issues = _dict_items(payload.get("final_issues"))
    repairs = _dict_items(payload.get("repairs"))
    final_valid = bool(payload.get("final_valid"))
    issue_items = initial_issues if final_valid or repairs else (final_issues or initial_issues)
    phase = "initial" if issue_items is initial_issues else "final"

    for issue in issue_items:
        path = _string_or_none(issue.get("path")) or "$"
        code = str(issue.get("code") or "schema_issue")
        expected = _string_or_none(issue.get("expected"))
        actual = _string_or_none(issue.get("actual"))
        message = str(
            issue.get("message")
            or f"{path}: expected {expected or 'valid value'}, got {actual or 'invalid'}"
        )
        diagnostics.append(
            ToolDiagnostic(
                stage=ToolDiagnosticStage.ARGUMENT_VALIDATION.value,
                kind=ToolDiagnosticKind.SCHEMA_ISSUE.value,
                severity=str(issue.get("severity") or "error"),
                code=code,
                message=message,
                path=path,
                field=_string_or_none(issue.get("field")),
                expected=expected,
                actual=actual,
                repairable=bool(issue.get("repairable", False)),
                tool_name=tool_name or None,
                tool_call_id=tool_call_id,
                metadata={"phase": phase},
            )
        )

    for repair in repairs:
        action = str(repair.get("action") or "repair_applied")
        diagnostics.append(
            ToolDiagnostic(
                stage=ToolDiagnosticStage.ARGUMENT_VALIDATION.value,
                kind=ToolDiagnosticKind.REPAIR_APPLIED.value,
                severity=str(repair.get("severity") or "warning"),
                code=action,
                message=str(repair.get("message") or action),
                path=_string_or_none(repair.get("path")) or "$",
                repairable=True,
                tool_name=tool_name or None,
                tool_call_id=tool_call_id,
                action=action,
                metadata={
                    key: value
                    for key, value in {
                        "before": repair.get("before"),
                        "after": repair.get("after"),
                    }.items()
                    if value is not None
                },
            )
        )

    provider_diagnostics = payload.get("provider_diagnostics")
    if provider_diagnostics:
        diagnostics.append(
            ToolDiagnostic(
                stage=ToolDiagnosticStage.ARGUMENT_VALIDATION.value,
                kind=ToolDiagnosticKind.SCHEMA_ISSUE.value,
                severity="error",
                code="provider_argument_error",
                message=str(payload.get("provider_argument_error") or "provider returned invalid tool arguments"),
                path="$",
                expected="object",
                actual="invalid",
                repairable=False,
                tool_name=tool_name or None,
                tool_call_id=tool_call_id,
                metadata={"provider_diagnostics": provider_diagnostics},
            )
        )
    return diagnostics


def _enum_value(value: Enum | str) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
