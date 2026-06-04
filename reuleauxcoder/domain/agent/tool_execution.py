"""Tool execution - handles tool calls."""

from __future__ import annotations
from typing import TYPE_CHECKING, List
import concurrent.futures
from contextlib import nullcontext
import re

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent, ToolFailureKind
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
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
from reuleauxcoder.domain.memory.runtime import memory_metadata_from_agent
from reuleauxcoder.extensions.tools.registry import get_tool
from reuleauxcoder.services.llm.diagnostics import persist_tool_diagnostic_event


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    @staticmethod
    def _tool_source(tool: object | None) -> str:
        return getattr(tool, "tool_source", "builtin" if tool is not None else "unknown")

    def _emit_tool_end(
        self,
        tc: "ToolCall",
        result: str,
        *,
        tool: object | None = None,
        index: int | None = None,
        meta: dict | None = None,
    ) -> None:
        self.agent._emit_event(
            AgentEvent.tool_call_end(
                tc.name,
                result,
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
                meta=meta,
            )
        )

    def _emit_tool_start(
        self,
        tc: "ToolCall",
        *,
        tool: object | None = None,
        index: int | None = None,
    ) -> None:
        self.agent._emit_event(
            AgentEvent.tool_call_start(
                tc.name,
                dict(tc.arguments or {}),
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                index=index,
            )
        )

    @staticmethod
    def _bad_arguments_message(tool_name: str, detail: str) -> str:
        return f"Error: bad arguments for {tool_name}: {detail}"

    @staticmethod
    def _diagnostics_meta(diagnostics: list[ToolDiagnostic | dict] | None) -> dict | None:
        if not diagnostics:
            return None
        return {"tool_diagnostics": [diagnostic_to_dict(item) for item in diagnostics]}

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
        return {
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
            return f"Error: tool '{tool_name}' blocked pending review: {reason}"
        return f"Error: tool '{tool_name}' denied by permission gateway: {reason}"

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
        message = self._permission_block_message(decision, tc.name)
        diagnostic = tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.PREFLIGHT,
            kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
            code=f"permission_{decision.action.value}",
            message=message,
            tool_name=tc.name,
            tool_call_id=tc.id,
            metadata={"permission": decision.to_dict()},
        )
        context = self._tool_argument_context(tc, tool)
        self._record_lifecycle_diagnostics([diagnostic], context)
        self._emit_tool_end(
            tc,
            message,
            tool=tool,
            index=index,
            meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
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
        index: int | None = None,
    ) -> str | None:
        provider = self.agent.approval_provider
        if provider is None:
            message = (
                decision.reason
                or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.APPROVAL,
                kind=ToolDiagnosticKind.APPROVAL_DENIED,
                code="approval_provider_missing",
                message=message,
                tool_name=tc.name,
                tool_call_id=tc.id,
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
            decision_result = provider.request_approval(
                ApprovalRequest(
                    tool_name=tc.name,
                    tool_args=approval_tool_args,
                    tool_source=getattr(tool, "tool_source", "builtin_tool")
                    if tool is not None
                    else "unknown",
                    reason=decision.reason,
                    intent=approval_intent,
                    metadata={
                        "tool_call_id": tc.id,
                        "permission": decision.to_dict(),
                    },
                )
            )
        except (KeyboardInterrupt, EOFError):
            message = f"Tool '{tc.name}' approval interrupted by user"
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.APPROVAL,
                kind=ToolDiagnosticKind.APPROVAL_DENIED,
                code="approval_interrupted",
                message=message,
                tool_name=tc.name,
                tool_call_id=tc.id,
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
        if decision_result.approved:
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
                ).to_dict()
            ]
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
        dispatcher = getattr(self.agent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return []
        tool_source = self._tool_source(tool)
        trigger_source = str(getattr(self.agent, "permission_trigger_source", "") or "chat")
        session_run_id = str(getattr(self.agent, "current_session_id", "") or "")
        agent_run_id = str(getattr(self.agent, "runtime_agent_run_id", "") or "")
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
        outputs: list[LifecycleHookOutput] = []
        for result_item in list(results or []):
            output = getattr(result_item, "output", None)
            if isinstance(output, LifecycleHookOutput):
                annotate_lifecycle_output_diagnostics(event_name, output)
                outputs.append(output)
            self._emit_lifecycle_observation(
                "result",
                event_name,
                context,
                result=result_item,
            )
        return outputs

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
                payload["output"] = output_dict
            emit(AgentEvent.lifecycle_hook(payload))
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
    ) -> str | None:
        tool_call = context.tool_call
        if tool_call is None:
            return None
        try:
            outputs = self._dispatch_lifecycle_event(
                "PreToolUse",
                tool_call=tool_call,
                tool=tool,
                metadata=dict(context.metadata or {}),
            )
        except Exception as exc:
            return f"Error: lifecycle PreToolUse failed for {tool_call.name}: {exc}"
        for output in outputs:
            if output.decision == "deny" or output.continue_flow is False:
                reason = output.reason if isinstance(output.reason, str) else ""
                return reason or f"lifecycle PreToolUse denied {tool_call.name}"
            if output.updated_input is not None:
                context.tool_call = self._tool_call_from_lifecycle_input(
                    context.tool_call or tool_call,
                    output.updated_input,
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
            agent_run_id=str(getattr(self.agent, "runtime_agent_run_id", "") or ""),
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
        tool = self.agent.get_tool(tc.name)
        if tool is None:
            tool = get_tool(tc.name)
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

        lifecycle_error = (
            None
            if suppress_lifecycle
            else self._apply_pre_tool_lifecycle(before_context, tool=tool)
        )
        if lifecycle_error is not None:
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="lifecycle_pre_tool_denied",
                message=lifecycle_error,
                tool_name=tc.name,
                tool_call_id=tc.id,
            )
            lifecycle_context = self._tool_argument_context(tc, tool)
            self._record_lifecycle_diagnostics([diagnostic], lifecycle_context)
            self._emit_tool_end(
                tc,
                lifecycle_error,
                tool=tool,
                index=index,
                meta=self._diagnostics_meta([diagnostic]),
            )
            return lifecycle_error

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
                )
                return internal_lifecycle.message
            if isinstance(internal_lifecycle.context, BeforeToolExecuteContext):
                before_context = internal_lifecycle.context

        tool_call = self._preserve_provider_tool_call_id(
            before_context.tool_call or tc,
            tc.id,
        )
        before_context.tool_call = tool_call

        # First check agent's tools, then fall back to global registry
        tool = self.agent.get_tool(tool_call.name)
        if tool is None:
            tool = get_tool(tool_call.name)

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
            )
            return message

        final_validation_context = self._tool_argument_context(tool_call, tool)
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
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._diagnostics_meta(parse_diagnostics),
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
        self._persist_tool_diagnostics(
            final_validation_diagnostics,
            final_validation_context,
            validation=final_validation,
        )
        validation_meta = self._diagnostics_meta(final_validation_diagnostics)
        if not final_validation.final_valid:
            message = self._bad_arguments_message(
                tool_call.name,
                format_tool_argument_retry_message(
                    tool_call.name,
                    final_validation.final_issues,
                ),
            )
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=validation_meta,
            )
            return message
        tool_call.arguments = final_validation.arguments

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
            self._emit_tool_end(
                tool_call,
                message,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        if preflight_error:
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="preflight_failed",
                message=str(preflight_error),
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], final_validation_context)
            self._emit_tool_end(
                tool_call,
                preflight_error,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return preflight_error

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
                index=index,
            )
            if approval_message is not None:
                return approval_message
        execution_context = self._tool_argument_context(tool_call, tool)
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
            shell_context = nullcontext()
            if tool_call.name == "shell":
                agent_id = str(
                    getattr(self.agent, "runtime_agent_id", "")
                    or f"agent:{id(self.agent)}"
                )

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
                result = tool.execute(**tool_call.arguments)
            if (shell_cwd := getattr(tool, "_cwd", None)) is not None:
                setattr(self.agent, "runtime_working_directory", str(shell_cwd))
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
            self._emit_tool_end(
                tool_call,
                after_context.result,
                tool=tool,
                index=index,
                meta=self._merge_meta(validation_meta, result_meta),
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
