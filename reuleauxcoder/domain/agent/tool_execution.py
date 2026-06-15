"""Tool execution - handles tool calls."""

from __future__ import annotations
from typing import TYPE_CHECKING, Any, List
import concurrent.futures
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
import threading

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent, ToolFailureKind
from reuleauxcoder.domain.agent.runtime_boundary import (
    runtime_agent_run_id,
    runtime_path_space,
    runtime_workspace_root,
    runtime_working_directory,
)
from reuleauxcoder.domain.agent.runtime_budget import (
    runtime_budget_int,
    runtime_budget_limit_message,
)
from reuleauxcoder.domain.agent.tool_arguments import (
    format_tool_argument_retry_message,
    policy_for_provider,
    validate_and_repair_tool_arguments,
)
from reuleauxcoder.domain.agent.tool_diagnostics import (
    ToolDiagnostic,
    ToolDiagnosticKind,
    ToolDiagnosticStage,
    diagnostic_to_dict,
    diagnostics_from_argument_validation,
    tool_diagnostic_from_failure,
)
from reuleauxcoder.app.runtime.agent_runtime import (
    AgentRunCancelled,
    get_interactive_run_limiter,
)
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.files import LocalWorkspaceMutationBackend
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    HookPoint,
)
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookOutput,
    annotate_lifecycle_output_diagnostics,
    build_lifecycle_event_context,
    build_tool_batch_lifecycle_payload,
    build_tool_lifecycle_payload,
    dispatch_internal_lifecycle_hook_point,
    lifecycle_output_audit_fields,
    lifecycle_runtime_artifacts_for_event,
)
from reuleauxcoder.domain.hooks.lifecycle_policy import (
    lifecycle_gate_output_is_terminal,
    lifecycle_output_message,
    lifecycle_output_requests_approval,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
from reuleauxcoder.domain.memory.runtime import memory_metadata_from_agent
from reuleauxcoder.extensions.tools.registry import get_tool
from reuleauxcoder.extensions.tools.spec import ToolExposure
from reuleauxcoder.services.llm.diagnostics import persist_tool_diagnostic_event


_RUNTIME_BUDGET_LOCK_INIT_LOCK = threading.Lock()


@dataclass(slots=True)
class _PreToolLifecycleResult:
    blocked_message: str | None = None
    approval_message: str | None = None
    approval_hooks: list[dict[str, str]] | None = None


@dataclass(slots=True)
class _LifecycleOutputRecord:
    output: LifecycleHookOutput
    hook_id: str
    display_name: str
    handler_type: str


@dataclass(slots=True)
class _MutationPreviewOutcome:
    status: str
    tool_name: str
    tool_call_id: str | None
    item_id: str
    changes: list[dict] = field(default_factory=list)
    diff: str = ""
    plan_id: str | None = None
    preview_identity: dict = field(default_factory=dict)
    approved_save_candidate: dict = field(default_factory=dict)
    error: str | None = None
    failure_code: str | None = None
    retry_hint: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.error is None


@dataclass(frozen=True, slots=True)
class CapabilityTargetContext:
    gateway_tool_name: str
    parent_tool_call_id: str
    target_tool_call_id: str
    target_tool_id: str
    target_tool_name: str
    target_arguments: dict[str, Any]
    target_exposure: str
    target_risk: str
    target_permission_policy: str

    def as_event_metadata(self) -> dict[str, Any]:
        return {
            "gateway_tool_name": self.gateway_tool_name,
            "parent_tool_call_id": self.parent_tool_call_id,
            "target_tool_call_id": self.target_tool_call_id,
            "target_tool_id": self.target_tool_id,
            "target_tool_name": self.target_tool_name,
            "target_arguments": dict(self.target_arguments),
            "target_exposure": self.target_exposure,
            "target_risk": self.target_risk,
            "target_permission_policy": self.target_permission_policy,
        }

    def as_execute_trace(self) -> dict[str, Any]:
        return {
            "gateway_tool_name": self.gateway_tool_name,
            "parent_tool_call_id": self.parent_tool_call_id,
            "target_tool_call_id": self.target_tool_call_id,
            "tool_id": self.target_tool_id,
            "target_tool_id": self.target_tool_id,
            "target_tool_name": self.target_tool_name,
            "target_exposure": self.target_exposure,
            "target_risk": self.target_risk,
            "target_permission_policy": self.target_permission_policy,
        }


def _meta_has_approval_denied(meta: dict | None) -> bool:
    diagnostics = (meta or {}).get("tool_diagnostics")
    if not isinstance(diagnostics, list):
        return False
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        if str(diagnostic.get("kind") or "") == ToolFailureKind.APPROVAL_DENIED.value:
            return True
    return False


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self._budget_lock = self._runtime_budget_lock()
        self._file_change_started_item_ids: set[str] = set()
        self._file_change_changes_by_item_id: dict[str, list[dict]] = {}
        self._mutation_preview_outcomes_by_item_id: dict[str, _MutationPreviewOutcome] = {}
        self._anonymous_tool_invocation_seq = 0
        self._tool_result_meta_by_call_identity: dict[int, dict[str, Any]] = {}
        self._capability_target_context_by_call_id: dict[str, CapabilityTargetContext] = {}

    def _runtime_budget_lock(self) -> threading.Lock:
        lock = getattr(self.agent, "runtime_budget_lock", None)
        if lock is not None:
            return lock
        with _RUNTIME_BUDGET_LOCK_INIT_LOCK:
            lock = getattr(self.agent, "runtime_budget_lock", None)
            if lock is None:
                lock = threading.Lock()
                setattr(self.agent, "runtime_budget_lock", lock)
            return lock

    def _bind_tool_lifecycle_context(self, tool, tool_call: "ToolCall"):
        bind = getattr(tool, "bind_lifecycle_context", None)
        if not callable(bind):
            return None
        context = {
            "session_run_id": str(getattr(self.agent, "current_session_id", "") or ""),
            "agent_run_id": runtime_agent_run_id(self.agent),
            "turn_id": str(getattr(self.agent, "runtime_turn_id", "") or ""),
            "tool_call_id": str(getattr(tool_call, "id", "") or ""),
            "tool_name": str(getattr(tool_call, "name", "") or ""),
            "mcp_server": str(getattr(tool, "server_name", "") or ""),
            "trigger_source": str(
                getattr(self.agent, "permission_trigger_source", "") or "chat"
            ),
        }
        if self._tool_source(tool) == "mcp":
            def _emit_bound_lifecycle_event(payload: dict) -> None:
                if not isinstance(payload, dict):
                    return
                event_payload = dict(payload)
                event_payload.setdefault("session_run_id", context["session_run_id"])
                event_payload.setdefault("agent_run_id", context["agent_run_id"])
                event_payload.setdefault("turn_id", context["turn_id"])
                event_payload.setdefault("tool_call_id", context["tool_call_id"])
                event_payload.setdefault("tool_name", context["tool_name"])
                event_payload.setdefault("mcp_server", context["mcp_server"])
                event_payload.setdefault("trigger_source", context["trigger_source"])
                self.agent._emit_event(AgentEvent.lifecycle_hook(event_payload))

            context["_agent_lifecycle_event_emitter"] = _emit_bound_lifecycle_event
        try:
            restore = bind(context)
        except Exception:
            return None
        return restore if callable(restore) else None

    def _consume_tool_call_budget(self) -> str | None:
        if limit_message := runtime_budget_limit_message(self.agent):
            return f"Error: {limit_message}"
        max_tool_calls = runtime_budget_int(self.agent, "max_tool_calls")
        if max_tool_calls is None:
            return None
        with self._budget_lock:
            try:
                current = int(getattr(self.agent, "runtime_tool_call_count", 0) or 0)
            except (TypeError, ValueError):
                current = 0
            if current >= max_tool_calls:
                return (
                    "Error: AgentRun budget exceeded: "
                    f"max_tool_calls={max_tool_calls}"
                )
            setattr(self.agent, "runtime_tool_call_count", current + 1)
        return None

    @staticmethod
    def _tool_source(tool: object | None) -> str:
        return getattr(tool, "tool_source", "builtin" if tool is not None else "unknown")

    @staticmethod
    def _enum_payload_value(value: object) -> str:
        text = getattr(value, "value", value)
        return str(text or "").strip()

    def _tool_event_metadata(self, tool: object | None) -> dict[str, Any]:
        if tool is None:
            return {}
        tool_spec = getattr(tool, "tool_spec", None)
        if not callable(tool_spec):
            return {}
        try:
            spec = tool_spec()
        except Exception:
            return {}
        metadata = getattr(spec, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        namespace = str(getattr(spec, "namespace", "") or "").strip()
        name = str(getattr(spec, "name", "") or "").strip()
        tool_id = str(metadata.get("tool_id") or "").strip()
        if not tool_id and namespace and name:
            tool_id = f"{namespace}:{name}"
        risk = self._enum_payload_value(getattr(spec, "risk", ""))
        exposure = self._enum_payload_value(getattr(spec, "exposure", ""))
        payload: dict[str, Any] = {}
        if tool_id:
            payload["tool_id"] = tool_id
        if risk:
            payload["risk"] = risk
        if exposure:
            payload["exposure"] = exposure
        return payload

    def _tool_event_metadata_for_call(
        self,
        tc: "ToolCall",
        tool: object | None,
    ) -> dict[str, Any]:
        metadata = self._tool_event_metadata(tool)
        target_context = self._capability_target_context(tc)
        if target_context is None:
            return metadata
        metadata.update(
            {
                "tool_id": target_context.target_tool_id,
                "risk": target_context.target_risk,
                "exposure": target_context.target_exposure,
                "capability_target": target_context.as_event_metadata(),
            }
        )
        return metadata

    def _record_tool_result_meta(
        self,
        tool_call: "ToolCall",
        key: str,
        value: dict[str, Any],
    ) -> None:
        if not isinstance(value, dict):
            return
        meta = self._tool_result_meta_by_call_identity.setdefault(id(tool_call), {})
        meta[key] = dict(value)

    def _consume_tool_result_meta(self, tool_call: "ToolCall") -> dict[str, Any] | None:
        return self._tool_result_meta_by_call_identity.pop(id(tool_call), None)

    def _resolve_model_tool(self, name: str) -> tuple[object | None, object | None, bool]:
        exposure_plan = getattr(self.agent, "tool_exposure_plan", None)
        route_plan = getattr(self.agent, "tool_route_plan", None)
        if callable(exposure_plan) and callable(route_plan):
            plan = exposure_plan()
            routes = route_plan()
            return (
                plan.get_model_callable_tool(name),
                routes.get_executor(name),
                True,
            )
        tool = self.agent.get_tool(name)
        if tool is None:
            tool = get_tool(name)
        return tool, tool, False

    def _return_tool_not_directly_exposed(
        self,
        tc: "ToolCall",
        *,
        tool: object | None,
        index: int | None = None,
    ) -> str:
        message = f"Error: tool '{tc.name}' is not directly exposed to the model"
        diagnostic = tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.PREFLIGHT,
            kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
            code="tool_not_directly_exposed",
            message=message,
            tool_name=tc.name,
            tool_call_id=tc.id,
        )
        context = self._tool_argument_context(tc, tool)
        self._record_lifecycle_diagnostics([diagnostic], context)
        self._emit_tool_end(
            tc,
            message,
            tool=tool,
            index=index,
            meta=self._diagnostics_meta([diagnostic]),
            emit_file_change=False,
        )
        return message

    def _execute_tool_search(self, tool_call: "ToolCall") -> str:
        arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        query = str(arguments.get("query") or "").strip()
        try:
            max_results = int(arguments.get("max_results") or 8)
        except (TypeError, ValueError):
            max_results = 8
        max_results = max(1, min(max_results, 20))
        route_plan = self.agent.tool_exposure_plan()
        terms = [item for item in re.split(r"\s+", query.lower()) if item]
        scored: list[tuple[int, str, object]] = []
        for entry in route_plan.deferred:
            score = self._tool_search_score(entry, terms)
            if terms and score <= 0:
                continue
            scored.append((score, entry.tool_id, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        entries = [entry for _score, _tool_id, entry in scored[:max_results]]
        discovered = getattr(self.agent, "_discovered_capability_tool_ids", None)
        if not isinstance(discovered, set):
            discovered = set()
        discovered.update(entry.tool_id for entry in entries)
        setattr(self.agent, "_discovered_capability_tool_ids", discovered)
        self._record_tool_result_meta(
            tool_call,
            "search_trace",
            {
                "query": query,
                "result_count": len(entries),
                "tool_ids": [entry.tool_id for entry in entries],
            },
        )
        return json.dumps(
            {
                "query": query,
                "results": [
                    self._tool_search_result_from_entry(entry) for entry in entries
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _tool_search_score(entry: object, terms: list[str]) -> int:
        spec = getattr(entry, "spec", None)
        haystack = "\n".join(
            str(part or "")
            for part in (
                getattr(entry, "tool_id", ""),
                getattr(entry, "name", ""),
                getattr(spec, "description", ""),
                getattr(spec, "search_text", ""),
                " ".join(getattr(spec, "search_keywords", ()) or ()),
            )
        ).lower()
        if not terms:
            return 1
        return sum(1 for term in terms if term in haystack)

    def _tool_search_result_from_entry(self, entry: object) -> dict:
        spec = entry.spec
        input_schema = dict(spec.input_schema)
        return {
            "tool_id": entry.tool_id,
            "call_via": "capability_execute",
            "name": spec.name,
            "description": spec.description,
            "input_schema": input_schema,
            "call_template": {
                "tool_id": entry.tool_id,
                "arguments": self._tool_search_call_template_arguments(input_schema),
            },
            "risk": spec.risk.value,
            "permission": {"policy": spec.permission.policy},
        }

    @classmethod
    def _tool_search_call_template_arguments(cls, schema: dict[str, Any]) -> dict:
        if schema.get("type") != "object":
            return {}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        required = schema.get("required")
        if not isinstance(required, list):
            return {}
        arguments: dict[str, Any] = {}
        for name in required:
            if not isinstance(name, str):
                continue
            property_schema = properties.get(name)
            if not isinstance(property_schema, dict):
                continue
            template = cls._tool_search_template_for_schema(property_schema)
            if template is not None:
                arguments[name] = template
        return arguments

    @classmethod
    def _tool_search_template_for_schema(cls, schema: dict[str, Any]) -> Any:
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and enum_values:
            return "<one of: " + " | ".join(str(value) for value in enum_values) + ">"
        schema_type = schema.get("type")
        if schema_type == "string":
            return "<string>"
        if schema_type in {"number", "integer"}:
            return 0
        if schema_type == "boolean":
            return False
        if schema_type == "array":
            item_schema = schema.get("items")
            if not isinstance(item_schema, dict):
                return []
            item_template = cls._tool_search_template_for_schema(item_schema)
            return [] if item_template is None else [item_template]
        if schema_type == "object":
            return cls._tool_search_call_template_arguments(schema)
        return None

    def _execute_capability_execute(
        self,
        tool_call: "ToolCall",
        *,
        index: int | None,
    ) -> str:
        arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        tool_id = str(arguments.get("tool_id") or "").strip()
        raw_target_arguments = arguments.get("arguments")
        if not tool_id:
            return "Error: capability_execute requires tool_id"
        if not isinstance(raw_target_arguments, dict):
            return "Error: capability_execute arguments must be an object"
        exposure_plan = self.agent.tool_exposure_plan()
        entry = exposure_plan.executor_routes_by_id.get(tool_id)
        if entry is None:
            return (
                f"Error: capability tool_id '{tool_id}' is not available "
                "in the active tool exposure plan"
            )
        if entry.spec.exposure != ToolExposure.DEFERRED:
            return f"Error: capability tool_id '{tool_id}' is not a deferred capability tool"
        route_plan = self.agent.tool_route_plan()
        target_tool = self._capability_target_executor(entry, route_plan)
        if target_tool is None:
            return f"Error: capability tool_id '{tool_id}' has no registered executor"
        target_name = str(entry.name or getattr(target_tool, "name", "") or "")
        target_tool_id = str(entry.tool_id)
        target_exposure = self._enum_payload_value(entry.spec.exposure)
        target_risk = self._enum_payload_value(entry.spec.risk)
        permission = getattr(entry.spec, "permission", None)
        target_permission_policy = str(getattr(permission, "policy", "") or "")
        nested_call_id = self._capability_target_call_id(tool_call, tool_id)
        target_arguments = dict(raw_target_arguments)
        target_context = CapabilityTargetContext(
            gateway_tool_name="capability_execute",
            parent_tool_call_id=str(tool_call.id or "capability_execute"),
            target_tool_call_id=nested_call_id,
            target_tool_id=target_tool_id,
            target_tool_name=target_name,
            target_arguments=target_arguments,
            target_exposure=target_exposure,
            target_risk=target_risk,
            target_permission_policy=target_permission_policy,
        )
        self._capability_target_context_by_call_id[nested_call_id] = target_context
        self._record_tool_result_meta(
            tool_call,
            "execute_trace",
            target_context.as_execute_trace(),
        )
        nested_call = ToolCall(
            id=nested_call_id,
            name=target_name,
            arguments=target_arguments,
        )
        try:
            return self._execute_resolved_tool(nested_call, target_tool, index=index)
        finally:
            self._capability_target_context_by_call_id.pop(nested_call_id, None)

    @staticmethod
    def _capability_target_call_id(tool_call: "ToolCall", tool_id: str) -> str:
        return f"{tool_call.id or 'capability_execute'}:{tool_id}"

    def _tool_permission_policy(self, tool: object | None) -> str:
        if tool is None:
            return ""
        tool_spec = getattr(tool, "tool_spec", None)
        if not callable(tool_spec):
            return ""
        try:
            spec = tool_spec()
        except Exception:
            return ""
        permission = getattr(spec, "permission", None)
        return str(getattr(permission, "policy", "") or "")

    def _capability_target_executor(
        self,
        entry: object,
        route_plan: object,
    ) -> object | None:
        tool = entry.tool
        if str(getattr(tool, "tool_source", "") or "") != "capability":
            return tool
        refs = self._capability_target_refs(entry)
        for ref in refs:
            target_entry = route_plan.executor_routes_by_id.get(ref)
            if target_entry is not None and target_entry.tool is not tool:
                return target_entry.tool
            target_name = self._tool_name_from_capability_target_ref(ref)
            if target_name:
                target_tool = route_plan.get_executor(target_name)
                if target_tool is not None and target_tool is not tool:
                    return target_tool
                target_tool = get_tool(target_name)
                if target_tool is not None:
                    return target_tool
        return None

    @staticmethod
    def _capability_target_refs(entry: object) -> list[str]:
        spec = entry.spec
        metadata = dict(getattr(spec, "metadata", {}) or {})
        refs = [
            metadata.get("target_tool_ref"),
            metadata.get("executor_ref"),
            getattr(spec.execution, "executor_ref", ""),
        ]
        return [
            str(ref).strip()
            for ref in refs
            if str(ref or "").strip()
        ]

    @staticmethod
    def _tool_name_from_capability_target_ref(ref: str) -> str:
        text = str(ref or "").strip()
        if ":" not in text:
            return ""
        prefix, name = text.split(":", 1)
        if prefix in {"builtin", "builtin_tool", "tool"}:
            return name.strip()
        return ""

    def _emit_tool_end(
        self,
        tc: "ToolCall",
        result: str,
        *,
        tool: object | None = None,
        index: int | None = None,
        meta: dict | None = None,
        emit_file_change: bool = True,
    ) -> None:
        if emit_file_change:
            self._emit_apply_patch_file_change_completed(
                tc,
                result,
                tool=tool,
                index=index,
                meta=meta,
            )
        tool_metadata = self._tool_event_metadata_for_call(tc, tool)
        self.agent._emit_event(
            AgentEvent.tool_call_end(
                tc.name,
                result,
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
                meta=meta,
                tool_metadata=tool_metadata,
            )
        )

    def _emit_tool_start(
        self,
        tc: "ToolCall",
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> None:
        self._emit_apply_patch_file_change_started(tc, tool=tool, index=index)
        tool_metadata = self._tool_event_metadata_for_call(tc, tool)
        self.agent._emit_event(
            AgentEvent.tool_call_start(
                tc.name,
                dict(tc.arguments or {}),
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
                tool_metadata=tool_metadata,
            )
        )

    def _capability_target_event_metadata(self, tc: "ToolCall") -> dict[str, Any]:
        context = self._capability_target_context(tc)
        if context is None:
            return {}
        return {"capability_target": context.as_event_metadata()}

    def _capability_target_diagnostic_metadata(
        self,
        tc: "ToolCall",
    ) -> dict[str, Any]:
        return self._capability_target_event_metadata(tc)

    def _capability_target_context(
        self,
        tc: "ToolCall",
    ) -> CapabilityTargetContext | None:
        return self._capability_target_context_by_call_id.get(str(tc.id or ""))

    def _sync_capability_target_arguments(
        self,
        tc: "ToolCall",
        arguments: dict[str, Any],
    ) -> None:
        context = self._capability_target_context(tc)
        if context is None:
            return
        self._capability_target_context_by_call_id[str(tc.id or "")] = replace(
            context,
            target_arguments=dict(arguments),
        )

    def _emit_capability_target_start_if_needed(
        self,
        tc: "ToolCall",
        tool: object | None,
        *,
        index: int | None,
        already_emitted: bool,
    ) -> bool:
        if already_emitted or self._capability_target_context(tc) is None:
            return already_emitted
        self._emit_tool_start(tc, tool=tool, index=index)
        return True

    def _bad_arguments_message_for_call(
        self,
        tool_call: "ToolCall",
        tool: object | None,
        detail: str,
    ) -> str:
        context = self._capability_target_context(tool_call)
        if context is None:
            return self._bad_arguments_message(tool_call.name, detail)
        schema = getattr(tool, "parameters", None)
        template = {
            "tool_id": context.target_tool_id,
            "arguments": self._tool_search_call_template_arguments(
                schema if isinstance(schema, dict) else {}
            ),
        }
        return "\n".join(
            [
                f"Error: bad arguments for target tool {context.target_tool_name}",
                f"tool_id: {context.target_tool_id}",
                "",
                detail,
                "",
                "Retry by calling capability_execute with:",
                json.dumps(template, ensure_ascii=False, indent=2),
            ]
        )

    def _preview_failure_message_for_call(
        self,
        tool_call: "ToolCall",
        detail: str,
    ) -> str:
        context = self._capability_target_context(tool_call)
        if context is None:
            return f"Error: {detail or 'apply_patch semantic preview failed'}"
        return "\n".join(
            [
                f"Error: target tool '{context.target_tool_name}' semantic preview failed",
                f"tool_id: {context.target_tool_id}",
                f"Reason: {detail or 'apply_patch semantic preview failed'}",
            ]
        )

    def _preflight_error_message_for_call(
        self,
        tool_call: "ToolCall",
        tool: object | None,
        detail: str,
    ) -> str:
        context = self._capability_target_context(tool_call)
        if context is None:
            return detail
        schema = getattr(tool, "parameters", None)
        template = {
            "tool_id": context.target_tool_id,
            "arguments": self._tool_search_call_template_arguments(
                schema if isinstance(schema, dict) else {}
            ),
        }
        return "\n".join(
            [
                f"Error: preflight failed for target tool {context.target_tool_name}",
                f"tool_id: {context.target_tool_id}",
                "",
                detail,
                "",
                "Retry by calling capability_execute with:",
                json.dumps(template, ensure_ascii=False, indent=2),
            ]
        )

    def _emit_tool_arguments_complete(
        self,
        tc: "ToolCall",
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> None:
        self.agent._emit_event(
            AgentEvent.tool_arguments_complete(
                tc.name,
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
            )
        )

    def _emit_tool_arguments_valid(
        self,
        tc: "ToolCall",
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> None:
        self.agent._emit_event(
            AgentEvent.tool_arguments_valid(
                tc.name,
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
            )
        )

    def _emit_tool_arguments_invalid(
        self,
        tc: "ToolCall",
        message: str,
        *,
        tool: object | None = None,
        index: int | None = None,
        code: str | None = None,
        retry_hint: str | None = None,
    ) -> None:
        self.agent._emit_event(
            AgentEvent.tool_arguments_invalid(
                tc.name,
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
                message=message,
                code=code,
                retry_hint=retry_hint,
            )
        )

    def _file_change_item_id(self, tc: "ToolCall", index: int | None = None) -> str:
        stable = tc.id or (
            f"index-{index}:{self._tool_invocation_cache_key(tc)}:args-{self._tool_args_hash(tc.arguments)}"
            if index is not None
            else f"pending:{self._tool_invocation_cache_key(tc)}:args-{self._tool_args_hash(tc.arguments)}"
        )
        return f"file-change:{stable}"

    @staticmethod
    def _apply_patch_preview_required(
        tc: "ToolCall",
        tool: object | None = None,
    ) -> bool:
        if tc.name != "apply_patch":
            return False
        arguments = tc.arguments if isinstance(tc.arguments, dict) else {}
        if isinstance(arguments.get("patch"), str):
            return True
        parameters = getattr(tool, "parameters", None)
        if not isinstance(parameters, dict):
            return False
        properties = parameters.get("properties")
        return isinstance(properties, dict) and "patch" in properties

    def _emit_apply_patch_file_change_started(
        self,
        tc: "ToolCall",
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> str | None:
        if not self._apply_patch_preview_required(tc, tool):
            return None
        item_id = self._file_change_item_id(tc, index)
        if item_id in self._file_change_started_item_ids:
            return item_id
        outcome = self._ensure_apply_patch_preview_outcome(tc, tool=tool, index=index)
        if outcome is None or not outcome.ready:
            return None
        changes = outcome.changes
        self._file_change_started_item_ids.add(item_id)
        self._file_change_changes_by_item_id[item_id] = changes
        self.agent._emit_event(
            AgentEvent.file_change_started(
                item_id=item_id,
                tool_call_id=tc.id,
                changes=changes,
                tool_metadata=self._tool_event_metadata_for_call(tc, tool),
            )
        )
        return item_id

    def _emit_apply_patch_file_change_completed(
        self,
        tc: "ToolCall",
        result: str,
        *,
        tool: object | None = None,
        index: int | None = None,
        meta: dict | None = None,
    ) -> None:
        if tc.name != "apply_patch":
            return
        item_id = self._emit_apply_patch_file_change_started(
            tc,
            tool=tool,
            index=index,
        )
        if item_id is None:
            return
        failure_kind = str((meta or {}).get("failure_kind") or "")
        text_result = str(result or "")
        if failure_kind == ToolFailureKind.APPROVAL_DENIED.value or _meta_has_approval_denied(meta):
            status = "declined"
        elif text_result.startswith("Error"):
            status = "failed"
        else:
            status = "completed"
        self.agent._emit_event(
            AgentEvent.file_change_completed(
                item_id=item_id,
                tool_call_id=tc.id,
                changes=self._file_change_changes_by_item_id.get(item_id, []),
                status=status,
                error=text_result if status in {"failed", "declined"} else None,
                tool_metadata=self._tool_event_metadata_for_call(tc, tool),
            )
        )

    def _emit_apply_patch_approval_requested(
        self,
        tc: "ToolCall",
        *,
        reason: str | None,
        tool: object | None = None,
        index: int | None = None,
    ) -> str | None:
        item_id = self._emit_apply_patch_file_change_started(
            tc,
            tool=tool,
            index=index,
        )
        if item_id is None:
            return None
        approval_id = f"approval:{tc.id or item_id}"
        self.agent._emit_event(
            AgentEvent.file_change_approval_requested(
                item_id=item_id,
                approval_id=approval_id,
                tool_call_id=tc.id,
                reason=reason or "",
                tool_metadata=self._tool_event_metadata_for_call(tc, tool),
            )
        )
        return approval_id

    def _emit_apply_patch_approval_resolved(
        self,
        tc: "ToolCall",
        *,
        approval_id: str | None,
        decision: str,
        reason: str | None,
        tool: object | None = None,
        index: int | None = None,
    ) -> None:
        item_id = self._emit_apply_patch_file_change_started(
            tc,
            tool=tool,
            index=index,
        )
        if item_id is None:
            return
        self.agent._emit_event(
            AgentEvent.file_change_approval_resolved(
                item_id=item_id,
                approval_id=approval_id or f"approval:{tc.id or item_id}",
                decision=decision,
                tool_call_id=tc.id,
                reason=reason or "",
                tool_metadata=self._tool_event_metadata_for_call(tc, tool),
            )
        )

    def _capability_target_preview_payloads(
        self,
        tc: "ToolCall",
        preview_identity: dict,
        approved_save_candidate: dict,
    ) -> tuple[dict, dict]:
        context = self._capability_target_context(tc)
        if context is None:
            return preview_identity, approved_save_candidate
        target_payload = context.as_event_metadata()
        target_preview_identity = dict(preview_identity)
        target_preview_identity["capability_target"] = target_payload
        target_candidate = dict(approved_save_candidate)
        target_candidate["preview_identity"] = target_preview_identity
        target_candidate["capability_target"] = target_payload
        return target_preview_identity, target_candidate

    def _ensure_apply_patch_preview_outcome(
        self,
        tc: "ToolCall",
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> _MutationPreviewOutcome | None:
        if not self._apply_patch_preview_required(tc, tool):
            return None
        item_id = self._file_change_item_id(tc, index)
        cache_key = self._mutation_preview_cache_key(tc, tool=tool, index=index)
        existing = self._mutation_preview_outcomes_by_item_id.get(cache_key)
        if existing is not None:
            return existing
        self.agent._emit_event(
            AgentEvent.mutation_previewing(
                tc.name,
                item_id=item_id,
                tool_call_id=tc.id,
                index=index,
                tool_metadata=self._tool_event_metadata_for_call(tc, tool),
            )
        )
        result = self._preview_apply_patch_result(tc)
        if str(getattr(result, "status", "") or "") == "failed" or getattr(result, "error", None):
            error = str(
                getattr(result, "error", None)
                or getattr(result, "message", None)
                or "apply_patch semantic preview failed"
            )
            outcome = _MutationPreviewOutcome(
                status="failed",
                tool_name=tc.name,
                tool_call_id=tc.id,
                item_id=item_id,
                error=error,
                failure_code="semantic_preview_failed",
                retry_hint=self._apply_patch_semantic_retry_hint(error),
            )
            self._mutation_preview_outcomes_by_item_id[cache_key] = outcome
            self.agent._emit_event(
                AgentEvent.mutation_preview_failed(
                    tc.name,
                    item_id=item_id,
                    tool_call_id=tc.id,
                    error=error,
                    failure_code=outcome.failure_code,
                    retry_hint=outcome.retry_hint,
                    index=index,
                    tool_metadata=self._tool_event_metadata_for_call(tc, tool),
                )
            )
            return outcome
        changes = [change.to_dict() for change in getattr(result, "changes", ()) or ()]
        preview_identity = self._preview_identity_from_preview_result(result)
        approved_save_candidate = self._approved_save_candidate_from_preview_result(
            result
        )
        preview_identity, approved_save_candidate = self._capability_target_preview_payloads(
            tc,
            preview_identity,
            approved_save_candidate,
        )
        missing_save_candidate = self._missing_mutation_save_candidate_fields(
            preview_identity,
            approved_save_candidate,
        )
        if missing_save_candidate:
            error = (
                "apply_patch preview missing required approved_save_candidate fields: "
                + ", ".join(missing_save_candidate)
            )
            outcome = _MutationPreviewOutcome(
                status="failed",
                tool_name=tc.name,
                tool_call_id=tc.id,
                item_id=item_id,
                error=error,
                failure_code="preview_save_candidate_missing",
                retry_hint=(
                    "Build a fresh apply_patch preview that includes preview_identity "
                    "and approved_save_candidate before approval or execution."
                ),
            )
            self._mutation_preview_outcomes_by_item_id[cache_key] = outcome
            self.agent._emit_event(
                AgentEvent.mutation_preview_failed(
                    tc.name,
                    item_id=item_id,
                    tool_call_id=tc.id,
                    error=error,
                    failure_code=outcome.failure_code,
                    retry_hint=outcome.retry_hint,
                    index=index,
                    tool_metadata=self._tool_event_metadata_for_call(tc, tool),
                )
            )
            return outcome
        outcome = _MutationPreviewOutcome(
            status="ready",
            tool_name=tc.name,
            tool_call_id=tc.id,
            item_id=item_id,
            changes=changes,
            diff=str(getattr(result, "diff", "") or ""),
            plan_id=getattr(result, "plan_id", None),
            preview_identity=preview_identity,
            approved_save_candidate=approved_save_candidate,
        )
        self._mutation_preview_outcomes_by_item_id[cache_key] = outcome
        self.agent._emit_event(
            AgentEvent.mutation_preview_ready(
                tc.name,
                item_id=item_id,
                tool_call_id=tc.id,
                changes=changes,
                index=index,
                tool_metadata=self._tool_event_metadata_for_call(tc, tool),
            )
        )
        return outcome

    def _mutation_preview_cache_key(
        self,
        tc: "ToolCall",
        *,
        tool: object | None,
        index: int | None,
    ) -> str:
        backend = getattr(self.agent, "workspace_mutation_backend", None) or getattr(
            tool, "backend", None
        )
        context = getattr(backend, "context", None)
        identity = {
            "tool_name": tc.name,
            "args_hash": self._tool_args_hash(tc.arguments),
            "invocation_id": self._tool_invocation_cache_key(tc),
            "tool_call_id": tc.id or "",
            "index": index,
            "workspace_id": str(
                getattr(backend, "workspace_id", "")
                or runtime_workspace_root(self.agent)
                or runtime_working_directory(self.agent)
                or ""
            ),
            "execution_target": str(
                getattr(backend, "execution_target", "")
                or getattr(context, "execution_target", "")
                or "local"
            ),
            "path_space": str(getattr(backend, "path_space", "") or ""),
        }
        return self._stable_identity_hash(identity)

    def _tool_invocation_cache_key(self, tc: "ToolCall") -> str:
        if tc.id:
            return str(tc.id)
        existing = getattr(tc, "_labrastro_invocation_cache_key", None)
        if isinstance(existing, str) and existing:
            return existing
        self._anonymous_tool_invocation_seq += 1
        value = f"anonymous:{self._anonymous_tool_invocation_seq}"
        try:
            setattr(tc, "_labrastro_invocation_cache_key", value)
        except Exception:
            return f"{value}:object:{id(tc):x}"
        return value

    @classmethod
    def _tool_args_hash(cls, args: object) -> str:
        return cls._stable_identity_hash(args if isinstance(args, dict) else {})

    @staticmethod
    def _stable_identity_hash(value: object) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _preview_identity_from_preview_result(result) -> dict:
        raw = getattr(result, "preview_identity", None)
        if isinstance(raw, dict) and raw:
            return dict(raw)
        candidate = getattr(result, "approved_save_candidate", None)
        if isinstance(candidate, dict) and isinstance(candidate.get("preview_identity"), dict):
            return dict(candidate["preview_identity"])
        return {}

    @staticmethod
    def _approved_save_candidate_from_preview_result(result) -> dict:
        raw = getattr(result, "approved_save_candidate", None)
        if isinstance(raw, dict) and raw:
            return dict(raw)
        return {}

    @staticmethod
    def _missing_mutation_save_candidate_fields(
        preview_identity: dict,
        approved_save_candidate: dict,
    ) -> list[str]:
        missing: list[str] = []
        if not isinstance(preview_identity, dict) or not preview_identity:
            missing.append("preview_identity")
        else:
            for key in (
                "plan_id",
                "candidate_hash",
                "tool_name",
                "workspace_id",
                "execution_target",
                "path_space",
                "args_hash",
            ):
                if not str(preview_identity.get(key) or "").strip():
                    missing.append(f"preview_identity.{key}")
        if not isinstance(approved_save_candidate, dict) or not approved_save_candidate:
            missing.append("approved_save_candidate")
        elif approved_save_candidate.get("preview_identity") != preview_identity:
            missing.append("approved_save_candidate.preview_identity")
        operations = (
            approved_save_candidate.get("operations")
            if isinstance(approved_save_candidate, dict)
            else None
        )
        if not isinstance(operations, list) or not operations:
            missing.append("approved_save_candidate.operations")
        return missing

    @staticmethod
    def _apply_patch_semantic_retry_hint(error: str) -> str:
        return (
            "Fix the patch so it applies to the current workspace state, then retry "
            "with the same apply_patch grammar. Do not use *** File:, *** Action:, "
            "unified diff headers, shell writes, or a parallel file mutation protocol."
        )

    def _preview_apply_patch_result(self, tc: "ToolCall"):
        patch = tc.arguments.get("patch") if isinstance(tc.arguments, dict) else None
        if not isinstance(patch, str) or not patch.strip():
            return LocalWorkspaceMutationBackend(None).preview_text_patch("")
        mutation_backend = getattr(self.agent, "workspace_mutation_backend", None)
        preview_text_patch = getattr(mutation_backend, "preview_text_patch", None)
        if callable(preview_text_patch):
            return preview_text_patch(patch)
        workspace_root = (
            runtime_workspace_root(self.agent)
            or runtime_working_directory(self.agent)
        )
        return LocalWorkspaceMutationBackend(workspace_root).preview_text_patch(patch)

    @staticmethod
    def _should_save_approved_mutation_candidate(
        tc: "ToolCall",
        tool: object | None,
        preview_outcome: _MutationPreviewOutcome | None,
    ) -> bool:
        return (
            tc.name == "apply_patch"
            and preview_outcome is not None
            and preview_outcome.ready
            and bool(preview_outcome.approved_save_candidate)
            and bool(getattr(tool, "uses_workspace_mutation_candidate", False))
        )

    def _save_approved_mutation_candidate(
        self,
        tc: "ToolCall",
        tool: object | None,
        preview_outcome: _MutationPreviewOutcome | None,
    ) -> str:
        if preview_outcome is None or not preview_outcome.approved_save_candidate:
            return "Error: apply_patch approved_save_candidate is required"
        candidate = dict(preview_outcome.approved_save_candidate)
        save_backend = self._workspace_mutation_save_backend(tool)
        save_candidate = getattr(save_backend, "save_candidate", None)
        if not callable(save_candidate):
            return "Error: workspace mutation backend does not support save_candidate"
        result = save_candidate(candidate)
        return self._format_file_mutation_result(result)

    def _workspace_mutation_save_backend(self, tool: object | None):
        seen: set[int] = set()
        for backend in (
            getattr(self.agent, "workspace_mutation_backend", None),
            getattr(tool, "backend", None),
        ):
            if backend is None:
                continue
            backend_id = id(backend)
            if backend_id in seen:
                continue
            seen.add(backend_id)
            if callable(getattr(backend, "save_candidate", None)):
                return backend
        workspace_root = (
            runtime_workspace_root(self.agent)
            or runtime_working_directory(self.agent)
        )
        return LocalWorkspaceMutationBackend(workspace_root)

    @staticmethod
    def _format_file_mutation_result(result) -> str:
        ok = bool(getattr(result, "ok", False))
        message = str(getattr(result, "message", "") or "")
        diff = str(getattr(result, "diff", "") or "")
        error = getattr(result, "error", None)
        if not ok:
            return message or f"Error: {error or 'workspace mutation save failed'}"
        if diff:
            return f"{message}\n{diff}" if message else diff
        return message or "Applied patch"

    @staticmethod
    def _sync_tool_call(target: "ToolCall", source: "ToolCall") -> "ToolCall":
        target.name = source.name
        target.arguments = dict(source.arguments or {})
        target.argument_error = source.argument_error
        target.argument_diagnostics = list(source.argument_diagnostics)
        return target

    @staticmethod
    def _budget_diagnostic(tc: "ToolCall", message: str) -> ToolDiagnostic:
        return tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.EXECUTION,
            kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
            code="runtime_budget_exceeded",
            message=message,
            tool_name=tc.name,
            tool_call_id=tc.id,
        )

    def _return_budget_error(
        self,
        tc: "ToolCall",
        message: str,
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> str:
        diagnostic = self._budget_diagnostic(tc, message)
        context = self._tool_argument_context(tc, tool)
        self._record_lifecycle_diagnostics([diagnostic], context)
        self._emit_tool_end(
            tc,
            message,
            tool=tool,
            index=index,
            meta=self._diagnostics_meta([diagnostic]),
            emit_file_change=False,
        )
        return message

    @staticmethod
    def _bad_arguments_message(tool_name: str, detail: str) -> str:
        return f"Error: bad arguments for {tool_name}: {detail}"

    @staticmethod
    def _diagnostics_meta(diagnostics: list[ToolDiagnostic | dict] | None) -> dict | None:
        if not diagnostics:
            return None
        return {"tool_diagnostics": [diagnostic_to_dict(item) for item in diagnostics]}

    def _diagnostics_with_capability_target(
        self,
        tool_call: "ToolCall",
        diagnostics: list[ToolDiagnostic | dict],
    ) -> list[dict]:
        target_metadata = self._capability_target_diagnostic_metadata(tool_call)
        if not target_metadata:
            return [diagnostic_to_dict(item) for item in diagnostics]
        enriched: list[dict] = []
        for item in diagnostics:
            payload = diagnostic_to_dict(item)
            metadata = dict(payload.get("metadata") or {})
            metadata.update(target_metadata)
            payload["metadata"] = metadata
            enriched.append(payload)
        return enriched

    @classmethod
    def _validation_meta(
        cls,
        validation: object | None,
        *,
        tool_call_id: str | None = None,
    ) -> dict | None:
        if validation is None:
            return None
        return cls._diagnostics_meta(
            diagnostics_from_argument_validation(
                validation,
                tool_call_id=tool_call_id,
            )
        )

    @staticmethod
    def _merge_meta(*items: dict | None) -> dict | None:
        merged: dict = {}
        for item in items:
            if item:
                diagnostics = item.get("tool_diagnostics")
                if isinstance(diagnostics, list):
                    merged.setdefault("tool_diagnostics", [])
                    merged["tool_diagnostics"].extend(diagnostics)
                for key, value in item.items():
                    if key == "tool_diagnostics":
                        continue
                    merged[key] = value
        return merged or None

    def _tool_argument_context(self, tc: "ToolCall", tool: object | None) -> dict:
        llm = getattr(self.agent, "llm", None)
        provider_config = getattr(llm, "provider_config", None)
        context = {
            "session_id": getattr(self.agent, "current_session_id", None),
            "round_index": getattr(self.agent.state, "current_round", None),
            "tool": tc.name,
            "tool_call_id": tc.id,
            "tool_source": self._tool_source(tool),
            "mcp_server": getattr(tool, "server_name", None),
            "provider_id": getattr(llm, "provider_id", None),
            "provider_type": getattr(llm, "provider_type", None),
            "compat": getattr(provider_config, "compat", None),
            "model": getattr(llm, "model", None),
        }
        target_context = self._capability_target_context(tc)
        if target_context is not None:
            context["capability_target"] = target_context.as_event_metadata()
        return context

    def _evaluate_permission(
        self,
        tc: "ToolCall",
        tool: object | None,
    ) -> PermissionDecision | None:
        evaluator = getattr(self.agent, "evaluate_tool_permission", None)
        if not callable(evaluator):
            return None
        permission_tool = tool
        if permission_tool is None:
            permission_tool = type(
                "_PermissionTool",
                (),
                {"name": tc.name, "tool_source": "unknown"},
            )()
        return evaluator(permission_tool, tool_call=tc)

    def _permission_context_payload(
        self,
        decision: PermissionDecision,
    ) -> dict:
        audit = dict(decision.audit or {})
        return {
            "agent_id": audit.get("agent_id", ""),
            "source": audit.get("source", ""),
            "interactive": audit.get("interactive", False),
            "target": {
                "kind": audit.get("target_kind", ""),
                "name": audit.get("target_name", ""),
                "tool_source": audit.get("tool_source", ""),
                "mcp_server": audit.get("mcp_server", ""),
            },
            "decision": decision.to_dict(),
        }

    def _permission_block_message(
        self,
        decision: PermissionDecision,
        tool_name: str,
    ) -> str:
        reason = decision.reason or "blocked by permission gateway"
        if decision.action == PermissionAction.BLOCKED_REVIEW:
            message = f"Error: tool '{tool_name}' blocked pending review: {reason}"
        else:
            message = f"Error: tool '{tool_name}' denied by permission gateway: {reason}"
        feedback = self._permission_denied_lifecycle_feedback(decision)
        return f"{message}{feedback}"

    def _permission_block_message_for_call(
        self,
        decision: PermissionDecision,
        tool_call: "ToolCall",
    ) -> str:
        context = self._capability_target_context(tool_call)
        if context is None:
            return self._permission_block_message(decision, tool_call.name)
        reason = decision.reason or "blocked by permission gateway"
        if decision.action == PermissionAction.BLOCKED_REVIEW:
            message = (
                f"Error: target tool '{context.target_tool_name}' blocked pending review\n"
                f"tool_id: {context.target_tool_id}\n"
                f"Reason: {reason}"
            )
        else:
            message = (
                f"Error: target tool '{context.target_tool_name}' denied by permission gateway\n"
                f"tool_id: {context.target_tool_id}\n"
                f"Reason: {reason}"
            )
        feedback = self._permission_denied_lifecycle_feedback(decision)
        return f"{message}{feedback}"

    @staticmethod
    def _permission_denied_lifecycle_feedback(decision: PermissionDecision) -> str:
        audit = decision.audit if isinstance(decision.audit, dict) else {}
        outputs = audit.get("permission_denied_lifecycle")
        if not isinstance(outputs, list):
            return ""
        messages: list[str] = []
        seen: set[str] = set()
        for item in outputs:
            if not isinstance(item, dict):
                continue
            message = str(item.get("user_message") or item.get("reason") or "").strip()
            if not message or message in seen:
                continue
            seen.add(message)
            messages.append(message)
        if not messages:
            return ""
        if len(messages) == 1:
            return f"\nPermission feedback: {messages[0]}"
        return "\nPermission feedback:\n" + "\n".join(f"- {message}" for message in messages)

    @staticmethod
    def _approval_payload_args(arguments: dict) -> tuple[dict, str | None]:
        payload_args = dict(arguments or {})
        intent = payload_args.pop("intent", None)
        return payload_args, intent.strip() if isinstance(intent, str) and intent.strip() else None

    def _return_permission_block(
        self,
        tc: "ToolCall",
        tool: object | None,
        decision: PermissionDecision,
        *,
        index: int | None = None,
        validation_meta: dict | None = None,
    ) -> str:
        message = self._permission_block_message_for_call(decision, tc)
        metadata = {"permission": decision.to_dict()}
        target_context = self._capability_target_context(tc)
        if target_context is not None:
            metadata["capability_target"] = target_context.as_event_metadata()
        diagnostic = tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.PREFLIGHT,
            kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
            code=f"permission_{decision.action.value}",
            message=message,
            tool_name=tc.name,
            tool_call_id=tc.id,
            metadata=metadata,
        )
        context = self._tool_argument_context(tc, tool)
        self._record_lifecycle_diagnostics([diagnostic], context)
        self._emit_tool_end(
            tc,
            message,
            tool=tool,
            index=index,
            meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            emit_file_change=False,
        )
        return message

    def _request_permission_approval(
        self,
        tc: "ToolCall",
        tool: object | None,
        decision: PermissionDecision,
        validation_context: dict,
        validation_meta: dict | None,
        *,
        preview_outcome: _MutationPreviewOutcome | None = None,
        index: int | None = None,
    ) -> str | None:
        provider = self.agent.approval_provider
        if provider is None:
            message = (
                decision.reason
                or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
            )
            self._emit_apply_patch_approval_resolved(
                tc,
                approval_id=self._emit_apply_patch_approval_requested(
                    tc,
                    reason=decision.reason,
                    tool=tool,
                    index=index,
                ),
                decision="deny_once",
                reason=message,
                tool=tool,
                index=index,
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.APPROVAL,
                kind=ToolDiagnosticKind.APPROVAL_DENIED,
                code="approval_provider_missing",
                message=message,
                tool_name=tc.name,
                tool_call_id=tc.id,
                metadata=self._capability_target_diagnostic_metadata(tc),
            )
            self._record_lifecycle_diagnostics([diagnostic], validation_context)
            self._emit_tool_end(
                tc,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        try:
            approval_tool_args, approval_intent = self._approval_payload_args(tc.arguments)
            file_change_approval_id = self._emit_apply_patch_approval_requested(
                tc,
                reason=decision.reason,
                tool=tool,
                index=index,
            )
            approval_metadata = {
                "tool_call_id": tc.id,
                "permission": decision.to_dict(),
            }
            approval_metadata.update(self._capability_target_event_metadata(tc))
            if preview_outcome is not None and preview_outcome.ready:
                approval_metadata["preview_identity"] = dict(
                    preview_outcome.preview_identity
                )
                approval_metadata["approved_save_candidate"] = dict(
                    preview_outcome.approved_save_candidate
                )
            decision_result = provider.request_approval(
                ApprovalRequest(
                    tool_name=tc.name,
                    tool_args=approval_tool_args,
                    tool_source=getattr(tool, "tool_source", "builtin_tool")
                    if tool is not None
                    else "unknown",
                    reason=decision.reason,
                    intent=approval_intent,
                    metadata=approval_metadata,
                )
            )
        except (KeyboardInterrupt, EOFError):
            message = f"Tool '{tc.name}' approval interrupted by user"
            self._emit_apply_patch_approval_resolved(
                tc,
                approval_id=locals().get("file_change_approval_id"),
                decision="deny_once",
                reason=message,
                tool=tool,
                index=index,
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.APPROVAL,
                kind=ToolDiagnosticKind.APPROVAL_DENIED,
                code="approval_interrupted",
                message=message,
                tool_name=tc.name,
                tool_call_id=tc.id,
                metadata=self._capability_target_diagnostic_metadata(tc),
            )
            self._record_lifecycle_diagnostics([diagnostic], validation_context)
            self._emit_tool_end(
                tc,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        self._emit_apply_patch_approval_resolved(
            tc,
            approval_id=file_change_approval_id,
            decision=decision_result.mode,
            reason=decision_result.reason,
            tool=tool,
            index=index,
        )
        if decision_result.approved:
            confirmed_candidate = self._confirmed_approved_save_candidate(
                decision_result,
                preview_outcome,
            )
            if confirmed_candidate is not None and preview_outcome is not None:
                (
                    preview_outcome.preview_identity,
                    preview_outcome.approved_save_candidate,
                ) = self._capability_target_preview_payloads(
                    tc,
                    preview_outcome.preview_identity,
                    confirmed_candidate,
                )
            return None
        message = decision_result.reason or f"Tool '{tc.name}' denied by approval provider"
        decision_diagnostics = [
            diagnostic_to_dict(item)
            for item in decision_result.meta.get("tool_diagnostics", [])
            if isinstance(item, (ToolDiagnostic, dict))
        ]
        if not decision_diagnostics:
            decision_diagnostics = [
                tool_diagnostic_from_failure(
                    stage=ToolDiagnosticStage.APPROVAL,
                    kind=ToolDiagnosticKind.APPROVAL_DENIED,
                    code="approval_denied",
                    message=message,
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    metadata=self._capability_target_diagnostic_metadata(tc),
                ).to_dict()
            ]
        decision_diagnostics = self._diagnostics_with_capability_target(
            tc,
            decision_diagnostics,
        )
        self._record_lifecycle_diagnostics(decision_diagnostics, validation_context)
        failure_meta = {
            "failure_kind": decision_result.meta.get(
                "failure_kind", ToolFailureKind.APPROVAL_DENIED.value
            ),
            **decision_result.meta,
            "tool_diagnostics": decision_diagnostics,
        }
        self._emit_tool_end(
            tc,
            message,
            tool=tool,
            index=index,
            meta=self._merge_meta(validation_meta, failure_meta),
        )
        return message

    @staticmethod
    def _confirmed_approved_save_candidate(
        decision_result,
        preview_outcome: _MutationPreviewOutcome | None,
    ) -> dict | None:
        decision_meta = getattr(decision_result, "meta", None)
        if isinstance(decision_meta, dict):
            raw_candidate = decision_meta.get("approved_save_candidate")
            if not isinstance(raw_candidate, dict) or not raw_candidate:
                raw_candidate = decision_meta.get("save_candidate")
            if isinstance(raw_candidate, dict) and raw_candidate:
                return dict(raw_candidate)
        if preview_outcome is not None and preview_outcome.approved_save_candidate:
            return dict(preview_outcome.approved_save_candidate)
        return None

    def _request_lifecycle_pre_tool_approval(
        self,
        tc: "ToolCall",
        tool: object | None,
        message: str,
        validation_context: dict,
        validation_meta: dict | None,
        *,
        lifecycle_hooks: list[dict[str, str]] | None = None,
        index: int | None = None,
    ) -> str | None:
        if not bool(getattr(self.agent, "permission_interactive", True)):
            decision = PermissionDecision(
                action=PermissionAction.BLOCKED_REVIEW,
                authorized=False,
                reason=message,
                audit={
                    "lifecycle_event": "PreToolUse",
                    "lifecycle_hooks": list(lifecycle_hooks or []),
                },
            )
            return self._return_permission_block(
                tc,
                tool,
                decision,
                index=index,
                validation_meta=validation_meta,
            )

        provider = getattr(self.agent, "approval_provider", None)
        request_approval = getattr(provider, "request_approval", None)
        if not callable(request_approval):
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.APPROVAL,
                kind=ToolDiagnosticKind.APPROVAL_DENIED,
                code="approval_provider_missing",
                message=(
                    message
                    or f"Tool '{tc.name}' requires lifecycle approval, but no approval provider is configured"
                ),
                tool_name=tc.name,
                tool_call_id=tc.id,
                metadata=self._capability_target_diagnostic_metadata(tc),
            )
            self._emit_apply_patch_approval_resolved(
                tc,
                approval_id=self._emit_apply_patch_approval_requested(
                    tc,
                    reason=message,
                    tool=tool,
                    index=index,
                ),
                decision="deny_once",
                reason=diagnostic.message,
                tool=tool,
                index=index,
            )
            self._record_lifecycle_diagnostics([diagnostic], validation_context)
            self._emit_tool_end(
                tc,
                diagnostic.message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return diagnostic.message

        try:
            approval_tool_args, approval_intent = self._approval_payload_args(tc.arguments)
            file_change_approval_id = self._emit_apply_patch_approval_requested(
                tc,
                reason=message,
                tool=tool,
                index=index,
            )
            decision_result = request_approval(
                ApprovalRequest(
                    tool_name=tc.name,
                    tool_args=approval_tool_args,
                    tool_source="lifecycle_hook",
                    reason=message,
                    intent=approval_intent,
                    metadata={
                        "tool_call_id": tc.id,
                        "lifecycle_event": "PreToolUse",
                        "lifecycle_hooks": list(lifecycle_hooks or []),
                        **self._capability_target_event_metadata(tc),
                    },
                )
            )
        except (KeyboardInterrupt, EOFError):
            message = f"Tool '{tc.name}' lifecycle approval interrupted by user"
            self._emit_apply_patch_approval_resolved(
                tc,
                approval_id=locals().get("file_change_approval_id"),
                decision="deny_once",
                reason=message,
                tool=tool,
                index=index,
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.APPROVAL,
                kind=ToolDiagnosticKind.APPROVAL_DENIED,
                code="approval_interrupted",
                message=message,
                tool_name=tc.name,
                tool_call_id=tc.id,
                metadata=self._capability_target_diagnostic_metadata(tc),
            )
            self._record_lifecycle_diagnostics([diagnostic], validation_context)
            self._emit_tool_end(
                tc,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        self._emit_apply_patch_approval_resolved(
            tc,
            approval_id=file_change_approval_id,
            decision=decision_result.mode,
            reason=decision_result.reason,
            tool=tool,
            index=index,
        )

        if decision_result.approved:
            return None

        message = decision_result.reason or f"Tool '{tc.name}' lifecycle approval was denied"
        decision_diagnostics = [
            diagnostic_to_dict(item)
            for item in decision_result.meta.get("tool_diagnostics", [])
            if isinstance(item, (ToolDiagnostic, dict))
        ]
        if not decision_diagnostics:
            decision_diagnostics = [
                tool_diagnostic_from_failure(
                    stage=ToolDiagnosticStage.APPROVAL,
                    kind=ToolDiagnosticKind.APPROVAL_DENIED,
                    code="approval_denied",
                    message=message,
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    metadata=self._capability_target_diagnostic_metadata(tc),
                ).to_dict()
            ]
        decision_diagnostics = self._diagnostics_with_capability_target(
            tc,
            decision_diagnostics,
        )
        self._record_lifecycle_diagnostics(decision_diagnostics, validation_context)
        failure_meta = {
            "failure_kind": decision_result.meta.get(
                "failure_kind", ToolFailureKind.APPROVAL_DENIED.value
            ),
            **decision_result.meta,
            "tool_diagnostics": decision_diagnostics,
        }
        self._emit_tool_end(
            tc,
            message,
            tool=tool,
            index=index,
            meta=self._merge_meta(validation_meta, failure_meta),
        )
        return message

    def _persist_tool_diagnostics(
        self,
        diagnostics_payload: list[ToolDiagnostic | dict],
        context: dict,
        *,
        validation: object | None = None,
    ) -> None:
        diagnostics = getattr(getattr(self.agent, "config", None), "diagnostics", None)
        tool_diagnostics = getattr(diagnostics, "tool_diagnostics", None)
        if getattr(tool_diagnostics, "enabled", True) is False:
            return
        if not diagnostics_payload and not getattr(tool_diagnostics, "record_clean", False):
            return
        try:
            persist_tool_diagnostic_event(
                diagnostics=diagnostics_payload,
                metadata=context,
                validation=validation,
            )
        except Exception:
            pass

    def _validation_diagnostics(
        self,
        validation: object,
        *,
        tool_call_id: str | None,
    ) -> list[ToolDiagnostic]:
        return diagnostics_from_argument_validation(validation, tool_call_id=tool_call_id)

    def _record_lifecycle_diagnostics(
        self,
        diagnostics_payload: list[ToolDiagnostic | dict],
        context: dict,
    ) -> None:
        self._persist_tool_diagnostics(diagnostics_payload, context)

    def _dispatch_lifecycle_event(
        self,
        event_name: str,
        *,
        tool_call: "ToolCall",
        tool: object | None,
        result: object | None = None,
        error: dict | None = None,
        metadata: dict | None = None,
    ) -> list[LifecycleHookOutput]:
        return [
            record.output
            for record in self._dispatch_lifecycle_event_records(
                event_name,
                tool_call=tool_call,
                tool=tool,
                result=result,
                error=error,
                metadata=metadata,
            )
        ]

    def _dispatch_lifecycle_event_records(
        self,
        event_name: str,
        *,
        tool_call: "ToolCall",
        tool: object | None,
        result: object | None = None,
        error: dict | None = None,
        metadata: dict | None = None,
    ) -> list[_LifecycleOutputRecord]:
        dispatcher = getattr(self.agent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return []
        tool_source = self._tool_source(tool)
        trigger_source = str(getattr(self.agent, "permission_trigger_source", "") or "chat")
        session_run_id = str(getattr(self.agent, "current_session_id", "") or "")
        agent_run_id = runtime_agent_run_id(self.agent)
        turn_id = str(getattr(self.agent, "runtime_turn_id", "") or "")
        context = build_lifecycle_event_context(
            event_name,
            placement="server",
            session_run_id=session_run_id,
            agent_run_id=agent_run_id,
            turn_id=turn_id,
            trigger_source=trigger_source,
            origin="agent",
            locale=str(getattr(self.agent, "locale", "") or ""),
            metadata={
                "round_index": getattr(self.agent.state, "current_round", None),
                **(metadata or {}),
            },
            payload=build_tool_lifecycle_payload(
                event_name,
                tool_call=tool_call,
                tool=tool,
                tool_source=tool_source,
                result=result,
                error=error,
            ),
        )
        self._emit_lifecycle_observation("dispatch_start", event_name, context)
        try:
            results = dispatch(context)
        except Exception as exc:
            self._emit_lifecycle_observation(
                "dispatch_failed",
                event_name,
                context,
                error=str(exc),
            )
            raise
        outputs: list[_LifecycleOutputRecord] = []
        for result_item in list(results or []):
            output = getattr(result_item, "output", None)
            if isinstance(output, LifecycleHookOutput):
                declaration = getattr(result_item, "declaration", None)
                annotate_lifecycle_output_diagnostics(event_name, output)
                outputs.append(
                    _LifecycleOutputRecord(
                        output=output,
                        hook_id=str(getattr(declaration, "id", "") or ""),
                        display_name=str(getattr(declaration, "display_name", "") or ""),
                        handler_type=str(getattr(declaration, "handler_type", "") or ""),
                    )
                )
            self._emit_lifecycle_observation(
                "result",
                event_name,
                context,
                result=result_item,
            )
        return outputs

    def _dispatch_cwd_changed_lifecycle(
        self,
        *,
        tool_call: "ToolCall",
        tool: object | None,
        previous_working_directory: str,
        current_working_directory: str,
        execution_target: str,
    ) -> None:
        dispatcher = getattr(self.agent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return
        workspace_root = str(getattr(self.agent, "runtime_workspace_root", "") or "")
        agent_run_id = runtime_agent_run_id(self.agent)
        context = build_lifecycle_event_context(
            "CwdChanged",
            placement="server",
            session_run_id=str(getattr(self.agent, "current_session_id", "") or ""),
            agent_run_id=agent_run_id,
            turn_id=str(getattr(self.agent, "runtime_turn_id", "") or ""),
            trigger_source=str(
                getattr(self.agent, "permission_trigger_source", "") or "chat"
            ),
            origin="agent",
            locale=str(getattr(self.agent, "locale", "") or ""),
            metadata={
                "round_index": getattr(self.agent.state, "current_round", None),
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
            },
            payload={
                "previous_working_directory": previous_working_directory,
                "current_working_directory": current_working_directory,
                "runtime_working_directory": current_working_directory,
                "runtime_workspace_root": workspace_root,
                "execution_target": execution_target,
                "path_space": runtime_path_space(self.agent),
                "tool_names": [tool_call.name],
                "tool_call_ids": [tool_call.id],
                "tool_sources": [self._tool_source(tool)],
                "mcp_servers": [],
            },
        )
        self._emit_lifecycle_observation("dispatch_start", "CwdChanged", context)
        try:
            results = list(dispatch(context) or [])
        except Exception as exc:
            self._emit_lifecycle_observation(
                "dispatch_failed",
                "CwdChanged",
                context,
                error=str(exc),
            )
            return
        for result in results:
            output = getattr(result, "output", None)
            if isinstance(output, LifecycleHookOutput):
                annotate_lifecycle_output_diagnostics("CwdChanged", output)
            self._emit_lifecycle_observation(
                "result",
                "CwdChanged",
                context,
                result=result,
            )

    def _emit_lifecycle_observation(
        self,
        phase: str,
        event_name: str,
        context: object,
        *,
        result: object | None = None,
        error: str = "",
    ) -> None:
        emit = getattr(self.agent, "_emit_event", None)
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
            payload = {
                "phase": phase,
                "event_name": event_name,
                "placement": str(getattr(context, "placement", "") or "server"),
                "session_run_id": str(getattr(context, "session_run_id", "") or ""),
                "agent_run_id": str(getattr(context, "agent_run_id", "") or ""),
                "turn_id": str(getattr(context, "turn_id", "") or ""),
                "trigger_source": str(getattr(context, "source", "") or ""),
                "hook_id": str(getattr(declaration, "id", "") or ""),
                "display_name": str(getattr(declaration, "display_name", "") or ""),
                "source": str(getattr(declaration, "source", "") or ""),
                "decision": decision,
                "continue_flow": continue_flow,
                "diagnostics": diagnostics,
                "error": error,
                "level": level,
                "title": str(getattr(declaration, "display_name", "") or event_name),
                "payload": dict(getattr(context, "payload", {}) or {}),
            }
            if isinstance(output_dict, dict):
                payload.update(lifecycle_output_audit_fields(output))
                payload["output"] = output_dict
            emit(
                AgentEvent.lifecycle_hook(
                    payload,
                    runtime_artifacts=lifecycle_runtime_artifacts_for_event(
                        output,
                        event_name=event_name,
                        context=context,
                    ),
                )
            )
        except Exception:
            return

    @staticmethod
    def _tool_call_payload(tool_call: "ToolCall") -> dict:
        return {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": dict(tool_call.arguments or {}),
        }

    @staticmethod
    def _tool_call_from_lifecycle_input(
        current: "ToolCall",
        updated_input: dict,
    ) -> "ToolCall":
        raw = updated_input.get("tool_call")
        if not isinstance(raw, dict):
            return current
        raw_arguments = raw.get("arguments", current.arguments)
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        return ToolCall(
            id=current.id,
            name=str(raw.get("name") or current.name),
            arguments=arguments,
            argument_error=current.argument_error,
            argument_diagnostics=list(current.argument_diagnostics),
        )

    @staticmethod
    def _preserve_provider_tool_call_id(
        tool_call: "ToolCall",
        provider_tool_call_id: str,
    ) -> "ToolCall":
        if tool_call.id == provider_tool_call_id:
            return tool_call
        return ToolCall(
            id=provider_tool_call_id,
            name=tool_call.name,
            arguments=dict(tool_call.arguments or {}),
            argument_error=tool_call.argument_error,
            argument_diagnostics=list(tool_call.argument_diagnostics),
        )

    def _apply_pre_tool_lifecycle(
        self,
        context: BeforeToolExecuteContext,
        *,
        tool: object | None,
    ) -> _PreToolLifecycleResult | None:
        tool_call = context.tool_call
        if tool_call is None:
            return None
        try:
            records = self._dispatch_lifecycle_event_records(
                "PreToolUse",
                tool_call=tool_call,
                tool=tool,
                metadata=dict(context.metadata or {}),
            )
        except Exception as exc:
            return _PreToolLifecycleResult(
                blocked_message=f"Error: lifecycle PreToolUse failed for {tool_call.name}: {exc}"
            )
        approval_messages: list[str] = []
        approval_hooks: list[dict[str, str]] = []
        for record in records:
            output = record.output
            if lifecycle_gate_output_is_terminal(output):
                return _PreToolLifecycleResult(
                    blocked_message=lifecycle_output_message(
                        output,
                        fallback=f"lifecycle PreToolUse blocked {tool_call.name}",
                    )
                )
            if lifecycle_output_requests_approval(output):
                message = lifecycle_output_message(
                    output,
                    fallback=f"lifecycle PreToolUse requires approval for {tool_call.name}",
                )
                approval_messages.append(message)
                approval_hooks.append(
                    {
                        "hook_id": record.hook_id,
                        "display_name": record.display_name,
                        "handler_type": record.handler_type,
                        "reason": message,
                    }
                )
            if output.updated_input is not None:
                context.tool_call = self._tool_call_from_lifecycle_input(
                    context.tool_call or tool_call,
                    output.updated_input,
                )
        if approval_messages:
            return _PreToolLifecycleResult(
                approval_message="\n".join(approval_messages),
                approval_hooks=approval_hooks,
            )
        return None

    def _apply_post_tool_lifecycle(
        self,
        context: AfterToolExecuteContext,
        *,
        tool: object | None,
    ) -> None:
        try:
            outputs = self._dispatch_lifecycle_event(
                "PostToolUse",
                tool_call=context.tool_call,
                tool=tool,
                result=context.result,
                metadata=dict(context.metadata or {}),
            )
        except Exception:
            return
        for output in outputs:
            if output.updated_input is not None and "result" in output.updated_input:
                context.result = output.updated_input["result"]

    def _dispatch_post_tool_failure_lifecycle(
        self,
        *,
        tool_call: "ToolCall",
        tool: object | None,
        message: str,
        error_type: str,
        error_message: str,
        metadata: dict | None = None,
    ) -> None:
        try:
            self._dispatch_lifecycle_event(
                "PostToolUseFailure",
                tool_call=tool_call,
                tool=tool,
                result=message,
                error={"type": error_type, "message": error_message},
                metadata=metadata,
            )
        except Exception:
            return

    def _dispatch_post_tool_batch_lifecycle(
        self,
        *,
        tool_calls: list["ToolCall"],
        results: list[str],
    ) -> None:
        dispatcher = getattr(self.agent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return
        tool_sources: list[str] = []
        mcp_servers: list[str] = []
        for item in tool_calls:
            tool = self.agent.get_tool(item.name)
            if tool is None:
                tool = get_tool(item.name)
            source = self._tool_source(tool)
            if source:
                tool_sources.append(source)
            server = str(getattr(tool, "server_name", "") or "").strip()
            if server:
                mcp_servers.append(server)
        context = build_lifecycle_event_context(
            "PostToolBatch",
            placement="server",
            session_run_id=str(getattr(self.agent, "current_session_id", "") or ""),
            agent_run_id=runtime_agent_run_id(self.agent),
            turn_id=str(getattr(self.agent, "runtime_turn_id", "") or ""),
            trigger_source=str(getattr(self.agent, "permission_trigger_source", "") or "chat"),
            origin="agent",
            locale=str(getattr(self.agent, "locale", "") or ""),
            metadata={"round_index": getattr(self.agent.state, "current_round", None)},
            payload=build_tool_batch_lifecycle_payload(
                tool_calls=tool_calls,
                results=results,
                tool_sources=tool_sources,
                mcp_servers=mcp_servers,
            ),
        )
        try:
            self._emit_lifecycle_observation("dispatch_start", "PostToolBatch", context)
            results = dispatch(context)
            for result_item in list(results or []):
                output = getattr(result_item, "output", None)
                if isinstance(output, LifecycleHookOutput):
                    annotate_lifecycle_output_diagnostics("PostToolBatch", output)
                self._emit_lifecycle_observation(
                    "result",
                    "PostToolBatch",
                    context,
                    result=result_item,
                )
        except Exception:
            return

    @staticmethod
    def _tool_result_error_diagnostic(
        result: object,
        *,
        tool_name: str,
        tool_call_id: str | None,
    ) -> ToolDiagnostic | None:
        if not isinstance(result, str):
            return None
        text = result.strip()
        if not text.startswith("Error"):
            return None
        match = re.match(r"^Error\s+\[([A-Z0-9_:-]+)\]:\s*(.*)$", text)
        if match:
            code = match.group(1)
            message = match.group(2) or text
        else:
            code = "tool_result_error"
            message = text
        return tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.EXECUTION,
            kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
            code=code,
            message=message,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            repairable=True,
        )

    def execute(self, tc: "ToolCall", *, index: int | None = None) -> str:
        """Execute a single tool call."""
        return self._execute_tool_call(tc, index=index)

    def _execute_resolved_tool(
        self,
        tc: "ToolCall",
        tool: object,
        *,
        index: int | None = None,
    ) -> str:
        """Execute an already routed tool through the normal execution pipeline."""
        return self._execute_tool_call(tc, index=index, resolved_tool=tool)

    def _execute_tool_call(
        self,
        tc: "ToolCall",
        *,
        index: int | None = None,
        resolved_tool: object | None = None,
    ) -> str:
        tool, routed_tool, has_exposure_plan = (
            self._resolve_model_tool(tc.name)
            if resolved_tool is None
            else (resolved_tool, resolved_tool, True)
        )
        budget_error = self._consume_tool_call_budget()
        if budget_error is not None:
            return self._return_budget_error(
                tc,
                budget_error,
                tool=tool,
                index=index,
            )
        if has_exposure_plan and tool is None and routed_tool is not None:
            return self._return_tool_not_directly_exposed(
                tc,
                tool=routed_tool,
                index=index,
            )
        suppress_lifecycle = bool(
            getattr(self.agent, "_suppress_tool_lifecycle", False)
        )

        before_context = BeforeToolExecuteContext(
            hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
            tool_call=tc,
            round_index=self.agent.state.current_round,
            session_id=getattr(self.agent, "current_session_id", None),
            metadata={
                **memory_metadata_from_agent(self.agent),
                "tool_source": getattr(
                    tool, "tool_source", "builtin" if tool is not None else "unknown"
                ),
                "mcp_server": getattr(tool, "server_name", None),
                "tool_description": getattr(tool, "description", None),
                "tool_schema": getattr(tool, "parameters", None),
            },
        )

        lifecycle_result = (
            None
            if suppress_lifecycle
            else self._apply_pre_tool_lifecycle(before_context, tool=tool)
        )
        if lifecycle_result is not None and lifecycle_result.blocked_message is not None:
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="lifecycle_pre_tool_denied",
                message=lifecycle_result.blocked_message,
                tool_name=tc.name,
                tool_call_id=tc.id,
            )
            lifecycle_context = self._tool_argument_context(tc, tool)
            self._record_lifecycle_diagnostics([diagnostic], lifecycle_context)
            self._emit_tool_end(
                tc,
                lifecycle_result.blocked_message,
                tool=tool,
                index=index,
                meta=self._diagnostics_meta([diagnostic]),
                emit_file_change=False,
            )
            return lifecycle_result.blocked_message

        if not suppress_lifecycle:
            internal_lifecycle = dispatch_internal_lifecycle_hook_point(
                getattr(self.agent, "lifecycle_dispatcher", None),
                HookPoint.BEFORE_TOOL_EXECUTE,
                before_context,
                trigger_source=str(
                    getattr(self.agent, "permission_trigger_source", "") or "chat"
                ),
                origin="agent",
            )
            if internal_lifecycle.blocked:
                diagnostic = tool_diagnostic_from_failure(
                    stage=ToolDiagnosticStage.PREFLIGHT,
                    kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                    code="legacy_lifecycle_pre_tool_denied",
                    message=internal_lifecycle.message,
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                )
                lifecycle_context = self._tool_argument_context(tc, tool)
                self._record_lifecycle_diagnostics([diagnostic], lifecycle_context)
                self._emit_tool_end(
                    tc,
                    internal_lifecycle.message,
                    tool=tool,
                    index=index,
                    meta=self._diagnostics_meta([diagnostic]),
                    emit_file_change=False,
                )
                return internal_lifecycle.message
            if isinstance(internal_lifecycle.context, BeforeToolExecuteContext):
                before_context = internal_lifecycle.context

        tool_call = self._preserve_provider_tool_call_id(
            before_context.tool_call or tc,
            tc.id,
        )
        tool_call = self._sync_tool_call(tc, tool_call)
        before_context.tool_call = tool_call

        tool, routed_tool, has_exposure_plan = (
            self._resolve_model_tool(tool_call.name)
            if resolved_tool is None
            else (resolved_tool, resolved_tool, True)
        )
        if has_exposure_plan and tool is None and routed_tool is not None:
            return self._return_tool_not_directly_exposed(
                tool_call,
                tool=routed_tool,
                index=index,
            )

        if tool is None:
            message = f"Error: unknown tool '{tool_call.name}'"
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="unknown_tool",
                message=message,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
            )
            context = self._tool_argument_context(tool_call, tool)
            self._record_lifecycle_diagnostics([diagnostic], context)
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._diagnostics_meta([diagnostic]),
                emit_file_change=False,
            )
            return message

        tool_start_emitted = False
        final_validation_context = self._tool_argument_context(tool_call, tool)
        self._emit_tool_arguments_complete(tool_call, tool=tool, index=index)
        argument_error = getattr(tool_call, "argument_error", None)
        if argument_error:
            parse_validation = {
                "tool_name": tool_call.name,
                "policy": "provider_parse",
                "final_valid": False,
                "initial_issues": [
                    {
                        "path": "$",
                        "field": None,
                        "code": "invalid_tool_arguments",
                        "expected": "object",
                        "actual": "invalid",
                        "receivedPreview": "",
                        "severity": "error",
                        "repairable": False,
                        "message": str(argument_error),
                    }
                ],
                "final_issues": [],
                "repairs": [],
                "provider_diagnostics": list(
                    getattr(tool_call, "argument_diagnostics", None) or []
                ),
            }
            parse_diagnostics = self._validation_diagnostics(
                parse_validation,
                tool_call_id=tool_call.id,
            )
            self._persist_tool_diagnostics(
                parse_diagnostics,
                final_validation_context,
                validation=parse_validation,
            )
            message = self._bad_arguments_message(tool_call.name, str(argument_error))
            tool_start_emitted = self._emit_capability_target_start_if_needed(
                tool_call,
                tool,
                index=index,
                already_emitted=tool_start_emitted,
            )
            self._emit_tool_arguments_invalid(
                tool_call,
                message,
                tool=tool,
                index=index,
                code="preflight_type_error",
            )
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._diagnostics_meta(parse_diagnostics),
                emit_file_change=False,
            )
            return message

        final_validation = validate_and_repair_tool_arguments(
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            schema=getattr(tool, "parameters", None),
            policy=policy_for_provider(
                compat=final_validation_context.get("compat"),
                model=final_validation_context.get("model"),
            ),
        )
        final_validation_diagnostics = self._validation_diagnostics(
            final_validation,
            tool_call_id=tool_call.id,
        )
        if not final_validation.final_valid:
            self._persist_tool_diagnostics(
                final_validation_diagnostics,
                final_validation_context,
                validation=final_validation,
            )
            validation_meta = self._diagnostics_meta(
                self._diagnostics_with_capability_target(
                    tool_call,
                    final_validation_diagnostics,
                )
            )
            message = self._bad_arguments_message_for_call(
                tool_call,
                tool,
                format_tool_argument_retry_message(
                    tool_call.name,
                    final_validation.final_issues,
                ),
            )
            tool_start_emitted = self._emit_capability_target_start_if_needed(
                tool_call,
                tool,
                index=index,
                already_emitted=tool_start_emitted,
            )
            self._emit_tool_arguments_invalid(
                tool_call,
                message,
                tool=tool,
                index=index,
            )
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=validation_meta,
                emit_file_change=False,
            )
            return message
        tool_call.arguments = final_validation.arguments
        self._sync_capability_target_arguments(tool_call, tool_call.arguments)
        final_validation_context = self._tool_argument_context(tool_call, tool)
        self._persist_tool_diagnostics(
            final_validation_diagnostics,
            final_validation_context,
            validation=final_validation,
        )
        validation_meta = self._diagnostics_meta(
            self._diagnostics_with_capability_target(
                tool_call,
                final_validation_diagnostics,
            )
        )
        tool_start_emitted = self._emit_capability_target_start_if_needed(
            tool_call,
            tool,
            index=index,
            already_emitted=tool_start_emitted,
        )

        try:
            preflight_error = (
                tool.preflight_validate(**tool_call.arguments) if tool is not None else None
            )
        except TypeError as e:
            message = self._bad_arguments_message(tool_call.name, str(e))
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="preflight_type_error",
                message=str(e),
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], final_validation_context)
            self._emit_tool_arguments_invalid(
                tool_call,
                message,
                tool=tool,
                index=index,
            )
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
                emit_file_change=False,
            )
            return message
        if preflight_error:
            preflight_message = self._preflight_error_message_for_call(
                tool_call,
                tool,
                str(preflight_error),
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="preflight_failed",
                message=preflight_message,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], final_validation_context)
            self._emit_tool_arguments_invalid(
                tool_call,
                preflight_message,
                tool=tool,
                index=index,
                code="preflight_failed",
            )
            self._emit_tool_end(
                tool_call,
                preflight_message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
                emit_file_change=False,
            )
            return preflight_message
        self._emit_tool_arguments_valid(tool_call, tool=tool, index=index)
        preview_outcome = self._ensure_apply_patch_preview_outcome(
            tool_call,
            tool=tool,
            index=index,
        )
        if preview_outcome is not None and not preview_outcome.ready:
            message = self._preview_failure_message_for_call(
                tool_call,
                preview_outcome.error or "apply_patch semantic preview failed",
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code=preview_outcome.failure_code or "semantic_preview_failed",
                message=message,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                repairable=True,
                metadata=self._capability_target_diagnostic_metadata(tool_call),
            )
            self._record_lifecycle_diagnostics([diagnostic], final_validation_context)
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(
                    validation_meta,
                    self._diagnostics_meta([diagnostic]),
                ),
                emit_file_change=False,
            )
            return message

        if lifecycle_result is not None and lifecycle_result.approval_message is not None:
            lifecycle_approval_context = self._tool_argument_context(tool_call, tool)
            approval_message = self._request_lifecycle_pre_tool_approval(
                tool_call,
                tool,
                lifecycle_result.approval_message,
                lifecycle_approval_context,
                validation_meta,
                lifecycle_hooks=lifecycle_result.approval_hooks,
                index=index,
            )
            if approval_message is not None:
                return approval_message

        permission_decision = self._evaluate_permission(tool_call, tool)
        if permission_decision is not None and permission_decision.action in {
            PermissionAction.DENY,
            PermissionAction.BLOCKED_REVIEW,
        }:
            return self._return_permission_block(
                tool_call,
                tool,
                permission_decision,
                index=index,
                validation_meta=validation_meta,
            )
        if (
            permission_decision is not None
            and permission_decision.action == PermissionAction.REQUIRE_APPROVAL
        ):
            transformed_validation_context = self._tool_argument_context(
                tool_call,
                tool,
            )
            approval_message = self._request_permission_approval(
                tool_call,
                tool,
                permission_decision,
                transformed_validation_context,
                validation_meta,
                preview_outcome=preview_outcome,
                index=index,
            )
            if approval_message is not None:
                return approval_message
        execution_context = self._tool_argument_context(tool_call, tool)
        if not tool_start_emitted:
            self._emit_tool_start(tool_call, tool=tool, index=index)
        backend = getattr(tool, "backend", None)
        context = getattr(backend, "context", None)
        backend_id = getattr(backend, "backend_id", None)
        execution_target = getattr(context, "execution_target", None) or (
            "local" if backend_id in (None, "local") else str(backend_id)
        )
        previous_tool_call_id = getattr(context, "current_tool_call_id", None)
        previous_permission_context = getattr(context, "permission_context", {})
        if context is not None:
            try:
                setattr(context, "current_tool_call_id", tool_call.id)
            except Exception:
                pass
            if permission_decision is not None:
                try:
                    setattr(
                        context,
                        "permission_context",
                        self._permission_context_payload(permission_decision),
                    )
                except Exception:
                    pass
        try:
            restore_lifecycle_context = self._bind_tool_lifecycle_context(
                tool,
                tool_call,
            )
            try:
                shell_context = nullcontext()
                previous_working_directory = runtime_working_directory(self.agent)
                if tool_call.name == "shell":
                    initial_cwd = previous_working_directory
                    if initial_cwd and getattr(tool, "_cwd", None) is None:
                        setattr(tool, "_cwd", initial_cwd)
                    agent_id = runtime_agent_run_id(self.agent) or f"agent:{id(self.agent)}"

                    def _emit_shell_runtime(payload: dict) -> None:
                        self.agent._emit_event(AgentEvent.runtime_status(payload))

                    shell_context = get_interactive_run_limiter().shell_slot(
                        agent_id,
                        tool_call_id=tool_call.id,
                        is_cancelled=getattr(
                            self.agent, "stop_requested", lambda: False
                        ),
                        on_wait=_emit_shell_runtime,
                    )
                with shell_context:
                    if resolved_tool is None and tool_call.name == "tool_search":
                        result = self._execute_tool_search(tool_call)
                    elif resolved_tool is None and tool_call.name == "capability_execute":
                        result = self._execute_capability_execute(
                            tool_call,
                            index=index,
                        )
                    elif self._should_save_approved_mutation_candidate(
                        tool_call,
                        tool,
                        preview_outcome,
                    ):
                        result = self._save_approved_mutation_candidate(
                            tool_call,
                            tool,
                            preview_outcome,
                        )
                    else:
                        result = tool.execute(**tool_call.arguments)
            finally:
                if callable(restore_lifecycle_context):
                    try:
                        restore_lifecycle_context()
                    except Exception:
                        pass
            if (shell_cwd := getattr(tool, "_cwd", None)) is not None:
                setattr(self.agent, "runtime_working_directory", str(shell_cwd))
                if (
                    tool_call.name == "shell"
                    and str(shell_cwd) != str(previous_working_directory or "")
                ):
                    self._dispatch_cwd_changed_lifecycle(
                        tool_call=tool_call,
                        tool=tool,
                        previous_working_directory=str(previous_working_directory or ""),
                        current_working_directory=str(shell_cwd),
                        execution_target=execution_target,
                    )
            if limit_message := runtime_budget_limit_message(self.agent):
                message = f"Error: {limit_message}"
                return self._return_budget_error(
                    tool_call,
                    message,
                    tool=tool,
                    index=index,
                )
            current_agent_run_id = runtime_agent_run_id(self.agent)
            current_workspace_root = runtime_workspace_root(self.agent)
            current_working_directory = runtime_working_directory(self.agent)
            path_space = runtime_path_space(self.agent)
            after_context = AfterToolExecuteContext(
                hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                tool_call=tool_call,
                result=result,
                round_index=self.agent.state.current_round,
                session_id=getattr(self.agent, "current_session_id", None),
                metadata={
                    **memory_metadata_from_agent(self.agent),
                    "tool_source": getattr(
                        tool, "tool_source", "builtin" if tool is not None else "unknown"
                    ),
                    "mcp_server": getattr(tool, "server_name", None),
                    "backend_id": backend_id,
                    "execution_target": execution_target,
                    "agent_run_id": current_agent_run_id,
                    "runtime_workspace_root": current_workspace_root,
                    "runtime_working_directory": current_working_directory,
                    "path_space": path_space,
                    "trigger_source": str(
                        getattr(self.agent, "permission_trigger_source", "") or "chat"
                    ),
                },
            )
            if not suppress_lifecycle:
                internal_lifecycle = dispatch_internal_lifecycle_hook_point(
                    getattr(self.agent, "lifecycle_dispatcher", None),
                    HookPoint.AFTER_TOOL_EXECUTE,
                    after_context,
                    trigger_source=str(
                        getattr(self.agent, "permission_trigger_source", "") or "chat"
                    ),
                    origin="agent",
                )
                if isinstance(internal_lifecycle.context, AfterToolExecuteContext):
                    after_context = internal_lifecycle.context
                self._apply_post_tool_lifecycle(after_context, tool=tool)
            result_diagnostic = self._tool_result_error_diagnostic(
                after_context.result,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
            )
            result_meta = self._diagnostics_meta([result_diagnostic]) if result_diagnostic else None
            if result_diagnostic is not None:
                self._record_lifecycle_diagnostics(
                    [result_diagnostic],
                    execution_context,
                )
            gateway_meta = self._consume_tool_result_meta(tool_call)
            self._emit_tool_end(
                tool_call,
                after_context.result,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, gateway_meta, result_meta),
            )
            return after_context.result
        except TypeError as e:
            message = f"Error: bad arguments for {tool_call.name}: {e}"
            self._dispatch_post_tool_failure_lifecycle(
                tool_call=tool_call,
                tool=tool,
                message=message,
                error_type=type(e).__name__,
                error_message=str(e),
                metadata=execution_context,
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.EXECUTION,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="execution_type_error",
                message=str(e),
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], execution_context)
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        except AgentRunCancelled:
            message = f"Tool '{tool_call.name}' cancelled while waiting for runtime slot"
            self._dispatch_post_tool_failure_lifecycle(
                tool_call=tool_call,
                tool=tool,
                message=message,
                error_type="AgentRunCancelled",
                error_message=message,
                metadata=execution_context,
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.EXECUTION,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="tool_cancelled",
                message=message,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
            )
            self._record_lifecycle_diagnostics([diagnostic], execution_context)
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        except Exception as e:
            message = f"Error executing {tool_call.name}: {e}"
            self._dispatch_post_tool_failure_lifecycle(
                tool_call=tool_call,
                tool=tool,
                message=message,
                error_type=type(e).__name__,
                error_message=str(e),
                metadata=execution_context,
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.EXECUTION,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code=type(e).__name__,
                message=str(e),
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], execution_context)
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(
                    validation_meta,
                    {
                        "failure_kind": ToolFailureKind.TOOL_RESULT_ERROR.value,
                        **(self._diagnostics_meta([diagnostic]) or {}),
                    },
                ),
            )
            return message
        finally:
            if context is not None:
                try:
                    setattr(context, "current_tool_call_id", previous_tool_call_id)
                except Exception:
                    pass
                try:
                    setattr(context, "permission_context", previous_permission_context)
                except Exception:
                    pass

    def execute_parallel(self, tool_calls: List["ToolCall"]) -> List[str]:
        """Execute multiple tool calls in parallel."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(self.execute, tc, index=index)
                for index, tc in enumerate(tool_calls)
            ]
            results = [f.result() for f in futures]
        self._dispatch_post_tool_batch_lifecycle(
            tool_calls=list(tool_calls),
            results=results,
        )
        return results
