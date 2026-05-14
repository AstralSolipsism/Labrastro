"""Tool argument schema validation, repair, and retry formatting."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import re
from typing import Any


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


@dataclass(slots=True)
class ToolArgumentRepairPolicy:
    """Provider/model-specific repair switches."""

    name: str = "generic"
    wrap_bare_string_arrays: bool = False


@dataclass(slots=True)
class ToolArgumentIssue:
    """A precise schema or semantic problem with a tool argument."""

    path: str
    field: str | None
    code: str
    expected: str
    actual: str
    received_preview: str
    severity: str = "error"
    repairable: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "field": self.field,
            "code": self.code,
            "expected": self.expected,
            "actual": self.actual,
            "receivedPreview": self.received_preview,
            "severity": self.severity,
            "repairable": self.repairable,
            "message": self.message or self.default_message,
        }

    @property
    def default_message(self) -> str:
        return f"{self.path}: expected {self.expected}, got {self.actual}"


@dataclass(slots=True)
class ToolArgumentRepair:
    """A repair action applied to one argument location."""

    path: str
    action: str
    before: str
    after: str
    severity: str = "warning"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "before": self.before,
            "after": self.after,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(slots=True)
class ToolArgumentValidationResult:
    """Validation and repair result for one tool call."""

    tool_name: str
    arguments: dict[str, Any]
    initial_issues: list[ToolArgumentIssue] = field(default_factory=list)
    final_issues: list[ToolArgumentIssue] = field(default_factory=list)
    repairs: list[ToolArgumentRepair] = field(default_factory=list)
    policy_name: str = "generic"

    @property
    def final_valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.final_issues)

    @property
    def has_diagnostics(self) -> bool:
        return bool(self.initial_issues or self.final_issues or self.repairs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "policy": self.policy_name,
            "final_valid": self.final_valid,
            "initial_issues": [issue.to_dict() for issue in self.initial_issues],
            "final_issues": [issue.to_dict() for issue in self.final_issues],
            "repairs": [repair.to_dict() for repair in self.repairs],
        }


def policy_for_provider(*, compat: str | None, model: str | None) -> ToolArgumentRepairPolicy:
    """Choose a conservative repair policy for a provider/model pair."""
    normalized_compat = str(compat or "").strip().lower()
    normalized_model = str(model or "").strip().lower()
    if normalized_compat == "deepseek" or "deepseek" in normalized_model:
        return ToolArgumentRepairPolicy(
            name="deepseek",
            wrap_bare_string_arrays=True,
        )
    return ToolArgumentRepairPolicy()


def validate_and_repair_tool_arguments(
    *,
    tool_name: str,
    arguments: object,
    schema: dict[str, Any] | None,
    policy: ToolArgumentRepairPolicy | None = None,
) -> ToolArgumentValidationResult:
    """Validate, repair, then revalidate a tool-call argument object."""
    active_policy = policy or ToolArgumentRepairPolicy()
    schema_obj = schema if isinstance(schema, dict) else {"type": "object"}
    if not isinstance(arguments, dict):
        issue = _issue(
            path="$",
            field=None,
            code="arguments_not_object",
            expected="object",
            actual=_type_name(arguments),
            value=arguments,
            repairable=False,
            message="Tool arguments must be a JSON object.",
        )
        return ToolArgumentValidationResult(
            tool_name=tool_name,
            arguments={},
            initial_issues=[issue],
            final_issues=[issue],
            policy_name=active_policy.name,
        )

    initial = validate_tool_arguments(arguments, schema_obj)
    repairs: list[ToolArgumentRepair] = []
    repaired = _repair_object(
        deepcopy(arguments),
        schema_obj,
        path="$",
        required=_required_fields(schema_obj),
        policy=active_policy,
        repairs=repairs,
    )
    final = validate_tool_arguments(repaired, schema_obj)
    return ToolArgumentValidationResult(
        tool_name=tool_name,
        arguments=repaired,
        initial_issues=initial,
        final_issues=final,
        repairs=repairs,
        policy_name=active_policy.name,
    )


def validate_tool_arguments(
    arguments: dict[str, Any],
    schema: dict[str, Any] | None,
) -> list[ToolArgumentIssue]:
    schema_obj = schema if isinstance(schema, dict) else {"type": "object"}
    return _validate_value(
        arguments,
        schema_obj,
        path="$",
        field=None,
        required_here=True,
        allow_optional_null=False,
    )


def format_tool_argument_retry_message(
    tool_name: str,
    issues: list[ToolArgumentIssue],
) -> str:
    """Build a compact model-readable retry hint for invalid tool arguments."""
    errors = [issue for issue in issues if issue.severity == "error"]
    if not errors:
        return f"invalid arguments for {tool_name}"
    lines = [
        f"{issue.path}: expected {issue.expected}, got {issue.actual}"
        for issue in errors[:8]
    ]
    if len(errors) > 8:
        lines.append(f"... {len(errors) - 8} more problem(s)")
    joined = "; ".join(lines)
    return (
        f"invalid arguments for {tool_name}. Problems: {joined}. "
        f"Re-call {tool_name} with a JSON object matching the tool schema."
    )


def _validate_value(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
    field: str | None,
    required_here: bool,
    allow_optional_null: bool,
) -> list[ToolArgumentIssue]:
    issues: list[ToolArgumentIssue] = []
    allowed_types = _allowed_types(schema)

    if value is None:
        if "null" in allowed_types:
            return issues
        code = "null_required" if required_here else "optional_null"
        issues.append(
            _issue(
                path=path,
                field=field,
                code=code,
                expected=_expected_label(schema, required_here=required_here),
                actual="null",
                value=value,
                severity="error" if required_here else "warning",
                repairable=not required_here and allow_optional_null,
                message=(
                    "Optional null should be omitted."
                    if not required_here
                    else "Required field cannot be null."
                ),
            )
        )
        return issues

    if allowed_types and not _matches_any_type(value, allowed_types):
        issues.append(
            _issue(
                path=path,
                field=field,
                code="type_mismatch",
                expected=_expected_label(schema, required_here=required_here),
                actual=_type_name(value),
                value=value,
                repairable=_type_mismatch_repairable(value, allowed_types),
            )
        )
        return issues

    if "enum" in schema and isinstance(schema.get("enum"), list):
        enum_values = schema["enum"]
        if value not in enum_values:
            issues.append(
                _issue(
                    path=path,
                    field=field,
                    code="enum_mismatch",
                    expected="one of " + _preview(enum_values),
                    actual=_type_name(value),
                    value=value,
                    repairable=False,
                )
            )

    if "object" in allowed_types and isinstance(value, dict):
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}
        required = _required_fields(schema)
        for name in required:
            if name not in value:
                issues.append(
                    _issue(
                        path=_property_path(path, name),
                        field=name,
                        code="missing_required",
                        expected=_expected_label(
                            props.get(name) if isinstance(props.get(name), dict) else {},
                            required_here=True,
                        ),
                        actual="missing",
                        value=None,
                        repairable=False,
                    )
                )
        for name, child_value in value.items():
            child_schema = props.get(name)
            if not isinstance(child_schema, dict):
                if schema.get("additionalProperties") is False:
                    issues.append(
                        _issue(
                            path=_property_path(path, str(name)),
                            field=str(name),
                            code="additional_property",
                            expected="no additional property",
                            actual=_type_name(child_value),
                            value=child_value,
                            repairable=False,
                        )
                    )
                continue
            issues.extend(
                _validate_value(
                    child_value,
                    child_schema,
                    path=_property_path(path, str(name)),
                    field=str(name),
                    required_here=str(name) in required,
                    allow_optional_null=True,
                )
            )

    if "array" in allowed_types and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                issues.extend(
                    _validate_value(
                        item,
                        item_schema,
                        path=f"{path}[{index}]",
                        field=field,
                        required_here=True,
                        allow_optional_null=False,
                    )
                )

    return issues


def _repair_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    required: set[str],
    policy: ToolArgumentRepairPolicy,
    repairs: list[ToolArgumentRepair],
) -> dict[str, Any]:
    properties = schema.get("properties")
    props = properties if isinstance(properties, dict) else {}
    for name in list(value.keys()):
        child_schema = props.get(name)
        if not isinstance(child_schema, dict):
            continue
        child_path = _property_path(path, str(name))
        child_value = value[name]

        if child_value is None and name not in required and not _allows_null(child_schema):
            repairs.append(
                _repair(
                    path=child_path,
                    action="omit_optional_null",
                    before=child_value,
                    after="<omitted>",
                    message="Optional null field omitted before execution.",
                )
            )
            del value[name]
            continue

        value[name] = _repair_value(
            child_value,
            child_schema,
            path=child_path,
            policy=policy,
            repairs=repairs,
        )
    return value


def _repair_value(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
    policy: ToolArgumentRepairPolicy,
    repairs: list[ToolArgumentRepair],
) -> Any:
    allowed_types = _allowed_types(schema)

    # Order matters: JSON-encoded arrays/objects must be decoded before bare string wrapping.
    if isinstance(value, str) and ("array" in allowed_types or "object" in allowed_types):
        parsed = _try_parse_json_container(value)
        if parsed is not _NO_REPAIR and _matches_any_type(parsed, allowed_types):
            repairs.append(
                _repair(
                    path=path,
                    action="parse_json_string",
                    before=value,
                    after=parsed,
                    message="JSON-encoded argument value parsed before schema validation.",
                )
            )
            value = parsed

    if isinstance(value, str):
        scalar = _repair_scalar_string(value, allowed_types)
        if scalar is not _NO_REPAIR:
            repairs.append(
                _repair(
                    path=path,
                    action="coerce_scalar_string",
                    before=value,
                    after=scalar,
                    message="Lossless scalar string coercion applied.",
                )
            )
            value = scalar

    if (
        policy.wrap_bare_string_arrays
        and "array" in allowed_types
        and isinstance(value, str)
        and value.strip() != "{}"
    ):
        wrapped = [value]
        repairs.append(
            _repair(
                path=path,
                action="wrap_bare_string_array",
                before=value,
                after=wrapped,
                message="DeepSeek policy wrapped a bare string as a single array item.",
            )
        )
        value = wrapped

    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            value = _repair_object(
                value,
                schema,
                path=path,
                required=_required_fields(schema),
                policy=policy,
                repairs=repairs,
            )
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            value = [
                _repair_value(
                    item,
                    item_schema,
                    path=f"{path}[{index}]",
                    policy=policy,
                    repairs=repairs,
                )
                for index, item in enumerate(value)
            ]
    return value


class _NoRepair:
    pass


_NO_REPAIR = _NoRepair()


def _try_parse_json_container(value: str) -> Any:
    stripped = value.strip()
    if not (
        (stripped.startswith("[") and stripped.endswith("]"))
        or (stripped.startswith("{") and stripped.endswith("}"))
    ):
        return _NO_REPAIR
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return _NO_REPAIR
    if isinstance(parsed, (list, dict)):
        return parsed
    return _NO_REPAIR


def _repair_scalar_string(value: str, allowed_types: set[str]) -> Any:
    stripped = value.strip()
    if "integer" in allowed_types and _INT_RE.match(stripped):
        return int(stripped)
    if "number" in allowed_types and _FLOAT_RE.match(stripped):
        number = float(stripped)
        if math.isfinite(number):
            return number
    if "boolean" in allowed_types:
        lowered = stripped.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return _NO_REPAIR


def _type_mismatch_repairable(value: Any, allowed_types: set[str]) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if (
            ("array" in allowed_types and stripped.startswith("["))
            or ("object" in allowed_types and stripped.startswith("{"))
            or "integer" in allowed_types
            or "number" in allowed_types
            or "boolean" in allowed_types
            or "array" in allowed_types
        ):
            return True
    return False


def _allowed_types(schema: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        result.add(raw_type)
    elif isinstance(raw_type, list):
        result.update(str(item) for item in raw_type if isinstance(item, str))
    if schema.get("nullable") is True:
        result.add("null")
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    result.update(_allowed_types(variant))
    if not result and "properties" in schema:
        result.add("object")
    if not result and "items" in schema:
        result.add("array")
    return result


def _allows_null(schema: dict[str, Any]) -> bool:
    return "null" in _allowed_types(schema)


def _matches_any_type(value: Any, allowed_types: set[str]) -> bool:
    if not allowed_types:
        return True
    return any(_matches_type(value, expected) for expected in allowed_types)


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _expected_label(schema: dict[str, Any], *, required_here: bool) -> str:
    allowed = sorted(_allowed_types(schema))
    if not allowed:
        label = "any"
    elif len(allowed) == 1:
        label = allowed[0]
    else:
        label = " or ".join(allowed)
    if not required_here:
        return f"omitted or {label}"
    return label


def _required_fields(schema: dict[str, Any]) -> set[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return set()
    return {str(name) for name in required if isinstance(name, str)}


def _issue(
    *,
    path: str,
    field: str | None,
    code: str,
    expected: str,
    actual: str,
    value: Any,
    severity: str = "error",
    repairable: bool = False,
    message: str = "",
) -> ToolArgumentIssue:
    return ToolArgumentIssue(
        path=path,
        field=field,
        code=code,
        expected=expected,
        actual=actual,
        received_preview=_preview(value),
        severity=severity,
        repairable=repairable,
        message=message,
    )


def _repair(
    *,
    path: str,
    action: str,
    before: Any,
    after: Any,
    message: str,
) -> ToolArgumentRepair:
    return ToolArgumentRepair(
        path=path,
        action=action,
        before=_preview(before),
        after=_preview(after),
        message=message,
    )


def _preview(value: Any, limit: int = 200) -> str:
    if value == "<omitted>":
        return value
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return text[:limit] + ("..." if len(text) > limit else "")


def _property_path(parent: str, name: str) -> str:
    if name.replace("_", "").isalnum():
        return f"{parent}.{name}"
    return f"{parent}[{json.dumps(name)}]"
