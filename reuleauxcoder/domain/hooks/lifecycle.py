"""Declarative lifecycle hook contract.

This module defines the public hook schema. It is intentionally separate from
the in-process Python HookRegistry, which remains an internal runtime adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

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

_AUTHORITATIVE_CONTEXT_FIELDS: set[str] = {
    "event_name",
    "placement",
    "trigger_source",
    "session_run_id",
    "agent_run_id",
    "turn_id",
    "timestamp",
}

CONFIG_HOOK_FIELDS: set[str] = {
    "event",
    "placement",
    "handler_type",
    "handler_ref",
    "matcher",
    "display_name",
    "summary",
    "permissions",
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

_SERVER_RUNTIME_HANDLER_TYPES: set[str] = {"internal"}

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
        return cls(
            continue_flow=bool(data.get("continue_flow", True)),
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


LifecycleHookHandler = Callable[
    [LifecycleHookDeclaration, LifecycleHookEventContext],
    LifecycleHookOutput,
]


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
        handler_types: set[str] | None = None,
    ) -> list[LifecycleHookDeclaration]:
        return [
            declaration
            for declaration in self.query(event=event, trust="trusted")
            if _runtime_unavailable_reason(
                declaration,
                placement=placement,
                handler_types=handler_types,
            )
            == ""
        ]

    def dashboard_items(self) -> list[dict[str, Any]]:
        return [_dashboard_item(declaration) for declaration in self._declarations.values()]


class LifecycleHookDispatcher:
    """Dispatch trusted lifecycle hook declarations to registered handlers."""

    def __init__(
        self,
        registry: LifecycleHookRegistry,
        *,
        handlers: dict[str, LifecycleHookHandler],
    ) -> None:
        self.registry = registry
        self.handlers = dict(handlers)

    def dispatch(
        self,
        context: LifecycleHookEventContext,
    ) -> list[LifecycleHookDispatchResult]:
        results: list[LifecycleHookDispatchResult] = []
        declarations = [
            declaration
            for declaration in self.registry.query(
                event=context.event_name,
                trust="trusted",
            )
            if _runtime_unavailable_reason(
                declaration,
                placement=context.placement,
                handler_types=set(self.handlers),
                include_handler=False,
            )
            == ""
        ]
        for declaration in declarations:
            if not _matcher_matches(declaration.matcher, context):
                continue
            handler = self.handlers.get(declaration.handler_type)
            if handler is None:
                output = LifecycleHookOutput.from_dict({
                    "diagnostics": [{
                        "code": "handler_unavailable",
                        "handler_type": declaration.handler_type,
                    }]
                })
            else:
                output = _coerce_hook_output(handler(declaration, context))
            results.append(LifecycleHookDispatchResult(declaration, output))
        return results


def default_lifecycle_hook_handlers() -> dict[str, LifecycleHookHandler]:
    """Return built-in declarative handlers for the production dispatcher.

    Handler runtimes such as command/http/mcp/prompt are intentionally explicit.
    Until a runtime adapter is implemented, a hook may only return a declarative
    output stored in technical.output; otherwise dispatch records a diagnostic.
    """

    return {"internal": _declarative_output_handler}


def _declarative_output_handler(
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
                "code": "handler_unavailable",
                "handler_type": declaration.handler_type,
                "handler_ref": declaration.handler_ref,
            }
        ]
    })


def _coerce_hook_output(value: Any) -> LifecycleHookOutput:
    if isinstance(value, LifecycleHookOutput):
        return value
    if isinstance(value, dict):
        return LifecycleHookOutput.from_dict(value)
    raise ValueError("lifecycle hook handler must return LifecycleHookOutput or dict")


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
            f"lifecycle hook {field_name} must be one of {', '.join(sorted(allowed))}"
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


def _dashboard_item(declaration: LifecycleHookDeclaration) -> dict[str, Any]:
    technical = dict(declaration.technical)
    if declaration.handler_ref:
        technical.setdefault("handler_ref", declaration.handler_ref)
    technical.setdefault("matcher", declaration.matcher)
    runtime_unavailable_reason = _runtime_unavailable_reason(declaration)
    executable = declaration.trust == "trusted" and not runtime_unavailable_reason
    placement_runtime = _placement_runtime(declaration)
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
        "unavailable_reason": unavailable_reason,
        "placement_runtime": placement_runtime,
        "permissions": list(declaration.permissions),
        "risk_level": declaration.risk_level,
        "technical": technical,
    }


def _placement_runtime(declaration: LifecycleHookDeclaration) -> dict[str, dict[str, Any]]:
    runtime: dict[str, dict[str, Any]] = {}
    placements = ["server", "peer"] if declaration.placement == "both" else [declaration.placement]
    for placement in placements:
        reason = _runtime_unavailable_reason(declaration, placement=placement)
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
    handler_types: set[str] | None = None,
    include_handler: bool = True,
) -> str:
    owner_reason = _owner_unavailable_reason(declaration)
    if owner_reason:
        return owner_reason
    placement_reason = _placement_unavailable_reason(declaration, placement=placement)
    if placement_reason:
        return placement_reason
    if include_handler:
        handler_reason = _handler_unavailable_reason(
            declaration,
            handler_types=handler_types,
        )
        if handler_reason:
            return handler_reason
    return ""


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


def _handler_unavailable_reason(
    declaration: LifecycleHookDeclaration,
    *,
    handler_types: set[str] | None = None,
) -> str:
    available = handler_types if handler_types is not None else _SERVER_RUNTIME_HANDLER_TYPES
    if declaration.handler_type in available:
        return ""
    return f"handler_unavailable:{declaration.handler_type}"


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
    "LIFECYCLE_HOOK_HANDLER_TYPES",
    "LIFECYCLE_HOOK_MATCHER_FIELDS",
    "LIFECYCLE_HOOK_PLACEMENTS",
    "LIFECYCLE_HOOK_SOURCES",
    "LIFECYCLE_HOOK_TRUST_STATES",
    "LifecycleHookDeclaration",
    "LifecycleHookDispatcher",
    "LifecycleHookDispatchResult",
    "LifecycleHookEventContext",
    "LifecycleHookHandler",
    "LifecycleHookOutput",
    "LifecycleHookRegistry",
    "build_lifecycle_event_context",
    "build_permission_lifecycle_payload",
    "build_tool_batch_lifecycle_payload",
    "build_tool_lifecycle_payload",
    "canonical_lifecycle_hook_id",
    "default_lifecycle_hook_handlers",
    "lifecycle_declarations_from_config_hooks",
    "lifecycle_registry_from_config",
    "sanitize_lifecycle_hooks_for_config",
    "system_builtin_lifecycle_declarations_from_hook_specs",
    "validate_lifecycle_hook_manifest",
]
