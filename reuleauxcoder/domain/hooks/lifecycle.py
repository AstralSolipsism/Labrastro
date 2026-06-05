"""Declarative lifecycle hook contract.

This module defines the public hook schema. It is intentionally separate from
the in-process Python HookRegistry, which remains an internal runtime adapter.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.runtime_boundary import (
    runtime_boundary_fields,
    runtime_agent_run_id,
    runtime_working_directory,
)
from reuleauxcoder.domain.agent.runtime_budget import runtime_budget_limit_message
from reuleauxcoder.domain.hooks.base import GuardHook, ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    GuardDecision,
    HookContext,
    HookPoint,
)
from reuleauxcoder.domain.hooks.lifecycle_policy import (
    lifecycle_gate_event_is_gate,
    lifecycle_gate_output_is_terminal,
    lifecycle_output_message,
)
from reuleauxcoder.domain.llm.models import ToolCall

LIFECYCLE_HOOK_EVENTS: set[str] = {
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "PreCompact",
    "PostCompact",
    "ConfigChange",
    "CwdChanged",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "Elicitation",
    "ElicitationResult",
    "Notification",
}

LIFECYCLE_HOOK_CONFIG_EVENTS: set[str] = {
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PermissionRequest",
    "PermissionDenied",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "SubagentStart",
    "TaskCreated",
    "PreCompact",
    "PostCompact",
    "ConfigChange",
    "CwdChanged",
    "FileChanged",
    "Notification",
    "Stop",
    "StopFailure",
}

LIFECYCLE_HOOK_CONTROL_PLANE_AUDIT_EVENTS: set[str] = {
    "TaskCompleted",
    "SubagentStop",
    "WorktreeCreate",
    "WorktreeRemove",
}

LIFECYCLE_HOOK_SOURCES: set[str] = {
    "system_builtin",
    "admin_managed",
    "user_config",
    "project_config",
    "local_project_config",
    "capability_package",
    "skill",
    "mcp_server",
    "session",
}

LIFECYCLE_HOOK_PLACEMENTS: set[str] = {"server", "peer", "both"}

LIFECYCLE_HOOK_HANDLER_TYPES: set[str] = {
    "command",
    "http",
    "mcp_tool",
    "prompt",
    "agent",
    "internal",
}

LIFECYCLE_HOOK_TRUST_STATES: set[str] = {
    "pending_review",
    "trusted",
    "disabled",
    "blocked",
}

LIFECYCLE_HOOK_DECISIONS: set[str] = {"allow", "deny", "ask", "defer", "none"}

LIFECYCLE_HOOK_OUTPUT_SUPPORTED_FIELDS: dict[str, set[str]] = {
    "UserPromptSubmit": {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "additional_context",
        "updated_input",
        "diagnostics",
    },
    "UserPromptExpansion": {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "additional_context",
        "updated_input",
        "diagnostics",
    },
    "PermissionRequest": {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "diagnostics",
    },
    "PermissionDenied": {
        "reason",
        "user_message",
        "diagnostics",
    },
    "PreToolUse": {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "updated_input",
        "diagnostics",
    },
    "PostToolUse": {"updated_input", "diagnostics"},
    "PostToolUseFailure": {"diagnostics"},
    "PostToolBatch": {"diagnostics"},
    "SubagentStart": {"reason", "user_message", "diagnostics", "artifacts"},
    "TaskCreated": {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "diagnostics",
    },
    "PreCompact": {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "PostCompact": {
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "ConfigChange": {
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "CwdChanged": {
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "FileChanged": {
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "Notification": {
        "reason",
        "user_message",
        "diagnostics",
        "artifacts",
    },
    "WorktreeCreate": {
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "WorktreeRemove": {
        "reason",
        "user_message",
        "additional_context",
        "diagnostics",
        "artifacts",
    },
    "Stop": {"reason", "user_message", "diagnostics", "artifacts"},
    "StopFailure": {"reason", "user_message", "diagnostics", "artifacts"},
}

LIFECYCLE_HOOK_MATCHER_FIELDS: set[str] = {
    "event_name",
    "placement",
    "tool_names",
    "tool_call_ids",
    "tool_sources",
    "mcp_servers",
    "trigger_source",
    "session_run_id",
    "agent_run_id",
    "turn_id",
}

LIFECYCLE_TOOL_MATCHER_FIELDS: set[str] = {
    "tool_names",
    "tool_call_ids",
    "tool_sources",
    "mcp_servers",
}


def lifecycle_event_catalog_items() -> list[dict[str, Any]]:
    """Return ADR lifecycle events with explicit external wiring status."""

    items: list[dict[str, Any]] = []
    for event_name in sorted(LIFECYCLE_HOOK_EVENTS):
        external_supported = event_name in LIFECYCLE_HOOK_CONFIG_EVENTS
        audit_only = event_name in LIFECYCLE_HOOK_CONTROL_PLANE_AUDIT_EVENTS
        runtime_status = (
            "external_config_supported"
            if external_supported
            else "control_plane_audit_only"
            if audit_only
            else "external_event_unwired"
        )
        item = {
            "event": event_name,
            "in_adr_catalog": True,
            "external_config_supported": external_supported,
            "runtime_status": runtime_status,
        }
        if audit_only:
            item["runtime_reason"] = "emitted_by_agent_run_control_plane"
        items.append({
            **item,
        })
    return items

_AUTHORITATIVE_CONTEXT_FIELDS: set[str] = {
    "event_name",
    "placement",
    "source",
    "trigger_source",
    "session_run_id",
    "agent_run_id",
    "turn_id",
    "origin",
    "locale",
    "metadata",
    "timestamp",
}

LIFECYCLE_HOOK_OUTPUT_STRING_LIMIT = 4096
LIFECYCLE_HOOK_OUTPUT_TOTAL_STRING_LIMIT = 32768
LIFECYCLE_HOOK_OUTPUT_PREVIEW_LIMIT = 256

CONFIG_HOOK_FIELDS: set[str] = {
    "event",
    "placement",
    "handler_type",
    "handler_ref",
    "matcher",
    "display_name",
    "summary",
    "permissions",
    "credentials",
    "trust",
    "risk_level",
    "technical",
}

DERIVED_HOOK_VIEW_FIELDS: set[str] = {
    "id",
    "source",
    "owner_id",
    "owner_enabled",
    "owner_status",
    "enabled",
    "executable",
    "can_manage",
    "unavailable_reason",
    "placement_runtime",
    "hook_views",
    "recent_result",
}

TECHNICAL_FORBIDDEN_FIELDS: set[str] = CONFIG_HOOK_FIELDS.union(
    DERIVED_HOOK_VIEW_FIELDS,
)

_INACTIVE_OWNER_STATUSES: set[str] = {
    "disabled",
    "stopped",
    "missing",
    "removed",
    "deleted",
    "failed",
    "uninstalled",
}

_INTERNAL_HANDLER_ALLOWED_SOURCES: set[str] = {"system_builtin", "admin_managed"}

_LEGACY_HOOK_POINT_EVENT_MAP: dict[str, str] = {
    "before_tool_execute": "PreToolUse",
    "after_tool_execute": "PostToolUse",
    "before_llm_request": "UserPromptSubmit",
    "after_llm_response": "Stop",
    "runner_startup": "SessionStart",
    "runner_shutdown": "SessionEnd",
    "session_start": "SessionStart",
    "session_save": "SessionEnd",
}


@dataclass(slots=True)
class LifecycleHookDeclaration:
    """User-visible lifecycle hook declaration."""

    id: str
    event: str
    source: str
    placement: str
    handler_type: str
    display_name: str
    summary: str
    permissions: list[str]
    credentials: list[str] = field(default_factory=list)
    matcher: Any = "*"
    handler_ref: str = ""
    trust: str = "pending_review"
    risk_level: str = ""
    technical: dict[str, Any] = field(default_factory=dict)
    owner_id: str = ""
    owner_enabled: bool = True
    owner_status: str = "installed"

    @classmethod
    def from_dict(
        cls,
        hook_id: str,
        data: dict[str, Any] | None,
    ) -> "LifecycleHookDeclaration":
        if not isinstance(data, dict):
            raise ValueError("lifecycle hook declaration must be an object")
        resolved_id = _string(hook_id)
        event = _required_choice(data, "event", LIFECYCLE_HOOK_EVENTS)
        source = _required_choice(data, "source", LIFECYCLE_HOOK_SOURCES)
        placement = _required_choice(data, "placement", LIFECYCLE_HOOK_PLACEMENTS)
        handler_type = _required_choice(
            data,
            "handler_type",
            LIFECYCLE_HOOK_HANDLER_TYPES,
        )
        if handler_type == "internal" and source not in {"system_builtin", "admin_managed"}:
            raise ValueError(
                "lifecycle hook internal handlers are limited to system_builtin or admin_managed sources"
            )
        trust = _choice(data, "trust", LIFECYCLE_HOOK_TRUST_STATES, "pending_review")
        display_name = _required_string(data, "display_name")
        summary = _required_string(data, "summary")
        if "permissions" not in data:
            raise ValueError("lifecycle hook declaration permissions is required")
        permissions = _string_list(data.get("permissions"), "permissions")
        technical = data.get("technical")
        if technical is None:
            technical = {}
        if not isinstance(technical, dict):
            raise ValueError("lifecycle hook declaration technical must be an object")
        return cls(
            id=resolved_id,
            event=event,
            source=source,
            placement=placement,
            handler_type=handler_type,
            display_name=display_name,
            summary=summary,
            permissions=permissions,
            credentials=(
                _string_list(data.get("credentials"), "credentials")
                if "credentials" in data
                else []
            ),
            matcher=_validated_matcher(data.get("matcher", "*")),
            handler_ref=_string(data.get("handler_ref")),
            trust=trust,
            risk_level=_string(data.get("risk_level")),
            technical=dict(technical),
            owner_id=_string(data.get("owner_id")),
            owner_enabled=_bool_value(data.get("owner_enabled"), True),
            owner_status=_string(data.get("owner_status"), "installed"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "source": self.source,
            "placement": self.placement,
            "handler_type": self.handler_type,
            "handler_ref": self.handler_ref,
            "matcher": self.matcher,
            "permissions": list(self.permissions),
            "credentials": list(self.credentials),
            "display_name": self.display_name,
            "summary": self.summary,
            "trust": self.trust,
            "risk_level": self.risk_level,
            "technical": dict(self.technical),
            "owner_id": self.owner_id,
            "owner_enabled": self.owner_enabled,
            "owner_status": self.owner_status,
        }


@dataclass(slots=True)
class LifecycleHookOutput:
    """Structured result returned by a lifecycle hook handler."""

    continue_flow: bool = True
    decision: str = "none"
    reason: Any = None
    user_message: str = ""
    additional_context: list[Any] = field(default_factory=list)
    updated_input: dict[str, Any] | None = None
    diagnostics: list[Any] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)
    runtime_artifacts: list[Any] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LifecycleHookOutput":
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("lifecycle hook output must be an object")
        decision = _choice(data, "decision", LIFECYCLE_HOOK_DECISIONS, "none")
        updated_input = data.get("updated_input")
        if updated_input is not None and not isinstance(updated_input, dict):
            raise ValueError("lifecycle hook output updated_input must be a full object")
        continue_flow = data.get("continue_flow", True)
        if not isinstance(continue_flow, bool):
            raise ValueError("lifecycle hook output continue_flow must be a boolean")
        return cls(
            continue_flow=continue_flow,
            decision=decision,
            reason=data.get("reason"),
            user_message=_string(data.get("user_message")),
            additional_context=_list(data.get("additional_context"), "additional_context"),
            updated_input=dict(updated_input) if isinstance(updated_input, dict) else None,
            diagnostics=_list(data.get("diagnostics"), "diagnostics"),
            artifacts=_list(data.get("artifacts"), "artifacts"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "continue_flow": self.continue_flow,
            "decision": self.decision,
            "reason": self.reason,
            "user_message": self.user_message,
            "additional_context": list(self.additional_context),
            "updated_input": dict(self.updated_input)
            if self.updated_input is not None
            else None,
            "diagnostics": list(self.diagnostics),
            "artifacts": list(self.artifacts),
        }


def annotate_lifecycle_output_diagnostics(
    event_name: str,
    output: LifecycleHookOutput,
) -> None:
    """Record diagnostics for output fields the runtime will not consume."""

    supported = LIFECYCLE_HOOK_OUTPUT_SUPPORTED_FIELDS.get(event_name)
    if supported is None:
        return
    output_dict = output.to_dict()
    diagnostics: list[dict[str, str]] = []
    for field_name, value in output_dict.items():
        if field_name == "diagnostics" or field_name in supported:
            continue
        if not _lifecycle_output_field_present(field_name, value):
            continue
        diagnostics.append(
            {
                "code": "lifecycle_output_field_ignored",
                "event_name": event_name,
                "field": field_name,
                "message": (
                    f"Lifecycle event {event_name} does not consume output field "
                    f"{field_name}."
                ),
            }
        )
    diagnostics.extend(_updated_input_shape_diagnostics(event_name, output_dict))
    if not diagnostics:
        return
    existing = [
        item
        for item in output.diagnostics
        if not (
            isinstance(item, dict)
            and item.get("code")
            in {
                "lifecycle_output_field_ignored",
                "lifecycle_updated_input_field_ignored",
            }
            and item.get("event_name") == event_name
        )
    ]
    output.diagnostics = [*existing, *diagnostics]


def _lifecycle_output_field_present(field_name: str, value: Any) -> bool:
    if field_name == "continue_flow":
        return value is False
    if field_name == "decision":
        return str(value or "none") not in {"", "none"}
    if field_name in {"additional_context", "artifacts"}:
        return bool(value)
    if field_name == "updated_input":
        return value is not None
    if field_name in {"reason", "user_message"}:
        return bool(value)
    return value not in (None, "", [], {})


def _updated_input_shape_diagnostics(
    event_name: str,
    output_dict: dict[str, Any],
) -> list[dict[str, str]]:
    updated_input = output_dict.get("updated_input")
    if not isinstance(updated_input, dict):
        return []
    expected_keys = {
        "UserPromptSubmit": {"user_input"},
        "UserPromptExpansion": {"command_text", "user_input"},
        "PreToolUse": {"tool_call"},
        "PostToolUse": {"result"},
    }.get(event_name)
    if expected_keys is None:
        return []
    extra_keys = sorted(str(key) for key in updated_input if key not in expected_keys)
    return [
        {
            "code": "lifecycle_updated_input_field_ignored",
            "event_name": event_name,
            "field": key,
            "message": (
                f"Lifecycle event {event_name} does not consume updated_input field "
                f"{key}."
            ),
        }
        for key in extra_keys
    ]


@dataclass(slots=True)
class LifecycleHookEventContext:
    """Normalized lifecycle event context passed to declarative hooks."""

    event_name: str
    placement: str
    session_run_id: str = ""
    agent_run_id: str = ""
    turn_id: str = ""
    source: str = ""
    origin: str = ""
    locale: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LifecycleHookDispatchResult:
    """One hook dispatch result."""

    declaration: LifecycleHookDeclaration
    output: LifecycleHookOutput


@dataclass(slots=True)
class InternalLifecycleHookPointResult:
    """Result of dispatching an old HookPoint through lifecycle runtime."""

    context: HookContext
    blocked: bool = False
    message: str = ""
    diagnostics: list[object] = field(default_factory=list)
    lifecycle_context: LifecycleHookEventContext | None = None
    dispatch_results: list[LifecycleHookDispatchResult] = field(default_factory=list)


def _apply_lifecycle_updated_input_to_context(
    context: LifecycleHookEventContext,
    output: LifecycleHookOutput,
) -> None:
    updated_input = getattr(output, "updated_input", None)
    if not isinstance(updated_input, dict):
        return

    payload = dict(context.payload or {})
    if context.event_name in {"UserPromptSubmit", "UserPromptExpansion"}:
        candidate = updated_input.get("user_input")
        if context.event_name == "UserPromptExpansion":
            candidate = updated_input.get("command_text", candidate)
        if isinstance(candidate, str):
            if context.event_name == "UserPromptExpansion":
                payload["command_text"] = candidate
            else:
                payload["user_input"] = candidate
            context.payload = payload
        return

    if context.event_name != "PreToolUse":
        return

    tool_call = updated_input.get("tool_call")
    if not isinstance(tool_call, dict):
        return
    technical = payload.get("technical")
    technical_payload = dict(technical) if isinstance(technical, dict) else {}
    previous = technical_payload.get("tool_call")
    merged_tool_call = dict(previous) if isinstance(previous, dict) else {}
    merged_tool_call.update(dict(tool_call))
    technical_payload["tool_call"] = merged_tool_call
    payload["technical"] = technical_payload

    tool_name = _string(merged_tool_call.get("name"))
    tool_call_id = _string(merged_tool_call.get("id"))
    if tool_name:
        payload["tool_names"] = [tool_name]
    if tool_call_id:
        payload["tool_call_ids"] = [tool_call_id]

    context.payload = payload


LifecycleHookHandler = Callable[
    [LifecycleHookDeclaration, LifecycleHookEventContext],
    LifecycleHookOutput,
]


class LifecycleHookRuntimeAdapter(Protocol):
    """Runtime adapter for one public lifecycle hook handler type."""

    handler_type: str
    supported_placements: set[str]
    supported_events: set[str] | None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        """Return why this adapter cannot execute the declaration."""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        """Execute one lifecycle declaration."""


@dataclass(slots=True)
class FunctionLifecycleHookRuntimeAdapter:
    """Small adapter wrapper used by tests and narrow in-process runtimes."""

    handler_type: str
    handler: LifecycleHookHandler
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        return _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        return _coerce_hook_output(self.handler(declaration, context))


@dataclass(slots=True)
class DeclarativeLifecycleHookRuntimeAdapter:
    """Adapter for declarations that carry an explicit technical.output payload."""

    handler_type: str
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None
    require_output: bool = True

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if self.require_output and not isinstance(
            declaration.technical.get("output"),
            dict,
        ):
            return f"handler_ref_unavailable:{self.handler_type}"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        del context
        raw_output = declaration.technical.get("output")
        if isinstance(raw_output, dict):
            return LifecycleHookOutput.from_dict(raw_output)
        return LifecycleHookOutput.from_dict({
            "diagnostics": [
                {
                    "code": "handler_ref_unavailable",
                    "handler_type": declaration.handler_type,
                    "handler_ref": declaration.handler_ref,
                }
            ]
        })


@dataclass(slots=True)
class InternalHookRegistryLifecycleHookRuntimeAdapter:
    """Adapter that runs legacy in-process HookRegistry hooks behind lifecycle."""

    hook_registry: HookRegistry | None = None
    agent: Any | None = None
    handler_type: str = "internal"
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if declaration.permissions and self.agent is None:
            return "runtime_unavailable:agent_context"
        if declaration.source not in _INTERNAL_HANDLER_ALLOWED_SOURCES:
            return "handler_source_unavailable:internal"
        if not _string(declaration.technical.get("old_hook_point")):
            return ""
        if self.hook_registry is None:
            return "handler_ref_unavailable:internal"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        if declaration.permissions:
            if self.agent is None:
                return _adapter_failure_output(
                    declaration,
                    "internal adapter requires Agent context to enforce declared permissions",
                    code="agent_context_unavailable",
                    handler_type="internal",
                )
            permission_output = _lifecycle_permission_failure_output(
                self.agent,
                declaration,
                handler_type="internal",
                tool_name="lifecycle_internal",
                arguments=_lifecycle_adapter_permission_arguments(
                    declaration,
                    context,
                    agent=self.agent,
                    handler_type="internal",
                    handler_ref=declaration.handler_ref,
                    old_hook_point=_string(
                        declaration.technical.get("old_hook_point")
                    ),
                ),
                tool_source="lifecycle_hook",
            )
            if permission_output is not None:
                return permission_output
        old_hook_point = _string(declaration.technical.get("old_hook_point"))
        if not old_hook_point:
            return _internal_declarative_output(declaration)
        if self.hook_registry is None:
            return _internal_hook_unavailable_output(declaration)
        legacy_context = _legacy_context_from_lifecycle_context(context)
        if legacy_context is None:
            return LifecycleHookOutput.from_dict({
                "diagnostics": [{
                    "code": "legacy_context_unavailable",
                    "hook_id": declaration.id,
                    "old_hook_point": old_hook_point,
                }]
            })
        try:
            hook_point = HookPoint(old_hook_point)
        except ValueError:
            return LifecycleHookOutput.from_dict({
                "diagnostics": [{
                    "code": "legacy_hook_point_unavailable",
                    "hook_id": declaration.id,
                    "old_hook_point": old_hook_point,
                }]
            })
        hook = _find_legacy_hook(
            self.hook_registry,
            hook_point,
            declaration.handler_ref,
        )
        if hook is None:
            return _internal_hook_unavailable_output(declaration)
        if isinstance(hook, GuardHook):
            return _run_legacy_guard_hook(hook, hook_point, legacy_context)
        if isinstance(hook, TransformHook):
            updated_context = hook.run(legacy_context)
            if updated_context is None:
                raise TypeError(
                    f"transform hook '{hook.name}' returned None for {hook_point.value}"
                )
            if not isinstance(updated_context, legacy_context.__class__):
                raise TypeError(
                    f"transform hook '{hook.name}' returned "
                    f"{type(updated_context).__name__}, expected "
                    f"{legacy_context.__class__.__name__}"
                )
            _set_legacy_context_on_lifecycle_context(context, updated_context)
            return LifecycleHookOutput()
        if isinstance(hook, ObserverHook):
            try:
                hook.run(legacy_context)
            except Exception as exc:
                return LifecycleHookOutput.from_dict({
                    "diagnostics": [{
                        "code": "legacy_observer_failed",
                        "hook_name": hook.name,
                        "old_hook_point": hook_point.value,
                        "message": str(exc),
                    }]
                })
            return LifecycleHookOutput()
        return LifecycleHookOutput.from_dict({
            "diagnostics": [{
                "code": "legacy_hook_kind_unavailable",
                "hook_name": getattr(hook, "name", declaration.handler_ref),
                "old_hook_point": hook_point.value,
            }]
        })


@dataclass(slots=True)
class PromptLifecycleHookRuntimeAdapter:
    """Adapter that asks a controlled model call for LifecycleHookOutput JSON."""

    llm: Any | None = None
    agent: Any | None = None
    handler_type: str = "prompt"
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if not _prompt_adapter_instruction(declaration):
            return "handler_ref_unavailable:prompt"
        if self.llm is None or not callable(getattr(self.llm, "chat", None)):
            return "runtime_unavailable:prompt_model"
        if declaration.permissions and self.agent is None:
            return "runtime_unavailable:agent_context"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        instruction = _prompt_adapter_instruction(declaration)
        if not instruction:
            return _prompt_adapter_failure_output(
                declaration,
                context,
                "prompt adapter declaration has no instruction",
                code="handler_ref_unavailable",
            )
        if self.llm is None or not callable(getattr(self.llm, "chat", None)):
            return _prompt_adapter_failure_output(
                declaration,
                context,
                "prompt adapter has no model runtime",
                code="prompt_model_unavailable",
            )
        budget_output = _lifecycle_runtime_budget_failure_output(
            self.agent,
            declaration,
            handler_type="prompt",
        )
        if budget_output is not None:
            return budget_output
        if declaration.permissions:
            if self.agent is None:
                return _adapter_failure_output(
                    declaration,
                    "prompt adapter requires Agent context to enforce declared permissions",
                    code="agent_context_unavailable",
                    handler_type="prompt",
                )
            permission_output = _lifecycle_permission_failure_output(
                self.agent,
                declaration,
                handler_type="prompt",
                tool_name="lifecycle_prompt",
                arguments=_lifecycle_adapter_permission_arguments(
                    declaration,
                    context,
                    agent=self.agent,
                    handler_type="prompt",
                ),
                tool_source="lifecycle_hook",
            )
            if permission_output is not None:
                return permission_output
        messages = _prompt_adapter_messages(declaration, context, instruction)
        try:
            response = self.llm.chat(
                messages,
                tools=None,
                lifecycle_dispatcher=None,
                metadata={
                    "lifecycle_hook_id": declaration.id,
                    "lifecycle_event": context.event_name,
                    "lifecycle_handler_type": "prompt",
                },
            )
        except Exception as exc:
            return _prompt_adapter_failure_output(
                declaration,
                context,
                f"prompt adapter model request failed: {exc}",
                code="prompt_model_failed",
            )
        _record_lifecycle_prompt_usage(self.agent, response)
        budget_output = _lifecycle_runtime_budget_failure_output(
            self.agent,
            declaration,
            handler_type="prompt",
        )
        if budget_output is not None:
            return budget_output
        raw_content = str(getattr(response, "content", "") or "").strip()
        try:
            output_data = _parse_lifecycle_output_json(raw_content)
            return LifecycleHookOutput.from_dict(output_data)
        except Exception as exc:
            return _prompt_adapter_failure_output(
                declaration,
                context,
                f"prompt adapter returned invalid LifecycleHookOutput JSON: {exc}",
                code="prompt_output_invalid",
                raw_content=raw_content,
            )


@dataclass(slots=True)
class CommandLifecycleHookRuntimeAdapter:
    """Adapter that runs a permission-gated command returning LifecycleHookOutput JSON."""

    agent: Any | None = None
    handler_type: str = "command"
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if not _command_adapter_command(declaration):
            return "handler_ref_unavailable:command"
        if self.agent is None:
            return "runtime_unavailable:agent_context"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        command = _command_adapter_command(declaration)
        if not command:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                "command adapter declaration has no command",
                code="handler_ref_unavailable",
                handler_type="command",
            )
        budget_output = _lifecycle_runtime_budget_failure_output(
            self.agent,
            declaration,
            handler_type="command",
        )
        if budget_output is not None:
            return budget_output
        permission_output = _lifecycle_permission_failure_output(
            self.agent,
            declaration,
            handler_type="command",
            tool_name="shell",
            arguments=_lifecycle_adapter_permission_arguments(
                declaration,
                context,
                agent=self.agent,
                handler_type="command",
                command=command,
                intent=_command_adapter_intent(declaration, context),
            ),
            tool_source="builtin",
        )
        if permission_output is not None:
            return permission_output
        timeout = _int_value(declaration.technical.get("timeout_sec"), default=30)
        cwd = runtime_working_directory(self.agent) or None
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=max(1, timeout),
            )
        except Exception as exc:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"command adapter failed: {exc}",
                code="command_failed",
                handler_type="command",
            )
        if completed.returncode != 0:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"command adapter exited with code {completed.returncode}",
                code="command_nonzero_exit",
                handler_type="command",
                diagnostics_extra={
                    "stdout": completed.stdout[:1000],
                    "stderr": completed.stderr[:1000],
                },
            )
        return _lifecycle_output_from_external_json(
            declaration,
            context,
            completed.stdout,
            handler_type="command",
        )


@dataclass(slots=True)
class HttpLifecycleHookRuntimeAdapter:
    """Adapter that POSTs lifecycle context to an HTTP endpoint."""

    agent: Any | None = None
    handler_type: str = "http"
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if not _http_adapter_url(declaration):
            return "handler_ref_unavailable:http"
        if self.agent is None:
            return "runtime_unavailable:agent_context"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        url = _http_adapter_url(declaration)
        if not url:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                "http adapter declaration has no URL",
                code="handler_ref_unavailable",
                handler_type="http",
            )
        budget_output = _lifecycle_runtime_budget_failure_output(
            self.agent,
            declaration,
            handler_type="http",
        )
        if budget_output is not None:
            return budget_output
        permission_output = _lifecycle_permission_failure_output(
            self.agent,
            declaration,
            handler_type="http",
            tool_name="http_request",
            arguments=_lifecycle_adapter_permission_arguments(
                declaration,
                context,
                agent=self.agent,
                handler_type="http",
                url=url,
            ),
            tool_source="builtin",
        )
        if permission_output is not None:
            return permission_output
        body = json.dumps(
            _external_adapter_request(declaration, context),
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        for key, value in dict(declaration.technical.get("headers") or {}).items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        timeout = max(1, _int_value(declaration.technical.get("timeout_sec"), default=30))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_text = response.read(256_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"http adapter returned {exc.code}",
                code="http_status_error",
                handler_type="http",
            )
        except Exception as exc:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"http adapter failed: {exc}",
                code="http_failed",
                handler_type="http",
            )
        return _lifecycle_output_from_external_json(
            declaration,
            context,
            response_text,
            handler_type="http",
        )


@dataclass(slots=True)
class MCPToolLifecycleHookRuntimeAdapter:
    """Adapter that invokes an Agent-visible MCP tool after permission checks."""

    agent: Any | None = None
    handler_type: str = "mcp_tool"
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if not declaration.handler_ref.strip():
            return "handler_ref_unavailable:mcp_tool"
        if self.agent is None:
            return "runtime_unavailable:agent_context"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        if self.agent is None:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                "mcp_tool adapter has no Agent context",
                code="agent_context_unavailable",
                handler_type="mcp_tool",
            )
        tool_name = declaration.handler_ref.strip()
        tool = getattr(self.agent, "get_tool", lambda _name: None)(tool_name)
        if tool is None:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"MCP tool not found: {tool_name}",
                code="mcp_tool_unavailable",
                handler_type="mcp_tool",
            )
        arguments = _adapter_arguments(declaration, context)
        budget_output = _lifecycle_runtime_budget_failure_output(
            self.agent,
            declaration,
            handler_type="mcp_tool",
        )
        if budget_output is not None:
            return budget_output
        permission_output = _lifecycle_permission_failure_output(
            self.agent,
            declaration,
            handler_type="mcp_tool",
            tool_name=tool_name,
            arguments=_lifecycle_adapter_permission_arguments(
                declaration,
                context,
                agent=self.agent,
                handler_type="mcp_tool",
                **arguments,
            ),
            tool_source=str(getattr(tool, "tool_source", "") or "mcp"),
            tool=tool,
        )
        if permission_output is not None:
            return permission_output
        try:
            result = _execute_lifecycle_tool_call(
                self.agent,
                ToolCall(
                    id=f"lifecycle:{declaration.id}",
                    name=tool_name,
                    arguments=arguments,
                ),
            )
        except Exception as exc:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"MCP tool adapter failed: {exc}",
                code="mcp_tool_failed",
                handler_type="mcp_tool",
            )
        return _lifecycle_output_from_external_json(
            declaration,
            context,
            str(result or ""),
            handler_type="mcp_tool",
        )


@dataclass(slots=True)
class AgentLifecycleHookRuntimeAdapter:
    """Adapter that submits a controlled AgentRun and returns its run reference."""

    agent: Any | None = None
    handler_type: str = "agent"
    supported_placements: set[str] = field(default_factory=lambda: {"server"})
    supported_events: set[str] | None = None

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        reason = _adapter_runtime_unavailable_reason(
            self,
            declaration,
            placement=placement,
        )
        if reason:
            return reason
        if not _agent_adapter_agent_id(declaration):
            return "handler_ref_unavailable:agent"
        if self.agent is None:
            return "runtime_unavailable:agent_context"
        if getattr(self.agent, "agent_run_control_plane", None) is None:
            return "runtime_unavailable:agent_run_control_plane"
        return ""

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        target_agent_id = _agent_adapter_agent_id(declaration)
        prompt = _agent_adapter_prompt(declaration, context)
        if not target_agent_id or not prompt:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                "agent adapter requires target agent id and prompt",
                code="handler_ref_unavailable",
                handler_type="agent",
            )
        control = getattr(self.agent, "agent_run_control_plane", None)
        if control is None:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                "agent adapter has no AgentRun control plane",
                code="agent_run_control_plane_unavailable",
                handler_type="agent",
            )
        config = getattr(self.agent, "runtime_config", None)
        target_config = (
            getattr(getattr(config, "agent_registry", None), "agents", {}) or {}
        ).get(target_agent_id)
        if target_config is None:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"agent adapter target agent not found: {target_agent_id}",
                code="agent_not_found",
                handler_type="agent",
                diagnostics_extra={"agent_id": target_agent_id},
            )
        budget_output = _lifecycle_runtime_budget_failure_output(
            self.agent,
            declaration,
            handler_type="agent",
        )
        if budget_output is not None:
            return budget_output
        permission_output = _lifecycle_permission_failure_output(
            self.agent,
            declaration,
            handler_type="agent",
            tool_name="lifecycle_agent",
            arguments=_lifecycle_adapter_permission_arguments(
                declaration,
                context,
                agent=self.agent,
                handler_type="agent",
                target_agent_id=target_agent_id,
            ),
            tool_source="lifecycle_hook",
        )
        if permission_output is not None:
            return permission_output
        from reuleauxcoder.domain.permission_gateway import PermissionGateway

        decision = PermissionGateway().evaluate_agent_invocation(
            target_config,
            source=context.source or "lifecycle_hook",
            interactive=False,
        )
        if not decision.allowed:
            return _permission_denied_lifecycle_output(
                declaration,
                decision,
                handler_type="agent",
            )
        parent_run_id = context.agent_run_id or runtime_agent_run_id(self.agent) or None
        task_payload = _agent_adapter_child_lifecycle_payload(
            declaration,
            context,
            target_agent_id=target_agent_id,
            prompt=prompt,
            parent_run_id=parent_run_id,
        )
        task_lifecycle_output = _dispatch_agent_adapter_child_lifecycle(
            self.agent,
            "TaskCreated",
            task_payload,
            gate=True,
        )
        if task_lifecycle_output is not None:
            return task_lifecycle_output
        try:
            from labrastro_server.services.agent_runtime.control_plane import (
                AgentRunRequest,
            )

            run = control.submit_agent_run(
                AgentRunRequest(
                    issue_id=f"lifecycle:{declaration.id}",
                    agent_id=target_agent_id,
                    prompt=prompt,
                    source="delegation",
                    parent_run_id=parent_run_id,
                    delegated_by_run_id=parent_run_id,
                    runtime_profile_id=_string(declaration.technical.get("runtime_profile_id")) or None,
                    budget=_dict_or_empty(declaration.technical.get("budget")),
                    metadata={
                        "lifecycle_hook_id": declaration.id,
                        "lifecycle_event": context.event_name,
                        "trigger_source": context.source,
                        "parent_session_id": context.session_run_id,
                        "parent_turn_id": context.turn_id,
                        "lifecycle_hook_source": declaration.source,
                        "lifecycle_handler_type": declaration.handler_type,
                    },
                )
            )
            subagent_payload = dict(task_payload)
            subagent_payload.update({
                "child_agent_run_id": str(getattr(run, "id", "") or ""),
                "status": str(getattr(getattr(run, "status", None), "value", "") or ""),
            })
            _dispatch_agent_adapter_child_lifecycle(
                self.agent,
                "SubagentStart",
                subagent_payload,
                gate=False,
            )
        except Exception as exc:
            return _adapter_dispatch_failure_output(
                declaration,
                context,
                f"agent adapter failed: {exc}",
                code="agent_run_submit_failed",
                handler_type="agent",
            )
        return LifecycleHookOutput.from_dict({
            "diagnostics": [{
                "code": "agent_run_submitted",
                "agent_run_id": str(getattr(run, "id", "") or ""),
                "agent_id": str(getattr(run, "agent_id", "") or target_agent_id),
            }],
            "artifacts": [{
                "kind": "agent_run",
                "id": str(getattr(run, "id", "") or ""),
            }],
        })


def _agent_adapter_child_lifecycle_payload(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    *,
    target_agent_id: str,
    prompt: str,
    parent_run_id: str | None,
) -> dict[str, Any]:
    return {
        "agent_id": target_agent_id,
        "task": prompt,
        "parent_run_id": str(parent_run_id or ""),
        "source": "delegation",
        "trigger_source": context.source,
        "parent_session_id": context.session_run_id,
        "parent_turn_id": context.turn_id,
        "lifecycle_hook_id": declaration.id,
        "lifecycle_event": context.event_name,
        "lifecycle_hook_source": declaration.source,
        "lifecycle_handler_type": declaration.handler_type,
    }


def _dispatch_agent_adapter_child_lifecycle(
    agent: Any,
    event_name: str,
    payload: dict[str, Any],
    *,
    gate: bool,
) -> LifecycleHookOutput | None:
    dispatcher = getattr(agent, "lifecycle_dispatcher", None)
    dispatch = getattr(dispatcher, "dispatch", None)
    if not callable(dispatch):
        return None
    context = build_lifecycle_event_context(
        event_name,
        placement="server",
        trigger_source=str(payload.get("trigger_source") or "lifecycle_hook"),
        session_run_id=str(payload.get("parent_session_id") or ""),
        agent_run_id=str(payload.get("parent_run_id") or runtime_agent_run_id(agent)),
        turn_id=str(payload.get("parent_turn_id") or ""),
        origin="agent",
        locale=str(getattr(agent, "locale", "") or ""),
        metadata={
            "handler_type": "agent",
            "agent_id": str(payload.get("agent_id") or ""),
            "lifecycle_hook_id": str(payload.get("lifecycle_hook_id") or ""),
        },
        payload=payload,
    )
    try:
        results = list(dispatch(context))
    except Exception as exc:
        message = f"{event_name} lifecycle dispatch failed."
        _emit_agent_adapter_child_lifecycle_observation(
            agent,
            "dispatch_failed",
            context,
            error=f"{type(exc).__name__}: {exc}",
            message=message,
        )
        if not gate:
            return None
        return LifecycleHookOutput(
            continue_flow=False,
            decision="deny",
            user_message=message,
            diagnostics=[
                {
                    "code": "child_agent_lifecycle_dispatch_failed",
                    "event_name": event_name,
                    "message": message,
                }
            ],
        )
    for result in results:
        _emit_agent_adapter_child_lifecycle_observation(
            agent,
            "result",
            context,
            result=result,
        )
        output = getattr(result, "output", None)
        if gate and lifecycle_gate_output_is_terminal(output):
            message = lifecycle_output_message(
                output,
                fallback=f"{event_name} lifecycle blocked child AgentRun.",
            )
            return LifecycleHookOutput(
                continue_flow=False,
                decision="deny",
                user_message=message,
                diagnostics=[
                    {
                        "code": "child_agent_lifecycle_denied",
                        "event_name": event_name,
                        "message": message,
                    }
                ],
            )
    return None


def _emit_agent_adapter_child_lifecycle_observation(
    agent: Any,
    phase: str,
    context: LifecycleHookEventContext,
    *,
    result: object | None = None,
    error: str = "",
    message: str = "",
) -> None:
    emit = getattr(agent, "_emit_event", None)
    if not callable(emit):
        return
    try:
        declaration = getattr(result, "declaration", None)
        output = getattr(result, "output", None)
        output_dict = output.to_dict() if hasattr(output, "to_dict") else {}
        diagnostics = (
            list(output_dict.get("diagnostics") or [])
            if isinstance(output_dict, dict)
            else []
        )
        decision = (
            str(output_dict.get("decision") or "none")
            if isinstance(output_dict, dict)
            else "none"
        )
        continue_flow = (
            bool(output_dict.get("continue_flow", True))
            if isinstance(output_dict, dict)
            else True
        )
        level = (
            "error"
            if error or decision == "deny" or continue_flow is False
            else "warning"
            if diagnostics
            else "info"
        )
        event_payload = {
            "phase": phase,
            "event_name": context.event_name,
            "placement": context.placement,
            "session_run_id": context.session_run_id,
            "agent_run_id": context.agent_run_id,
            "turn_id": context.turn_id,
            "trigger_source": context.source,
            "hook_id": str(getattr(declaration, "id", "") or ""),
            "display_name": str(getattr(declaration, "display_name", "") or ""),
            "source": str(getattr(declaration, "source", "") or ""),
            "decision": decision,
            "continue_flow": continue_flow,
            "diagnostics": diagnostics,
            "error": error,
            "level": level,
            "title": str(getattr(declaration, "display_name", "") or context.event_name),
            "payload": dict(context.payload or {}),
        }
        if isinstance(output_dict, dict):
            event_payload.update(lifecycle_output_audit_fields(output))
            event_payload["output"] = output_dict
            output_message = lifecycle_output_message(output, fallback="")
            if output_message:
                event_payload["message"] = output_message
        if message:
            event_payload["message"] = message
        emit(
            AgentEvent.lifecycle_hook(
                event_payload,
                runtime_artifacts=lifecycle_runtime_artifacts_for_event(
                    output,
                    event_name=context.event_name,
                    context=context,
                ),
            )
        )
    except Exception:
        return


class LifecycleHookRuntimeAdapterRegistry:
    """Single source of truth for lifecycle handler runtime availability."""

    def __init__(
        self,
        adapters: list[LifecycleHookRuntimeAdapter] | None = None,
    ) -> None:
        self._adapters: dict[str, LifecycleHookRuntimeAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: LifecycleHookRuntimeAdapter) -> None:
        handler_type = str(getattr(adapter, "handler_type", "") or "").strip()
        if handler_type not in LIFECYCLE_HOOK_HANDLER_TYPES:
            raise ValueError(f"unsupported lifecycle hook runtime adapter: {handler_type}")
        if handler_type in self._adapters:
            raise ValueError(f"duplicate lifecycle hook runtime adapter: {handler_type}")
        self._adapters[handler_type] = adapter

    def get(self, handler_type: str) -> LifecycleHookRuntimeAdapter | None:
        return self._adapters.get(handler_type)

    def handler_types(self) -> set[str]:
        return set(self._adapters)

    def unavailable_reason(
        self,
        declaration: LifecycleHookDeclaration,
        *,
        placement: str | None = None,
    ) -> str:
        adapter = self.get(declaration.handler_type)
        if adapter is None:
            return f"handler_unavailable:{declaration.handler_type}"
        return adapter.unavailable_reason(declaration, placement=placement)

    def dispatch(
        self,
        declaration: LifecycleHookDeclaration,
        context: LifecycleHookEventContext,
    ) -> LifecycleHookOutput:
        adapter = self.get(declaration.handler_type)
        if adapter is None:
            raise RuntimeError(
                f"lifecycle hook runtime adapter unavailable: {declaration.handler_type}"
            )
        return _coerce_hook_output(
            adapter.dispatch(declaration, context),
            declaration=declaration,
        )


class LifecycleHookRegistry:
    """Registry for declarative lifecycle hook declarations."""

    def __init__(
        self,
        declarations: list[LifecycleHookDeclaration] | None = None,
    ) -> None:
        self._declarations: dict[str, LifecycleHookDeclaration] = {}
        for declaration in declarations or []:
            self.register(declaration)

    def register(self, declaration: LifecycleHookDeclaration) -> None:
        if declaration.id in self._declarations:
            raise ValueError(f"duplicate lifecycle hook id: {declaration.id}")
        self._declarations[declaration.id] = declaration

    def register_dict(self, hook_id: str, data: dict[str, Any]) -> LifecycleHookDeclaration:
        declaration = LifecycleHookDeclaration.from_dict(hook_id, data)
        self.register(declaration)
        return declaration

    def query(
        self,
        *,
        event: str | None = None,
        source: str | None = None,
        placement: str | None = None,
        trust: str | None = None,
    ) -> list[LifecycleHookDeclaration]:
        return [
            declaration
            for declaration in self._declarations.values()
            if _matches_optional(declaration.event, event)
            and _matches_optional(declaration.source, source)
            and _placement_matches(declaration.placement, placement)
            and _matches_optional(declaration.trust, trust)
        ]

    def executable(
        self,
        *,
        event: str,
        placement: str | None = None,
        runtime_adapters: LifecycleHookRuntimeAdapterRegistry | None = None,
    ) -> list[LifecycleHookDeclaration]:
        adapters = runtime_adapters or default_lifecycle_hook_runtime_adapters()
        return [
            declaration
            for declaration in self.query(event=event, trust="trusted")
            if _runtime_unavailable_reason(
                declaration,
                placement=placement,
                runtime_adapters=adapters,
            )
            == ""
        ]

    def dashboard_items(
        self,
        *,
        runtime_adapters: LifecycleHookRuntimeAdapterRegistry | None = None,
        recent_results: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        adapters = runtime_adapters or default_lifecycle_hook_runtime_adapters()
        return [
            _dashboard_item(
                declaration,
                runtime_adapters=adapters,
                recent_results=recent_results,
            )
            for declaration in self._declarations.values()
        ]


class LifecycleHookDispatcher:
    """Dispatch trusted lifecycle hook declarations to registered handlers."""

    def __init__(
        self,
        registry: LifecycleHookRegistry,
        *,
        runtime_adapters: LifecycleHookRuntimeAdapterRegistry,
    ) -> None:
        self.registry = registry
        self.runtime_adapters = runtime_adapters

    def dispatch(
        self,
        context: LifecycleHookEventContext,
        *,
        source: str | None = None,
    ) -> list[LifecycleHookDispatchResult]:
        results: list[LifecycleHookDispatchResult] = []
        declarations = [
            declaration
            for declaration in self.registry.query(
                event=context.event_name,
                source=source,
                trust="trusted",
            )
            if _runtime_unavailable_reason(
                declaration,
                placement=context.placement,
                runtime_adapters=self.runtime_adapters,
            )
            == ""
            and _legacy_context_matches_declaration(declaration, context)
        ]
        for declaration in declarations:
            if not _matcher_matches(declaration.matcher, context):
                continue
            try:
                output = self.runtime_adapters.dispatch(declaration, context)
            except Exception as exc:
                output = _lifecycle_dispatch_failure_output(
                    declaration,
                    context,
                    exc,
                )
            results.append(LifecycleHookDispatchResult(declaration, output))
            if lifecycle_gate_event_is_gate(context.event_name):
                if lifecycle_gate_output_is_terminal(output):
                    break
                _apply_lifecycle_updated_input_to_context(context, output)
        return results


def default_lifecycle_hook_runtime_adapters(
    *,
    hook_registry: HookRegistry | None = None,
    prompt_llm: Any | None = None,
) -> LifecycleHookRuntimeAdapterRegistry:
    """Return built-in lifecycle runtime adapters for production dispatch."""

    return LifecycleHookRuntimeAdapterRegistry([
        InternalHookRegistryLifecycleHookRuntimeAdapter(hook_registry=hook_registry),
        PromptLifecycleHookRuntimeAdapter(llm=prompt_llm),
        CommandLifecycleHookRuntimeAdapter(),
        HttpLifecycleHookRuntimeAdapter(),
        MCPToolLifecycleHookRuntimeAdapter(),
        AgentLifecycleHookRuntimeAdapter(),
    ])


def default_lifecycle_hook_catalog_runtime_adapters() -> LifecycleHookRuntimeAdapterRegistry:
    """Return adapter availability for Settings/dashboard catalog views.

    Catalog views are not inside an AgentRun, so they should answer whether the
    handler type is structurally backed by a runtime adapter. Actual execution
    still uses an AgentRun-bound registry from default_lifecycle_hook_runtime_adapters().
    """

    catalog_agent = _LifecycleCatalogAgent()
    return LifecycleHookRuntimeAdapterRegistry([
        InternalHookRegistryLifecycleHookRuntimeAdapter(),
        PromptLifecycleHookRuntimeAdapter(
            llm=_LifecycleCatalogPromptLLM(),
            agent=catalog_agent,
        ),
        CommandLifecycleHookRuntimeAdapter(agent=catalog_agent),
        HttpLifecycleHookRuntimeAdapter(agent=catalog_agent),
        MCPToolLifecycleHookRuntimeAdapter(agent=catalog_agent),
        AgentLifecycleHookRuntimeAdapter(agent=catalog_agent),
    ])


def _coerce_hook_output(
    value: Any,
    *,
    declaration: LifecycleHookDeclaration | None = None,
) -> LifecycleHookOutput:
    if isinstance(value, LifecycleHookOutput):
        output = value
    elif isinstance(value, dict):
        output = LifecycleHookOutput.from_dict(value)
    else:
        raise ValueError("lifecycle hook handler must return LifecycleHookOutput or dict")
    if declaration is None:
        return output
    return _bounded_lifecycle_hook_output(output, declaration)


def _bounded_lifecycle_hook_output(
    output: LifecycleHookOutput,
    declaration: LifecycleHookDeclaration,
) -> LifecycleHookOutput:
    overflow_artifacts: list[dict[str, Any]] = []
    runtime_artifacts: list[dict[str, Any]] = [
        dict(item)
        for item in getattr(output, "runtime_artifacts", []) or []
        if isinstance(item, dict)
    ]
    total_string_chars = 0

    def overflow_ref(field_path: str, value: str) -> str:
        safe_hook_id = "".join(
            char if char.isalnum() else "-"
            for char in (declaration.id or "hook")
        ).strip("-") or "hook"
        artifact_id = (
            f"lifecycle-output-overflow:{safe_hook_id}:{len(overflow_artifacts) + 1}"
        )
        overflow_artifacts.append({
            "kind": "lifecycle_output_overflow",
            "id": artifact_id,
            "hook_id": declaration.id,
            "field": field_path,
            "original_chars": len(value),
            "original_bytes": len(value.encode("utf-8", errors="replace")),
            "preview": value[:LIFECYCLE_HOOK_OUTPUT_PREVIEW_LIMIT],
        })
        runtime_artifacts.append({
            "artifact_id": artifact_id,
            "type": "log",
            "status": "generated",
            "content": value,
            "metadata": {
                "kind": "lifecycle_output_overflow",
                "hook_id": declaration.id,
                "source": declaration.source,
                "handler_type": declaration.handler_type,
                "field": field_path,
                "original_chars": len(value),
                "original_bytes": len(value.encode("utf-8", errors="replace")),
                "preview": value[:LIFECYCLE_HOOK_OUTPUT_PREVIEW_LIMIT],
            },
        })
        return artifact_id

    def sanitize(value: Any, field_path: str) -> Any:
        nonlocal total_string_chars
        if isinstance(value, str):
            value_chars = len(value)
            if (
                value_chars <= LIFECYCLE_HOOK_OUTPUT_STRING_LIMIT
                and total_string_chars + value_chars <= LIFECYCLE_HOOK_OUTPUT_TOTAL_STRING_LIMIT
            ):
                total_string_chars += value_chars
                return value
            artifact_id = overflow_ref(field_path, value)
            preview = value[:LIFECYCLE_HOOK_OUTPUT_PREVIEW_LIMIT]
            total_string_chars += len(preview)
            return f"{preview}...[truncated; artifact_ref={artifact_id}]"
        if isinstance(value, dict):
            return {
                str(key): sanitize(
                    item,
                    f"{field_path}.{key}" if field_path else str(key),
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                sanitize(item, f"{field_path}[{index}]")
                for index, item in enumerate(value)
            ]
        return value

    data = output.to_dict()
    sanitized = {key: sanitize(value, str(key)) for key, value in data.items()}
    if not overflow_artifacts:
        bounded = LifecycleHookOutput.from_dict(sanitized)
        bounded.runtime_artifacts = runtime_artifacts
        return bounded

    diagnostics = list(sanitized.get("diagnostics") or [])
    diagnostics.append({
        "code": "lifecycle_output_overflow",
        "hook_id": declaration.id,
        "message": "Lifecycle hook output exceeded size limits; full values are referenced as artifacts.",
        "fields": [artifact["field"] for artifact in overflow_artifacts],
        "artifact_refs": [artifact["id"] for artifact in overflow_artifacts],
    })
    artifacts = list(sanitized.get("artifacts") or [])
    artifacts.extend(overflow_artifacts)
    sanitized["diagnostics"] = diagnostics
    sanitized["artifacts"] = artifacts
    bounded = LifecycleHookOutput.from_dict(sanitized)
    bounded.runtime_artifacts = runtime_artifacts
    return bounded


def lifecycle_runtime_artifacts_for_event(
    output: Any,
    *,
    event_name: str,
    context: Any,
) -> list[dict[str, Any]]:
    artifacts = [
        dict(item)
        for item in getattr(output, "runtime_artifacts", []) or []
        if isinstance(item, dict)
    ]
    if not artifacts:
        return []
    enriched: list[dict[str, Any]] = []
    for artifact in artifacts:
        metadata = dict(artifact.get("metadata") or {})
        artifact["metadata"] = {
            **metadata,
            "event_name": str(event_name or getattr(context, "event_name", "") or ""),
            "session_run_id": str(getattr(context, "session_run_id", "") or ""),
            "agent_run_id": str(getattr(context, "agent_run_id", "") or ""),
            "turn_id": str(getattr(context, "turn_id", "") or ""),
            "placement": str(getattr(context, "placement", "") or ""),
            "trigger_source": str(getattr(context, "source", "") or ""),
        }
        enriched.append(artifact)
    return enriched


def lifecycle_output_audit_fields(output: Any) -> dict[str, Any]:
    output_dict = output.to_dict() if hasattr(output, "to_dict") else {}
    if not isinstance(output_dict, dict):
        return {}
    audit: dict[str, Any] = {}
    if output_dict.get("reason") is not None:
        audit["reason"] = output_dict.get("reason")
    user_message = str(output_dict.get("user_message") or "").strip()
    if user_message:
        audit["user_message"] = user_message
    artifacts = output_dict.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        audit["artifacts"] = list(artifacts)
    return audit


def dispatch_internal_lifecycle_hook_point(
    dispatcher: LifecycleHookDispatcher | None,
    hook_point: HookPoint,
    legacy_context: HookContext,
    *,
    trigger_source: str = "internal",
    origin: str = "agent",
) -> InternalLifecycleHookPointResult:
    """Run legacy HookRegistry hooks through the lifecycle dispatcher."""

    if dispatcher is None:
        return InternalLifecycleHookPointResult(context=legacy_context)
    event_name = _LEGACY_HOOK_POINT_EVENT_MAP.get(hook_point.value)
    if not event_name:
        return InternalLifecycleHookPointResult(context=legacy_context)
    lifecycle_context = build_lifecycle_event_context(
        event_name,
        placement="server",
        trigger_source=trigger_source,
        origin=origin,
        session_run_id=str(getattr(legacy_context, "session_id", "") or ""),
        metadata=dict(getattr(legacy_context, "metadata", {}) or {}),
        payload={
            "technical": {
                "legacy_context": legacy_context,
                "old_hook_point": hook_point.value,
            }
        },
    )
    try:
        results = dispatcher.dispatch(lifecycle_context, source="system_builtin")
    except TypeError as exc:
        if "source" not in str(exc):
            raise
        return InternalLifecycleHookPointResult(context=legacy_context)
    blocked_message = ""
    diagnostics: list[object] = []
    for result in results:
        output = result.output
        diagnostics.extend(list(output.diagnostics or []))
        if output.decision == "deny" or output.continue_flow is False:
            blocked_message = _lifecycle_output_message(output) or (
                f"Lifecycle hook blocked {hook_point.value}"
            )
            break
    updated_context = _legacy_context_from_lifecycle_context(lifecycle_context)
    return InternalLifecycleHookPointResult(
        context=updated_context or legacy_context,
        blocked=bool(blocked_message),
        message=blocked_message,
        diagnostics=diagnostics,
        lifecycle_context=lifecycle_context,
        dispatch_results=list(results),
    )


def _internal_declarative_output(
    declaration: LifecycleHookDeclaration,
) -> LifecycleHookOutput:
    raw_output = declaration.technical.get("output")
    if isinstance(raw_output, dict):
        return LifecycleHookOutput.from_dict(raw_output)
    return LifecycleHookOutput()


def _internal_hook_unavailable_output(
    declaration: LifecycleHookDeclaration,
) -> LifecycleHookOutput:
    return LifecycleHookOutput.from_dict({
        "diagnostics": [
            {
                "code": "handler_ref_unavailable",
                "handler_type": declaration.handler_type,
                "handler_ref": declaration.handler_ref,
            }
        ]
    })


def _legacy_context_matches_declaration(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
) -> bool:
    old_hook_point = _string(declaration.technical.get("old_hook_point"))
    if not old_hook_point:
        return True
    technical = context.payload.get("technical")
    if not isinstance(technical, dict):
        return False
    return _string(technical.get("old_hook_point")) == old_hook_point


def _legacy_context_from_lifecycle_context(
    context: LifecycleHookEventContext,
) -> HookContext | None:
    technical = context.payload.get("technical")
    if not isinstance(technical, dict):
        return None
    legacy_context = technical.get("legacy_context")
    return legacy_context if isinstance(legacy_context, HookContext) else None


def _set_legacy_context_on_lifecycle_context(
    context: LifecycleHookEventContext,
    legacy_context: HookContext,
) -> None:
    technical = context.payload.setdefault("technical", {})
    if isinstance(technical, dict):
        technical["legacy_context"] = legacy_context


def _find_legacy_hook(
    hook_registry: HookRegistry,
    hook_point: HookPoint,
    handler_ref: str,
) -> object | None:
    hooks = hook_registry._sorted_hooks(hook_registry._hooks.get(hook_point, []))
    target = _string(handler_ref)
    for hook in hooks:
        if _string(getattr(hook, "name", "")) == target:
            return hook
        if _string(getattr(hook.__class__, "__name__", "")) == target:
            return hook
    return None


def _run_legacy_guard_hook(
    hook: GuardHook[HookContext],
    hook_point: HookPoint,
    legacy_context: HookContext,
) -> LifecycleHookOutput:
    try:
        decision = hook.run(legacy_context)
    except Exception as exc:
        return LifecycleHookOutput.from_dict({
            "continue_flow": False,
            "decision": "deny",
            "reason": f"guard hook '{hook.name}' failed at {hook_point.value}: {exc}",
            "diagnostics": [{
                "code": "legacy_guard_failed",
                "hook_name": hook.name,
                "old_hook_point": hook_point.value,
                "message": str(exc),
            }],
        })
    if not isinstance(decision, GuardDecision):
        raise TypeError(
            f"guard hook '{hook.name}' returned {type(decision).__name__}, "
            "expected GuardDecision"
        )
    if not decision.allowed:
        return LifecycleHookOutput.from_dict({
            "continue_flow": False,
            "decision": "deny",
            "reason": decision.reason or f"guard hook '{hook.name}' denied flow",
            "diagnostics": [{
                "code": "legacy_guard_denied",
                "hook_name": hook.name,
                "old_hook_point": hook_point.value,
            }],
        })
    diagnostics: list[dict[str, str]] = []
    if decision.warning:
        diagnostics.append({
            "code": "legacy_guard_warning",
            "hook_name": hook.name,
            "old_hook_point": hook_point.value,
            "message": decision.warning,
        })
    if decision.requires_approval:
        diagnostics.append({
            "code": "legacy_guard_requires_approval_ignored",
            "hook_name": hook.name,
            "old_hook_point": hook_point.value,
            "message": decision.reason or "",
        })
    return LifecycleHookOutput.from_dict({"diagnostics": diagnostics})


def _lifecycle_output_message(output: LifecycleHookOutput) -> str:
    user_message = str(output.user_message or "").strip()
    if user_message:
        return user_message
    if isinstance(output.reason, str):
        return output.reason.strip()
    return ""


def _prompt_adapter_instruction(declaration: LifecycleHookDeclaration) -> str:
    technical = declaration.technical
    for key in ("prompt", "instructions", "template"):
        value = technical.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return declaration.handler_ref.strip()


def _prompt_adapter_messages(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    instruction: str,
) -> list[dict[str, str]]:
    request = {
        "hook": {
            "id": declaration.id,
            "event": declaration.event,
            "source": declaration.source,
            "display_name": declaration.display_name,
            "summary": declaration.summary,
            "risk_level": declaration.risk_level,
            "permissions": list(declaration.permissions),
        },
        "context": {
            "event_name": context.event_name,
            "placement": context.placement,
            "trigger_source": context.source,
            "session_run_id": context.session_run_id,
            "agent_run_id": context.agent_run_id,
            "turn_id": context.turn_id,
            "locale": context.locale,
            "metadata": _jsonable(context.metadata),
            "payload": _jsonable(context.payload),
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a lifecycle hook runtime adapter. Return only one JSON "
                "object matching LifecycleHookOutput. Allowed top-level fields are "
                "continue_flow, decision, reason, user_message, additional_context, "
                "updated_input, diagnostics, and artifacts. Do not include markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Hook instruction:\n{instruction}\n\n"
                "Lifecycle request JSON:\n"
                f"{json.dumps(request, ensure_ascii=False, sort_keys=True)}"
            ),
        },
    ]


def _parse_lifecycle_output_json(raw_content: str) -> dict[str, Any]:
    content = raw_content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("output must be a JSON object")
    return data


def _prompt_adapter_failure_output(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    message: str,
    *,
    code: str,
    raw_content: str = "",
) -> LifecycleHookOutput:
    diagnostic: dict[str, Any] = {}
    if raw_content:
        diagnostic["raw_content_preview"] = raw_content[:500]
    return _adapter_dispatch_failure_output(
        declaration,
        context,
        message,
        code=code,
        handler_type="prompt",
        diagnostics_extra=diagnostic,
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return _jsonable(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _jsonable(dict(value.__dict__))
        except Exception:
            pass
    return str(value)


class _LifecycleCatalogPromptLLM:
    def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("catalog lifecycle prompt model cannot execute")


class _LifecycleCatalogAgent:
    runtime_working_directory = ""
    agent_run_control_plane = object()
    runtime_config = type("_LifecycleCatalogRuntimeConfig", (), {"agent_registry": None})()

    def evaluate_tool_permission(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("catalog lifecycle agent cannot execute permissions")

    def get_tool(self, _name: str) -> Any:
        raise RuntimeError("catalog lifecycle agent cannot execute tools")


def bind_lifecycle_runtime_adapters_to_agent(agent: Any) -> None:
    dispatcher = getattr(agent, "lifecycle_dispatcher", None)
    runtime_adapters = getattr(dispatcher, "runtime_adapters", None)
    get_adapter = getattr(runtime_adapters, "get", None)
    if not callable(get_adapter):
        return
    for handler_type in ("internal", "prompt", "command", "http", "mcp_tool", "agent"):
        adapter = get_adapter(handler_type)
        if adapter is not None and hasattr(adapter, "agent"):
            setattr(adapter, "agent", agent)


def bind_lifecycle_dispatcher_to_hook_registry(
    hook_registry: HookRegistry | None,
    dispatcher: LifecycleHookDispatcher | None,
) -> None:
    if hook_registry is None:
        return
    hooks_by_point = getattr(hook_registry, "_hooks", {})
    values = hooks_by_point.values() if isinstance(hooks_by_point, dict) else []
    for hooks in values:
        for hook in list(hooks):
            setter = getattr(hook, "set_lifecycle_dispatcher", None)
            if not callable(setter):
                continue
            try:
                setter(dispatcher)
            except Exception:
                continue


def _command_adapter_command(declaration: LifecycleHookDeclaration) -> str:
    for key in ("command", "cmd"):
        value = declaration.technical.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return declaration.handler_ref.strip()


def _command_adapter_intent(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
) -> str:
    intent = declaration.technical.get("intent")
    if isinstance(intent, str) and intent.strip():
        return intent.strip()
    return f"Run lifecycle hook {declaration.display_name or declaration.id} for {context.event_name}."


def _http_adapter_url(declaration: LifecycleHookDeclaration) -> str:
    value = declaration.technical.get("url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return declaration.handler_ref.strip()


def _agent_adapter_agent_id(declaration: LifecycleHookDeclaration) -> str:
    value = declaration.technical.get("agent_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return declaration.handler_ref.strip()


def _agent_adapter_prompt(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
) -> str:
    value = declaration.technical.get("prompt")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return (
        "Lifecycle hook request:\n"
        f"{json.dumps(_external_adapter_request(declaration, context), ensure_ascii=False, sort_keys=True)}"
    )


def _adapter_arguments(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
) -> dict[str, Any]:
    for key in ("arguments", "args"):
        value = declaration.technical.get(key)
        if isinstance(value, dict):
            return _jsonable(value)
    return {"lifecycle_context": _external_adapter_request(declaration, context)}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _external_adapter_request(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
) -> dict[str, Any]:
    return {
        "hook": {
            "id": declaration.id,
            "event": declaration.event,
            "handler_type": declaration.handler_type,
            "handler_ref": declaration.handler_ref,
            "source": declaration.source,
            "display_name": declaration.display_name,
            "summary": declaration.summary,
            "permissions": list(declaration.permissions),
            "risk_level": declaration.risk_level,
        },
        "context": {
            "event_name": context.event_name,
            "placement": context.placement,
            "trigger_source": context.source,
            "session_run_id": context.session_run_id,
            "agent_run_id": context.agent_run_id,
            "turn_id": context.turn_id,
            "locale": context.locale,
            "metadata": _jsonable(context.metadata),
            "payload": _jsonable(context.payload),
        },
    }


def _lifecycle_adapter_permission_arguments(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    *,
    agent: Any | None = None,
    handler_type: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        **extra,
        **runtime_boundary_fields(agent),
        "hook_id": declaration.id,
        "event_name": context.event_name,
        "handler_type": handler_type,
        "permissions": list(declaration.permissions),
    }


def _lifecycle_runtime_budget_failure_output(
    agent: Any,
    declaration: LifecycleHookDeclaration,
    *,
    handler_type: str,
) -> LifecycleHookOutput | None:
    if agent is None:
        return None
    message = runtime_budget_limit_message(agent)
    if not message:
        return None
    return _adapter_failure_output(
        declaration,
        f"{handler_type} lifecycle hook blocked by AgentRun budget: {message}",
        code="runtime_budget_exceeded",
        handler_type=handler_type,
        diagnostics_extra={"budget": {"message": message}},
    )


def _record_lifecycle_prompt_usage(agent: Any, response: Any) -> None:
    if agent is None:
        return
    state = getattr(agent, "state", None)
    if state is None:
        return

    def _add_int(attr: str, response_attr: str) -> None:
        try:
            current = int(getattr(state, attr, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        try:
            amount = int(getattr(response, response_attr, 0) or 0)
        except (TypeError, ValueError):
            amount = 0
        setattr(state, attr, current + amount)

    _add_int("total_prompt_tokens", "prompt_tokens")
    _add_int("total_completion_tokens", "completion_tokens")

    usage_extra = getattr(response, "usage_extra", None)
    state_usage_extra = getattr(state, "usage_extra", None)
    if isinstance(usage_extra, dict) and isinstance(state_usage_extra, dict):
        state_usage_extra.update(usage_extra)


def _evaluate_lifecycle_tool_permission(
    agent: Any,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_source: str,
    tool: Any | None = None,
) -> Any | None:
    evaluator = getattr(agent, "evaluate_tool_permission", None)
    if not callable(evaluator):
        return None
    permission_tool = tool or type(
        "_LifecyclePermissionTool",
        (),
        {"name": tool_name, "tool_source": tool_source},
    )()
    tool_call = ToolCall(
        id=f"lifecycle:{tool_name}",
        name=tool_name,
        arguments=dict(arguments or {}),
    )
    return evaluator(permission_tool, tool_call=tool_call)


def _lifecycle_permission_failure_output(
    agent: Any,
    declaration: LifecycleHookDeclaration,
    *,
    handler_type: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_source: str,
    tool: Any | None = None,
) -> LifecycleHookOutput | None:
    try:
        permission = _evaluate_lifecycle_tool_permission(
            agent,
            tool_name=tool_name,
            arguments=arguments,
            tool_source=tool_source,
            tool=tool,
        )
    except Exception as exc:
        return _adapter_failure_output(
            declaration,
            f"{handler_type} lifecycle hook permission check failed: {exc}",
            code="permission_check_failed",
            handler_type=handler_type,
        )
    action = _permission_action_value(permission)
    if not action or action == "allow":
        return None
    if action == "require_approval":
        return _lifecycle_approval_failure_output(
            agent,
            declaration,
            permission,
            handler_type=handler_type,
            tool_name=tool_name,
            arguments=arguments,
            tool_source=tool_source,
        )
    return _permission_denied_lifecycle_output(
        declaration,
        permission,
        handler_type=handler_type,
    )


def _lifecycle_approval_failure_output(
    agent: Any,
    declaration: LifecycleHookDeclaration,
    permission: Any,
    *,
    handler_type: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_source: str,
) -> LifecycleHookOutput | None:
    reason = str(getattr(permission, "reason", "") or "").strip()
    if not bool(getattr(agent, "permission_interactive", True)):
        from reuleauxcoder.domain.permission_gateway import (
            PermissionAction,
            PermissionDecision,
        )

        return _permission_denied_lifecycle_output(
            declaration,
            PermissionDecision(
                action=PermissionAction.BLOCKED_REVIEW,
                authorized=False,
                reason=reason or f"{tool_name} requires lifecycle hook approval.",
                audit={
                    "lifecycle_hook_id": declaration.id,
                    "lifecycle_event": declaration.event,
                    "lifecycle_handler_type": handler_type,
                    "permission": _permission_dict(permission),
                },
            ),
            handler_type=handler_type,
        )
    provider = getattr(agent, "approval_provider", None)
    request_approval = getattr(provider, "request_approval", None)
    if not callable(request_approval):
        return _adapter_failure_output(
            declaration,
            (
                f"{handler_type} lifecycle hook requires approval, but no approval "
                "provider is configured"
            ),
            code="approval_provider_missing",
            handler_type=handler_type,
            diagnostics_extra={"permission": _permission_dict(permission)},
        )
    try:
        from reuleauxcoder.domain.approval import ApprovalRequest

        decision = request_approval(
            ApprovalRequest(
                tool_name=tool_name,
                tool_args=dict(_jsonable(arguments) or {}),
                tool_source=tool_source,
                reason=reason or f"{tool_name} requires lifecycle hook approval.",
                intent=(
                    f"Approve lifecycle hook {declaration.display_name or declaration.id} "
                    f"for {handler_type} execution."
                ),
                metadata={
                    "lifecycle_hook_id": declaration.id,
                    "lifecycle_event": declaration.event,
                    "lifecycle_handler_type": handler_type,
                    "permission": _permission_dict(permission),
                },
            )
        )
    except (KeyboardInterrupt, EOFError):
        return _adapter_failure_output(
            declaration,
            f"{handler_type} lifecycle hook approval was interrupted",
            code="approval_interrupted",
            handler_type=handler_type,
            diagnostics_extra={"permission": _permission_dict(permission)},
        )
    if bool(getattr(decision, "approved", False)):
        return None
    message = str(getattr(decision, "reason", "") or "").strip() or (
        f"{handler_type} lifecycle hook approval was denied"
    )
    return _adapter_failure_output(
        declaration,
        message,
        code="approval_denied",
        handler_type=handler_type,
        diagnostics_extra={"permission": _permission_dict(permission)},
    )


def _execute_lifecycle_tool_call(agent: Any, tool_call: ToolCall) -> str:
    from reuleauxcoder.domain.agent.tool_execution import ToolExecutor

    marker = object()
    previous = getattr(agent, "_suppress_tool_lifecycle", marker)
    setattr(agent, "_suppress_tool_lifecycle", True)
    try:
        return ToolExecutor(agent).execute(tool_call)
    finally:
        if previous is marker:
            try:
                delattr(agent, "_suppress_tool_lifecycle")
            except AttributeError:
                pass
        else:
            setattr(agent, "_suppress_tool_lifecycle", previous)


def _permission_denied_lifecycle_output(
    declaration: LifecycleHookDeclaration,
    decision: Any,
    *,
    handler_type: str,
) -> LifecycleHookOutput:
    action = _permission_action_value(decision)
    reason = str(getattr(decision, "reason", "") or "permission denied")
    return _adapter_failure_output(
        declaration,
        f"{handler_type} lifecycle hook blocked by permission gateway: {reason}",
        code=f"permission_{action or 'denied'}",
        handler_type=handler_type,
        diagnostics_extra={"permission": _permission_dict(decision)},
    )


def _permission_dict(decision: Any) -> dict[str, Any]:
    to_dict = getattr(decision, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, dict):
            return value
    return {
        "action": _permission_action_value(decision),
        "authorized": bool(getattr(decision, "authorized", False)),
        "reason": str(getattr(decision, "reason", "") or ""),
    }


def _permission_action_value(decision: Any) -> str:
    action = getattr(decision, "action", "")
    return str(getattr(action, "value", action) or "")


def _adapter_failure_output(
    declaration: LifecycleHookDeclaration,
    message: str,
    *,
    code: str,
    handler_type: str,
    diagnostics_extra: dict[str, Any] | None = None,
) -> LifecycleHookOutput:
    diagnostic: dict[str, Any] = {
        "code": code,
        "handler_type": handler_type,
        "hook_id": declaration.id,
        "message": message,
    }
    if diagnostics_extra:
        diagnostic.update(_jsonable(diagnostics_extra))
    return LifecycleHookOutput.from_dict({
        "continue_flow": False,
        "decision": "deny",
        "reason": message,
        "diagnostics": [diagnostic],
    })


def _adapter_dispatch_failure_output(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    message: str,
    *,
    code: str,
    handler_type: str,
    diagnostics_extra: dict[str, Any] | None = None,
) -> LifecycleHookOutput:
    diagnostic_extra = {
        "event_name": context.event_name,
        "failure_policy": (
            "fail_closed"
            if lifecycle_gate_event_is_gate(context.event_name)
            else "fail_open"
        ),
        **(_jsonable(diagnostics_extra or {})),
    }
    if lifecycle_gate_event_is_gate(context.event_name):
        return _adapter_failure_output(
            declaration,
            message,
            code=code,
            handler_type=handler_type,
            diagnostics_extra=diagnostic_extra,
        )
    return LifecycleHookOutput.from_dict({
        "diagnostics": [
            {
                "code": code,
                "handler_type": handler_type,
                "hook_id": declaration.id,
                "message": message,
                **diagnostic_extra,
            }
        ]
    })


def _lifecycle_dispatch_failure_output(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    exc: Exception,
) -> LifecycleHookOutput:
    message = (
        f"lifecycle hook '{declaration.display_name or declaration.id}' failed "
        f"during {context.event_name}: {exc}"
    )
    if lifecycle_gate_event_is_gate(context.event_name):
        output = _adapter_failure_output(
            declaration,
            message,
            code="lifecycle_hook_failed_closed",
            handler_type=declaration.handler_type,
            diagnostics_extra={
                "event_name": context.event_name,
                "failure_policy": "fail_closed",
                "error_type": type(exc).__name__,
            },
        )
    else:
        output = LifecycleHookOutput.from_dict({
            "diagnostics": [
                {
                    "code": "lifecycle_hook_failed_open",
                    "handler_type": declaration.handler_type,
                    "hook_id": declaration.id,
                    "event_name": context.event_name,
                    "failure_policy": "fail_open",
                    "error_type": type(exc).__name__,
                    "message": message,
                }
            ]
        })
    return _bounded_lifecycle_hook_output(output, declaration)


def _lifecycle_output_from_external_json(
    declaration: LifecycleHookDeclaration,
    context: LifecycleHookEventContext,
    raw_content: str,
    *,
    handler_type: str,
) -> LifecycleHookOutput:
    try:
        data = _parse_lifecycle_output_json(raw_content)
        return LifecycleHookOutput.from_dict(data)
    except Exception as exc:
        return _adapter_dispatch_failure_output(
            declaration,
            context,
            f"{handler_type} adapter returned invalid LifecycleHookOutput JSON: {exc}",
            code=f"{handler_type}_output_invalid",
            handler_type=handler_type,
            diagnostics_extra={"raw_content_preview": raw_content[:500]},
        )


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def system_builtin_lifecycle_declarations_from_hook_specs(
    specs: list[Any],
) -> list[LifecycleHookDeclaration]:
    """Project legacy Python HookSpec entries as trusted internal declarations."""

    declarations: list[LifecycleHookDeclaration] = []
    for spec in specs:
        hook_class = getattr(spec, "hook_class", None)
        hook_name = getattr(hook_class, "__name__", "") or "Hook"
        old_hook_point = _string(getattr(getattr(spec, "hook_point", None), "value", ""))
        event = _LEGACY_HOOK_POINT_EVENT_MAP.get(old_hook_point)
        if not event:
            continue
        priority = getattr(spec, "priority", 0)
        declarations.append(
            LifecycleHookDeclaration.from_dict(
                f"hook:system_builtin:{hook_name}",
                {
                    "event": event,
                    "source": "system_builtin",
                    "placement": "server",
                    "handler_type": "internal",
                    "handler_ref": hook_name,
                    "matcher": "*",
                    "permissions": [],
                    "display_name": _humanize_hook_name(hook_name),
                    "summary": f"Built-in internal adapter for {hook_name}.",
                    "trust": "trusted",
                    "risk_level": "system",
                    "technical": {
                        "old_hook_point": old_hook_point,
                        "hook_class": hook_name,
                        "priority": priority,
                    },
                },
            )
        )
    return declarations


def system_builtin_lifecycle_declarations_from_hook_registry(
    hook_registry: HookRegistry,
) -> list[LifecycleHookDeclaration]:
    """Project registered legacy HookRegistry hooks as trusted internal declarations."""

    declarations: list[LifecycleHookDeclaration] = []
    for hook_point, hooks in hook_registry._hooks.items():
        event = _LEGACY_HOOK_POINT_EVENT_MAP.get(hook_point.value)
        if not event:
            continue
        for hook in hook_registry._sorted_hooks(hooks):
            hook_name = _string(getattr(hook, "name", "")) or _string(
                getattr(hook.__class__, "__name__", "")
            )
            if not hook_name:
                continue
            hook_class = _string(getattr(hook.__class__, "__name__", "")) or hook_name
            declarations.append(
                _system_builtin_lifecycle_declaration(
                    hook_id=f"hook:system_builtin:{hook_point.value}:{hook_name}",
                    hook_name=hook_name,
                    hook_class=hook_class,
                    old_hook_point=hook_point.value,
                    event=event,
                    priority=int(getattr(hook, "priority", 0) or 0),
                )
            )
    return declarations


def _system_builtin_lifecycle_declaration(
    *,
    hook_id: str,
    hook_name: str,
    hook_class: str,
    old_hook_point: str,
    event: str,
    priority: int,
) -> LifecycleHookDeclaration:
    return LifecycleHookDeclaration.from_dict(
        hook_id,
        {
            "event": event,
            "source": "system_builtin",
            "placement": "server",
            "handler_type": "internal",
            "handler_ref": hook_name,
            "matcher": "*",
            "permissions": [],
            "display_name": _humanize_hook_name(hook_class),
            "summary": f"Built-in internal adapter for {hook_class}.",
            "trust": "trusted",
            "risk_level": "system",
            "technical": {
                "old_hook_point": old_hook_point,
                "hook_class": hook_class,
                "priority": priority,
            },
        },
    )


def lifecycle_declarations_from_config_hooks(
    *,
    owner_id: str,
    source: str,
    hooks: list[dict[str, Any]] | None,
    default_placement: str = "server",
    owner_enabled: bool = True,
    owner_status: str = "installed",
) -> list[LifecycleHookDeclaration]:
    """Project config-level hook manifests into public lifecycle declarations."""

    declarations: list[LifecycleHookDeclaration] = []
    for index, raw_hook in enumerate(hooks or []):
        if not isinstance(raw_hook, dict):
            raise ValueError("lifecycle hook config entry must be an object")
        _validate_lifecycle_hook_config_entry(raw_hook)
        data = dict(raw_hook)
        event = _string(data.get("event"))
        hook_id = canonical_lifecycle_hook_id(source, owner_id, event or "event", index)
        data["source"] = source
        data["owner_id"] = owner_id
        data["owner_enabled"] = owner_enabled
        data["owner_status"] = owner_status or "installed"
        data.setdefault("placement", default_placement)
        data.setdefault("permissions", [])
        data.setdefault("trust", "pending_review")
        _validate_handler_source(data, source=source)
        declarations.append(LifecycleHookDeclaration.from_dict(hook_id, data))
    return declarations


def canonical_lifecycle_hook_id(
    source: str,
    owner_id: str,
    event: str,
    index: int,
) -> str:
    return f"hook:{_string(source)}:{_string(owner_id)}:{_string(event) or 'event'}:{int(index)}"


def validate_lifecycle_hook_manifest(
    raw_hook: dict[str, Any],
    *,
    owner_id: str,
    source: str,
    index: int = 0,
    default_placement: str = "server",
    owner_enabled: bool = True,
    owner_status: str = "installed",
) -> LifecycleHookDeclaration:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id=owner_id,
        source=source,
        hooks=[raw_hook],
        default_placement=default_placement,
        owner_enabled=owner_enabled,
        owner_status=owner_status,
    )
    declaration = declarations[0]
    expected_id = canonical_lifecycle_hook_id(source, owner_id, declaration.event, index)
    if index != 0:
        data = declaration.to_dict()
        data["id"] = expected_id
        declaration = LifecycleHookDeclaration.from_dict(expected_id, data)
    return declaration


def sanitize_lifecycle_hooks_for_config(
    value: Any,
    *,
    owner_id: str,
    source: str,
    default_placement: str = "server",
    default_trust: str | None = "pending_review",
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for index, raw_hook in enumerate(value):
        if not isinstance(raw_hook, dict):
            raise ValueError("lifecycle hook config entry must be an object")
        raw = dict(raw_hook)
        if default_trust is not None:
            raw["trust"] = default_trust
        declaration = validate_lifecycle_hook_manifest(
            raw,
            owner_id=owner_id,
            source=source,
            index=index,
            default_placement=default_placement,
        )
        item: dict[str, Any] = {
            "event": declaration.event,
            "placement": declaration.placement,
            "handler_type": declaration.handler_type,
            "display_name": declaration.display_name,
            "summary": declaration.summary,
            "permissions": list(declaration.permissions),
            "trust": declaration.trust,
        }
        if declaration.credentials:
            item["credentials"] = list(declaration.credentials)
        if declaration.handler_ref:
            item["handler_ref"] = declaration.handler_ref
        if declaration.matcher != "*":
            item["matcher"] = declaration.matcher
        if declaration.risk_level:
            item["risk_level"] = declaration.risk_level
        if declaration.technical:
            technical = dict(declaration.technical)
            if technical:
                item["technical"] = technical
        sanitized.append(item)
    return sanitized


def lifecycle_registry_from_config(config: Any) -> LifecycleHookRegistry:
    """Build a declarative lifecycle registry from server config objects."""

    declarations: list[LifecycleHookDeclaration] = []
    skills = getattr(getattr(config, "skills", None), "items", {})
    if isinstance(skills, dict):
        for name, skill in skills.items():
            declarations.extend(
                lifecycle_declarations_from_config_hooks(
                    owner_id=str(getattr(skill, "name", "") or name),
                    source="skill",
                    hooks=_owner_hooks(skill),
                    owner_enabled=_owner_enabled(skill),
                    owner_status=_owner_status(skill),
                )
            )

    mcp_servers = getattr(config, "mcp_servers", [])
    if isinstance(mcp_servers, list):
        for server in mcp_servers:
            declarations.extend(
                lifecycle_declarations_from_config_hooks(
                    owner_id=str(getattr(server, "name", "") or ""),
                    source="mcp_server",
                    hooks=_owner_hooks(server),
                    owner_enabled=_owner_enabled(server),
                    owner_status=_owner_status(server),
                )
            )

    packages = getattr(config, "capability_packages", {})
    if isinstance(packages, dict):
        for package_id, package in packages.items():
            declarations.extend(
                lifecycle_declarations_from_config_hooks(
                    owner_id=str(getattr(package, "id", "") or package_id),
                    source="capability_package",
                    hooks=_owner_hooks(package),
                    owner_enabled=_owner_enabled(package),
                    owner_status=_owner_status(package),
                )
            )

    components = getattr(config, "capability_components", {})
    if isinstance(components, dict):
        for component_id, component in components.items():
            declarations.extend(
                lifecycle_declarations_from_config_hooks(
                    owner_id=str(getattr(component, "id", "") or component_id),
                    source=_component_hook_source(component),
                    hooks=_owner_hooks(component),
                    owner_enabled=_owner_enabled(component),
                    owner_status=_owner_status(component),
                )
            )

    return LifecycleHookRegistry(declarations)


def _owner_hooks(owner: Any) -> list[dict[str, Any]]:
    hooks = getattr(owner, "hooks", [])
    return [dict(item) for item in hooks] if isinstance(hooks, list) else []


def _owner_enabled(owner: Any) -> bool:
    return _bool_value(getattr(owner, "enabled", True), True)


def _owner_status(owner: Any) -> str:
    return _string(getattr(owner, "status", None), "installed")


def _component_hook_source(component: Any) -> str:
    kind = str(getattr(component, "kind", "") or "").strip()
    if kind == "skill":
        return "skill"
    if kind in {"mcp", "mcp_server", "mcp_tool"}:
        return "mcp_server"
    return "capability_package"


def _string(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip()


def _bool_value(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _required_string(data: dict[str, Any], field_name: str) -> str:
    value = _string(data.get(field_name))
    if not value:
        raise ValueError(f"lifecycle hook declaration {field_name} is required")
    return value


def _choice(
    data: dict[str, Any],
    field_name: str,
    allowed: set[str],
    fallback: str,
) -> str:
    value = _string(data.get(field_name), fallback)
    if value not in allowed:
        raise ValueError(
            f"lifecycle hook {field_name} must be one of {', '.join(sorted(allowed))}; "
            f"got {value!r}"
        )
    return value


def _required_choice(
    data: dict[str, Any],
    field_name: str,
    allowed: set[str],
) -> str:
    if field_name not in data:
        raise ValueError(f"lifecycle hook declaration {field_name} is required")
    return _choice(data, field_name, allowed, "")


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"lifecycle hook declaration {field_name} must be a list")
    return [_string(item) for item in value if _string(item)]


def _list(value: Any, field_name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"lifecycle hook output {field_name} must be a list")
    return list(value)


def _matches_optional(value: str, expected: str | None) -> bool:
    return expected is None or value == expected


def _placement_matches(declaration_placement: str, expected: str | None) -> bool:
    if expected is None:
        return True
    if declaration_placement == expected:
        return True
    return declaration_placement == "both" and expected in {"server", "peer"}


def _matcher_matches(matcher: Any, context: LifecycleHookEventContext) -> bool:
    if matcher in (None, "", "*"):
        return True
    matcher = _validated_matcher(matcher)
    if matcher == "*":
        return True
    fields = _standard_matcher_fields(context)
    for key, expected in matcher.items():
        actual = fields.get(key)
        if not _matcher_value_matches(actual, expected):
            return False
    return True


def _matcher_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        if not actual:
            return False
        if isinstance(expected, list):
            return bool(set(actual).intersection(expected))
        return expected in actual
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def _dashboard_item(
    declaration: LifecycleHookDeclaration,
    *,
    runtime_adapters: LifecycleHookRuntimeAdapterRegistry,
    recent_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    technical = dict(declaration.technical)
    if declaration.handler_ref:
        technical.setdefault("handler_ref", declaration.handler_ref)
    technical.setdefault("matcher", declaration.matcher)
    runtime_unavailable_reason = _runtime_unavailable_reason(
        declaration,
        runtime_adapters=runtime_adapters,
    )
    executable = declaration.trust == "trusted" and not runtime_unavailable_reason
    placement_runtime = _placement_runtime(
        declaration,
        runtime_adapters=runtime_adapters,
    )
    unavailable_reason = (
        ""
        if executable
        else f"trust:{declaration.trust}"
        if declaration.trust != "trusted"
        else runtime_unavailable_reason
    )
    can_manage = bool(declaration.id) and declaration.source != "system_builtin"
    return {
        "id": declaration.id,
        "event": declaration.event,
        "source": declaration.source,
        "owner_id": declaration.owner_id,
        "owner_enabled": declaration.owner_enabled,
        "owner_status": declaration.owner_status,
        "placement": declaration.placement,
        "handler_type": declaration.handler_type,
        "display_name": declaration.display_name,
        "summary": declaration.summary,
        "trust": declaration.trust,
        "enabled": executable,
        "executable": executable,
        "can_manage": can_manage,
        "management_actions": _lifecycle_hook_management_actions(declaration)
        if can_manage
        else [],
        "unavailable_reason": unavailable_reason,
        "placement_runtime": placement_runtime,
        "runtime_context_required": _runtime_context_required(declaration),
        "permissions": list(declaration.permissions),
        "credentials": list(declaration.credentials),
        "risk_level": declaration.risk_level,
        "recent_result": _lifecycle_hook_recent_result(
            declaration,
            recent_results,
        ),
        "technical": technical,
    }


def _lifecycle_hook_recent_result(
    declaration: LifecycleHookDeclaration,
    recent_results: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(recent_results, dict):
        for key in (
            declaration.id,
            declaration.event,
            declaration.handler_ref,
        ):
            raw = recent_results.get(key)
            if isinstance(raw, dict):
                return _sanitize_lifecycle_hook_recent_result(raw)
    return {
        "status": "unrecorded",
        "summary": "No lifecycle executions recorded.",
    }


def _sanitize_lifecycle_hook_recent_result(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "summary",
        "reason",
        "decision",
        "event",
        "session_run_id",
        "agent_run_id",
        "occurred_at",
        "updated_at",
    }
    result = {
        key: _string(value)
        for key, value in raw.items()
        if key in allowed and _string(value)
    }
    if "status" not in result:
        result["status"] = "unknown"
    if "summary" not in result:
        result["summary"] = result.get("reason", "") or result["status"]
    return result


def _lifecycle_hook_management_actions(
    declaration: LifecycleHookDeclaration,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    labels = {
        "pending_review": "Mark pending review",
        "trusted": "Trust hook",
        "disabled": "Disable hook",
        "blocked": "Block hook",
    }
    for trust in ("pending_review", "trusted", "disabled", "blocked"):
        if trust == declaration.trust:
            continue
        actions.append({
            "trust": trust,
            "label": labels[trust],
            "endpoint": "admin.lifecycle_hooks.trust",
        })
    return actions


def _placement_runtime(
    declaration: LifecycleHookDeclaration,
    *,
    runtime_adapters: LifecycleHookRuntimeAdapterRegistry,
) -> dict[str, dict[str, Any]]:
    runtime: dict[str, dict[str, Any]] = {}
    placements = ["server", "peer"] if declaration.placement == "both" else [declaration.placement]
    for placement in placements:
        reason = _runtime_unavailable_reason(
            declaration,
            placement=placement,
            runtime_adapters=runtime_adapters,
        )
        executable = declaration.trust == "trusted" and not reason
        runtime[placement] = {
            "executable": executable,
            "unavailable_reason": (
                ""
                if executable
                else f"trust:{declaration.trust}"
                if declaration.trust != "trusted"
                else reason
            ),
        }
    return runtime


def build_lifecycle_event_context(
    event_name: str,
    *,
    placement: str = "server",
    trigger_source: str = "",
    session_run_id: str = "",
    agent_run_id: str = "",
    turn_id: str = "",
    origin: str = "agent",
    locale: str = "",
    metadata: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> LifecycleHookEventContext:
    resolved_source = _string(trigger_source, "chat")
    resolved_payload = dict(payload or {})
    timestamp = _utc_timestamp()
    standard = {
        "event_name": event_name,
        "placement": placement,
        "trigger_source": resolved_source,
        "session_run_id": _string(session_run_id),
        "agent_run_id": _string(agent_run_id),
        "turn_id": _string(turn_id),
    }
    standard["timestamp"] = timestamp
    for key in _AUTHORITATIVE_CONTEXT_FIELDS:
        resolved_payload.pop(key, None)
    event_payload = dict(resolved_payload)
    event_payload.update(standard)
    return LifecycleHookEventContext(
        event_name=event_name,
        placement=placement,
        session_run_id=standard["session_run_id"],
        agent_run_id=standard["agent_run_id"],
        turn_id=standard["turn_id"],
        source=resolved_source,
        origin=origin,
        locale=_string(locale),
        timestamp=timestamp,
        metadata=dict(metadata or {}),
        payload=event_payload,
    )


def build_tool_lifecycle_payload(
    event_name: str,
    *,
    tool_call: Any,
    tool: Any,
    tool_source: str,
    mcp_server: str | None = None,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_name = _string(getattr(tool, "name", None), _string(getattr(tool_call, "name", "")))
    tool_call_id = _string(getattr(tool_call, "id", ""))
    resolved_tool_source = _string(tool_source)
    resolved_mcp_server = _string(mcp_server, _string(getattr(tool, "server_name", "")))
    payload: dict[str, Any] = {
        "tool_names": _single_item_list(tool_name),
        "tool_call_ids": _single_item_list(tool_call_id),
        "tool_sources": _single_item_list(resolved_tool_source),
        "mcp_servers": _single_item_list(resolved_mcp_server),
        "technical": {
            "tool_call": _tool_call_payload(tool_call),
            "tool": {
                "name": tool_name,
                "source": resolved_tool_source,
                "mcp_server": resolved_mcp_server,
                "description": getattr(tool, "description", None),
                "schema": getattr(tool, "parameters", None),
            },
        },
    }
    if result is not None:
        payload["technical"]["result"] = result
    if error is not None:
        payload["technical"]["error"] = dict(error)
    return payload


def build_tool_batch_lifecycle_payload(
    *,
    tool_calls: list[Any],
    results: list[str],
    tool_sources: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    calls = [_tool_call_payload(item) for item in tool_calls]
    names = [_string(item.get("name")) for item in calls if _string(item.get("name"))]
    call_ids = [_string(item.get("id")) for item in calls if _string(item.get("id"))]
    sources = [_string(item) for item in (tool_sources or []) if _string(item)]
    servers = [_string(item) for item in (mcp_servers or []) if _string(item)]
    return {
        "tool_names": names,
        "tool_call_ids": call_ids,
        "tool_sources": sources,
        "mcp_servers": servers,
        "technical": {
            "tool_calls": calls,
            "results": list(results),
        },
    }


def build_permission_lifecycle_payload(request: Any) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", None)
    target = getattr(request, "target", None)
    subject = getattr(request, "subject", None)
    tool_name = _string(
        getattr(tool_call, "name", None),
        _string(getattr(target, "name", "")),
    )
    tool_call_id = _string(getattr(tool_call, "id", ""))
    tool_source = _string(getattr(target, "tool_source", ""))
    mcp_server = _string(getattr(target, "mcp_server", ""))
    trigger_source = _string(getattr(subject, "trigger_source", ""), "chat")
    session_run_id = _string(getattr(subject, "session_id", ""))
    return {
        "tool_names": _single_item_list(tool_name),
        "tool_call_ids": _single_item_list(tool_call_id),
        "tool_sources": _single_item_list(tool_source),
        "mcp_servers": _single_item_list(mcp_server),
        "technical": {
            "subject": {
                "agent_id": getattr(subject, "agent_id", ""),
                "role": getattr(subject, "role", ""),
                "visibility": getattr(subject, "visibility", ""),
                "trigger_source": trigger_source,
                "interactive": getattr(subject, "interactive", False),
                "runtime_profile_id": getattr(subject, "runtime_profile_id", ""),
                "session_id": session_run_id,
                "task_id": getattr(subject, "task_id", ""),
                "workspace_root": getattr(subject, "workspace_root", ""),
            },
            "target": {
                "kind": getattr(target, "kind", ""),
                "name": getattr(target, "name", ""),
                "tool_source": tool_source,
                "registry_path": getattr(target, "registry_path", ""),
                "component_id": getattr(target, "component_id", ""),
                "mcp_server": mcp_server,
                "mcp_tool": getattr(target, "mcp_tool", ""),
                "target_agent_id": getattr(target, "target_agent_id", ""),
            },
            "action": getattr(request, "action", ""),
            "tool_call": _tool_call_payload(tool_call),
            "effective_capabilities": dict(getattr(request, "effective_capabilities", {}) or {}),
            "runtime_profile": dict(getattr(request, "runtime_profile", {}) or {}),
            "metadata": dict(getattr(request, "metadata", {}) or {}),
        },
    }


def _tool_call_payload(tool_call: Any) -> dict[str, Any]:
    if tool_call is None:
        return {"id": "", "name": "", "arguments": {}}
    raw_arguments = getattr(tool_call, "arguments", {})
    return {
        "id": _string(getattr(tool_call, "id", "")),
        "name": _string(getattr(tool_call, "name", "")),
        "arguments": dict(raw_arguments or {}) if isinstance(raw_arguments, dict) else {},
    }


def _single_item_list(value: Any) -> list[str]:
    resolved = _string(value)
    return [resolved] if resolved else []


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_lifecycle_hook_config_entry(raw_hook: dict[str, Any]) -> None:
    for field in raw_hook:
        if field not in CONFIG_HOOK_FIELDS:
            raise ValueError(f"lifecycle hook config field '{field}' is not supported")

    technical = raw_hook.get("technical")
    if technical is None:
        return
    if not isinstance(technical, dict):
        raise ValueError("lifecycle hook config technical must be an object")
    for field in technical:
        if field in TECHNICAL_FORBIDDEN_FIELDS:
            raise ValueError(
                f"lifecycle hook config technical field '{field}' is not supported"
            )


def _validate_handler_source(raw_hook: dict[str, Any], *, source: str) -> None:
    event = _string(raw_hook.get("event"))
    if event not in LIFECYCLE_HOOK_CONFIG_EVENTS:
        allowed = ", ".join(sorted(LIFECYCLE_HOOK_CONFIG_EVENTS))
        raise ValueError(
            f"lifecycle hook event '{event}' is not supported for external "
            f"configuration; supported events: {allowed}"
        )
    handler_type = _string(raw_hook.get("handler_type"))
    if (
        handler_type == "internal"
        and source not in _INTERNAL_HANDLER_ALLOWED_SOURCES
    ):
        raise ValueError(
            "lifecycle hook internal handlers are limited to system_builtin "
            "or admin_managed sources"
        )


def _standard_matcher_fields(context: LifecycleHookEventContext) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "event_name": context.event_name,
        "placement": context.placement,
        "trigger_source": context.source,
        "session_run_id": context.session_run_id,
        "agent_run_id": context.agent_run_id,
        "turn_id": context.turn_id,
    }
    for key in LIFECYCLE_TOOL_MATCHER_FIELDS:
        if key in context.payload:
            fields[key] = context.payload.get(key)
    return fields


def _validated_matcher(matcher: Any) -> Any:
    if matcher in (None, "", "*"):
        return "*"
    if not isinstance(matcher, dict):
        raise ValueError("lifecycle hook declaration matcher must be '*' or an object")
    validated: dict[str, Any] = {}
    for key, expected in matcher.items():
        field = _string(key)
        if field not in LIFECYCLE_HOOK_MATCHER_FIELDS:
            raise ValueError(f"lifecycle hook declaration matcher field '{field}' is not supported")
        validated[field] = _validated_matcher_value(field, expected)
    return validated


def _validated_matcher_value(field: str, value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        validated: list[Any] = []
        for item in value:
            if not (item is None or isinstance(item, (str, int, float, bool))):
                raise ValueError(
                    f"lifecycle hook declaration matcher field '{field}' must be a primitive value or list"
                )
            validated.append(item)
        return validated
    raise ValueError(
        f"lifecycle hook declaration matcher field '{field}' must be a primitive value or list"
    )


def _runtime_unavailable_reason(
    declaration: LifecycleHookDeclaration,
    *,
    placement: str | None = None,
    runtime_adapters: LifecycleHookRuntimeAdapterRegistry,
) -> str:
    owner_reason = _owner_unavailable_reason(declaration)
    if owner_reason:
        return owner_reason
    placement_reason = _placement_unavailable_reason(declaration, placement=placement)
    if placement_reason:
        return placement_reason
    return runtime_adapters.unavailable_reason(declaration, placement=placement)


def _owner_unavailable_reason(declaration: LifecycleHookDeclaration) -> str:
    if not declaration.owner_enabled:
        return "owner_disabled"
    status = declaration.owner_status.strip().lower()
    if status in _INACTIVE_OWNER_STATUSES:
        return f"owner_status:{status}"
    return ""


def _placement_unavailable_reason(
    declaration: LifecycleHookDeclaration,
    *,
    placement: str | None = None,
) -> str:
    if not _placement_matches(declaration.placement, placement):
        return "placement_mismatch"
    if placement == "peer" or (placement is None and declaration.placement == "peer"):
        return "peer_runtime_unavailable"
    return ""


def _adapter_runtime_unavailable_reason(
    adapter: LifecycleHookRuntimeAdapter,
    declaration: LifecycleHookDeclaration,
    *,
    placement: str | None = None,
) -> str:
    placements = (
        [placement]
        if placement
        else ["server", "peer"]
        if declaration.placement == "both"
        else [declaration.placement]
    )
    supported_placements = set(getattr(adapter, "supported_placements", set()) or set())
    if not any(item in supported_placements for item in placements):
        if placement:
            return f"{placement}_runtime_unavailable"
        if declaration.placement == "peer":
            return "peer_runtime_unavailable"
        return "runtime_placement_unavailable"
    supported_events = getattr(adapter, "supported_events", None)
    if supported_events is not None and declaration.event not in supported_events:
        return f"event_unavailable:{declaration.event}"
    return ""


def _runtime_context_required(declaration: LifecycleHookDeclaration) -> list[str]:
    if declaration.handler_type == "internal" and declaration.permissions:
        return ["agent"]
    return list(
        {
            "prompt": ["prompt_model"],
            "command": ["agent"],
            "http": ["agent"],
            "mcp_tool": ["agent"],
            "agent": ["agent", "agent_run_control_plane"],
        }.get(declaration.handler_type, [])
    )


def _humanize_hook_name(value: str) -> str:
    text = value.replace("Hook", "")
    words: list[str] = []
    current = ""
    for char in text:
        if char.isupper() and current:
            words.append(current)
            current = char
        else:
            current += char
    if current:
        words.append(current)
    return " ".join(words) or value


__all__ = [
    "LIFECYCLE_HOOK_DECISIONS",
    "LIFECYCLE_HOOK_EVENTS",
    "LIFECYCLE_HOOK_CONFIG_EVENTS",
    "LIFECYCLE_HOOK_HANDLER_TYPES",
    "LIFECYCLE_HOOK_MATCHER_FIELDS",
    "LIFECYCLE_HOOK_PLACEMENTS",
    "LIFECYCLE_HOOK_SOURCES",
    "LIFECYCLE_HOOK_TRUST_STATES",
    "AgentLifecycleHookRuntimeAdapter",
    "CommandLifecycleHookRuntimeAdapter",
    "HttpLifecycleHookRuntimeAdapter",
    "LifecycleHookDeclaration",
    "DeclarativeLifecycleHookRuntimeAdapter",
    "FunctionLifecycleHookRuntimeAdapter",
    "InternalHookRegistryLifecycleHookRuntimeAdapter",
    "InternalLifecycleHookPointResult",
    "MCPToolLifecycleHookRuntimeAdapter",
    "PromptLifecycleHookRuntimeAdapter",
    "LifecycleHookDispatcher",
    "LifecycleHookDispatchResult",
    "LifecycleHookEventContext",
    "LifecycleHookHandler",
    "LifecycleHookOutput",
    "LifecycleHookRegistry",
    "LifecycleHookRuntimeAdapter",
    "LifecycleHookRuntimeAdapterRegistry",
    "annotate_lifecycle_output_diagnostics",
    "build_lifecycle_event_context",
    "build_permission_lifecycle_payload",
    "build_tool_batch_lifecycle_payload",
    "build_tool_lifecycle_payload",
    "canonical_lifecycle_hook_id",
    "bind_lifecycle_dispatcher_to_hook_registry",
    "bind_lifecycle_runtime_adapters_to_agent",
    "default_lifecycle_hook_catalog_runtime_adapters",
    "default_lifecycle_hook_runtime_adapters",
    "dispatch_internal_lifecycle_hook_point",
    "lifecycle_declarations_from_config_hooks",
    "lifecycle_event_catalog_items",
    "lifecycle_registry_from_config",
    "sanitize_lifecycle_hooks_for_config",
    "system_builtin_lifecycle_declarations_from_hook_registry",
    "system_builtin_lifecycle_declarations_from_hook_specs",
    "validate_lifecycle_hook_manifest",
]
