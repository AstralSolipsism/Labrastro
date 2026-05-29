"""Tool execution - handles tool calls."""

from __future__ import annotations
from typing import TYPE_CHECKING, List
import concurrent.futures
from contextlib import nullcontext
import copy
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
        meta: dict | None = None,
    ) -> None:
        self.agent._emit_event(
            AgentEvent.tool_call_end(
                tc.name,
                result,
                tool_call_id=tc.id,
                tool_source=self._tool_source(tool),
                meta=meta,
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

    def execute(self, tc: "ToolCall") -> str:
        """Execute a single tool call."""
        tool = self.agent.get_tool(tc.name)
        if tool is None:
            tool = get_tool(tc.name)

        # Tool permissions are centralized in PermissionGateway; this hook point
        # is reserved for transforms/observers, not guard-chain authorization.
        permission_decision = self._evaluate_permission(tc, tool)
        approved_permission_key: tuple[str, dict] | None = None

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
                "permission_decision": (
                    permission_decision.to_dict()
                    if permission_decision is not None
                    else {}
                ),
            },
        )

        if permission_decision is not None and permission_decision.action in {
            PermissionAction.DENY,
            PermissionAction.BLOCKED_REVIEW,
        }:
            return self._return_permission_block(tc, tool, permission_decision)

        argument_error = getattr(tc, "argument_error", None)
        if argument_error:
            parse_validation = {
                "tool_name": tc.name,
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
                    getattr(tc, "argument_diagnostics", None) or []
                ),
            }
            context = self._tool_argument_context(tc, tool)
            parse_diagnostics = self._validation_diagnostics(
                parse_validation,
                tool_call_id=tc.id,
            )
            self._persist_tool_diagnostics(
                parse_diagnostics,
                context,
                validation=parse_validation,
            )
            message = self._bad_arguments_message(tc.name, str(argument_error))
            self._emit_tool_end(
                tc,
                message,
                tool=tool,
                meta=self._diagnostics_meta(parse_diagnostics),
            )
            return message

        validation_context = self._tool_argument_context(tc, tool)
        validation = validate_and_repair_tool_arguments(
            tool_name=tc.name,
            arguments=tc.arguments,
            schema=getattr(tool, "parameters", None),
            policy=policy_for_provider(
                compat=validation_context.get("compat"),
                model=validation_context.get("model"),
            ),
        )
        validation_diagnostics = self._validation_diagnostics(
            validation,
            tool_call_id=tc.id,
        )
        self._persist_tool_diagnostics(
            validation_diagnostics,
            validation_context,
            validation=validation,
        )
        validation_meta = self._diagnostics_meta(validation_diagnostics)
        if not validation.final_valid:
            message = self._bad_arguments_message(
                tc.name,
                format_tool_argument_retry_message(tc.name, validation.final_issues),
            )
            self._emit_tool_end(tc, message, tool=tool, meta=validation_meta)
            return message
        tc.arguments = validation.arguments

        try:
            preflight_error = (
                tool.preflight_validate(**tc.arguments) if tool is not None else None
            )
        except TypeError as e:
            message = self._bad_arguments_message(tc.name, str(e))
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="preflight_type_error",
                message=str(e),
                tool_name=tc.name,
                tool_call_id=tc.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], validation_context)
            self._emit_tool_end(
                tc,
                message,
                tool=tool,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        if preflight_error:
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREFLIGHT,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code="preflight_failed",
                message=str(preflight_error),
                tool_name=tc.name,
                tool_call_id=tc.id,
                repairable=True,
            )
            self._record_lifecycle_diagnostics([diagnostic], validation_context)
            self._emit_tool_end(
                tc,
                preflight_error,
                tool=tool,
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return preflight_error

        if (
            permission_decision is not None
            and permission_decision.action == PermissionAction.REQUIRE_APPROVAL
        ):
            approval_message = self._request_permission_approval(
                tc,
                tool,
                permission_decision,
                validation_context,
                validation_meta,
            )
            if approval_message is not None:
                return approval_message
            approved_permission_key = (tc.name, copy.deepcopy(dict(tc.arguments or {})))

        before_context = self.agent.hook_registry.run_transforms(
            HookPoint.BEFORE_TOOL_EXECUTE,
            before_context,
        )
        self.agent.hook_registry.run_observers(
            HookPoint.BEFORE_TOOL_EXECUTE, before_context
        )

        tool_call = before_context.tool_call or tc

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
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        permission_decision = self._evaluate_permission(tool_call, tool)
        if permission_decision is not None and permission_decision.action in {
            PermissionAction.DENY,
            PermissionAction.BLOCKED_REVIEW,
        }:
            return self._return_permission_block(
                tool_call,
                tool,
                permission_decision,
                validation_meta=validation_meta,
            )
        if (
            permission_decision is not None
            and permission_decision.action == PermissionAction.REQUIRE_APPROVAL
        ):
            already_approved = (
                approved_permission_key is not None
                and approved_permission_key[0] == tool_call.name
                and approved_permission_key[1] == tool_call.arguments
            )
            if not already_approved:
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
                )
                if approval_message is not None:
                    return approval_message
        execution_context = self._tool_argument_context(tool_call, tool)
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
            after_context = self.agent.hook_registry.run_transforms(
                HookPoint.AFTER_TOOL_EXECUTE,
                after_context,
            )
            self.agent.hook_registry.run_observers(
                HookPoint.AFTER_TOOL_EXECUTE, after_context
            )
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
                meta=self._merge_meta(validation_meta, result_meta),
            )
            return after_context.result
        except TypeError as e:
            message = f"Error: bad arguments for {tool_call.name}: {e}"
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
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        except AgentRunCancelled:
            message = f"Tool '{tool_call.name}' cancelled while waiting for runtime slot"
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
                meta=self._merge_meta(validation_meta, self._diagnostics_meta([diagnostic])),
            )
            return message
        except Exception as e:
            message = f"Error executing {tool_call.name}: {e}"
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
            futures = [pool.submit(self.execute, tc) for tc in tool_calls]
            return [f.result() for f in futures]
