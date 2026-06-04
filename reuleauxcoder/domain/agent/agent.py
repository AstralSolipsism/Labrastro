"""Core agent - the main agent class."""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, List
from dataclasses import dataclass, field, replace
import threading

if TYPE_CHECKING:
    from reuleauxcoder.domain.approval import ApprovalProvider
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.extensions.tools.base import Tool
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig, ModeConfig
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.hooks import HookBase, HookPoint, HookRegistry
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    annotate_lifecycle_output_diagnostics,
    build_lifecycle_event_context,
    build_permission_lifecycle_payload,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import (
    PermissionDecision,
    PermissionGateway,
    PermissionRequest,
    PermissionSubject,
    PermissionTarget,
)
from reuleauxcoder.infrastructure.platform import get_platform_info
from reuleauxcoder.services.prompt.builder import system_prompt


def _same_approval_target(left: ApprovalRuleConfig, right: ApprovalRuleConfig) -> bool:
    return (
        left.tool_name == right.tool_name
        and left.tool_source == right.tool_source
        and left.mcp_server == right.mcp_server
        and left.effect_class == right.effect_class
        and left.profile == right.profile
    )


def _lifecycle_reason_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _lifecycle_additional_context_messages(items: list[object]) -> list[dict]:
    messages: list[dict] = []
    text_items: list[str] = []
    for item in items:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                role = str(item.get("role") or "system").strip().lower()
                if role in {"system", "developer"}:
                    messages.append({"role": "system", "content": content.strip()})
                else:
                    text_items.append(content.strip())
                continue
        text = _lifecycle_reason_text(item)
        if text:
            text_items.append(text)
    if text_items:
        messages.append(
            {
                "role": "system",
                "content": "Lifecycle additional context:\n"
                + "\n".join(text_items),
            }
        )
    return messages


def _lifecycle_terminal_message(output_dict: dict) -> str:
    user_message = _lifecycle_reason_text(output_dict.get("user_message"))
    if user_message:
        return user_message
    return _lifecycle_reason_text(output_dict.get("reason"))


def _terminal_lifecycle_resolution(
    results: list[LifecycleHookDispatchResult],
) -> TerminalLifecycleResolution:
    resolution = TerminalLifecycleResolution()
    for result in list(results or []):
        output = getattr(result, "output", None)
        to_dict = getattr(output, "to_dict", None)
        output_dict = to_dict() if callable(to_dict) else {}
        if not isinstance(output_dict, dict):
            continue
        message = _lifecycle_terminal_message(output_dict)
        if message and not resolution.user_message:
            resolution.user_message = message
        diagnostics = output_dict.get("diagnostics")
        if isinstance(diagnostics, list):
            resolution.diagnostics.extend(diagnostics)
        artifacts = output_dict.get("artifacts")
        if isinstance(artifacts, list):
            resolution.artifacts.extend(artifacts)
    return resolution


_PERMISSION_LIFECYCLE_STATIC_POLICY_MATCHES = {
    "system_hard_deny",
    "system_hard_policy",
    "mode.tool_whitelist",
    "effective_capabilities",
    "execution_policy:deny",
    "approval_policy:deny",
}

_PERMISSION_LIFECYCLE_STATIC_POLICY_PREFIXES = (
    "agent.",
)


@dataclass
class FollowUpMessage:
    followup_id: str
    text: str


@dataclass
class TerminalLifecycleResolution:
    user_message: str = ""
    diagnostics: list[object] = field(default_factory=list)
    artifacts: list[object] = field(default_factory=list)


@dataclass
class AgentState:
    """State of the agent."""

    messages: list[dict] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cache_read_tokens: int | None = None
    total_cache_write_tokens: int | None = None
    total_cost_usd: float | None = None
    usage_extra: dict = field(default_factory=dict)
    current_round: int = 0


@dataclass
class UserPromptLifecycleResolution:
    user_input: str
    blocked: bool = False
    message: str = ""
    additional_context: list[object] = field(default_factory=list)
    diagnostics: list[object] = field(default_factory=list)
    lifecycle_events: list[AgentEvent] = field(default_factory=list)


class Agent:
    """The main agent class - orchestrates LLM and tools."""

    def __init__(
        self,
        llm: "LLM",
        tools: Optional[List["Tool"]] = None,
        config: "Config" | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        hook_registry: HookRegistry | None = None,
        approval_provider: "ApprovalProvider" | None = None,
        available_modes: dict[str, ModeConfig] | None = None,
        active_mode: str | None = None,
        loop: AgentLoop | None = None,
        executor: ToolExecutor | None = None,
        lifecycle_dispatcher: LifecycleHookDispatcher | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else []
        self.config = config
        self.max_context_tokens = max_context_tokens
        self.max_rounds = max_rounds
        self.runtime_execution_target = "local"

        # Mode state
        self.available_modes: dict[str, ModeConfig] = dict(available_modes or {})
        self.active_mode: str | None = None

        # State
        self.state = AgentState()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._follow_up_lock = threading.Lock()
        self._pending_follow_ups: list[FollowUpMessage] = []
        self._follow_up_consumed_handler: Callable[[FollowUpMessage], None] | None = None

        # Context manager
        context_cfg = getattr(config, "context", None)
        if context_cfg:
            self.context = ContextManager(
                max_tokens=max_context_tokens,
                snip_keep_recent_tools=context_cfg.snip_keep_recent_tools,
                snip_threshold_chars=context_cfg.snip_threshold_chars,
                snip_min_lines=context_cfg.snip_min_lines,
                summarize_keep_recent_turns=context_cfg.summarize_keep_recent_turns,
                token_fudge_factor=getattr(context_cfg, "token_fudge_factor", 1.1),
            )
        else:
            self.context = ContextManager(max_tokens=max_context_tokens)

        # Hook runtime
        self.hook_registry = hook_registry or HookRegistry()
        self.lifecycle_dispatcher = lifecycle_dispatcher

        # Execution components
        self.approval_provider = approval_provider
        if loop is not None:
            self._loop = loop
        else:
            shell = get_platform_info().get_preferred_shell().value
            self._loop = AgentLoop(self, prompt_fn=system_prompt, shell_name=shell)
        self._executor = executor or ToolExecutor(self)

        # Event handlers
        self._event_handlers: List[Callable[[AgentEvent], None]] = []

        # Activate initial mode if available
        if self.available_modes:
            default_mode = active_mode or next(iter(self.available_modes.keys()), None)
            if default_mode in self.available_modes:
                self.active_mode = default_mode

    def _collect_pending_tool_calls(self) -> list[tuple[str, str]]:
        """Collect assistant tool calls that do not yet have matching tool outputs."""
        completed_ids = {
            msg.get("tool_call_id")
            for msg in self.state.messages
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }

        pending: list[tuple[str, str]] = []
        seen: set[str] = set()
        for msg in self.state.messages:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                tc_name = fn.get("name") or "unknown_tool"
                if not tc_id or tc_id in completed_ids or tc_id in seen:
                    continue
                pending.append((tc_id, tc_name))
                seen.add(tc_id)

        return pending

    def reconcile_pending_tool_calls(self, reason: str | None = None) -> int:
        """Append fallback tool outputs for any dangling assistant tool calls.

        Returns the number of synthetic tool results appended.
        """
        pending = self._collect_pending_tool_calls()
        if not pending:
            return 0

        suffix = f" {reason}" if reason else ""
        for tc_id, tc_name in pending:
            if not tc_id:
                continue
            self.state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": f"Tool '{tc_name}' interrupted before returning output.{suffix}",
                }
            )
        return len([tc_id for tc_id, _ in pending if tc_id])

    def request_stop(self) -> None:
        """Request cooperative stop for the current/next agent loop iteration."""
        self._stop_event.set()

    def clear_stop_request(self) -> None:
        """Clear any pending cooperative stop request."""
        self._stop_event.clear()

    def stop_requested(self) -> bool:
        """Return True when cooperative stop has been requested."""
        return self._stop_event.is_set()

    def queue_follow_up(self, followup_id: str, text: str) -> bool:
        """Queue a user follow-up for the next safe LLM boundary."""
        message_text = text.strip()
        ticket_id = followup_id.strip()
        if not ticket_id or not message_text:
            return False
        with self._follow_up_lock:
            if any(item.followup_id == ticket_id for item in self._pending_follow_ups):
                return False
            self._pending_follow_ups.append(
                FollowUpMessage(followup_id=ticket_id, text=message_text)
            )
            return True

    def cancel_follow_up(self, followup_id: str) -> bool:
        """Remove a queued follow-up that has not reached an LLM boundary."""
        ticket_id = followup_id.strip()
        if not ticket_id:
            return False
        with self._follow_up_lock:
            before = len(self._pending_follow_ups)
            self._pending_follow_ups = [
                item for item in self._pending_follow_ups if item.followup_id != ticket_id
            ]
            return len(self._pending_follow_ups) != before

    def consume_follow_ups(self) -> list[FollowUpMessage]:
        """Return and clear queued follow-ups in arrival order."""
        with self._follow_up_lock:
            if not self._pending_follow_ups:
                return []
            items = list(self._pending_follow_ups)
            self._pending_follow_ups = []
        handler = self._follow_up_consumed_handler
        if handler is not None:
            for item in items:
                try:
                    handler(item)
                except Exception:
                    pass
        return items

    def set_follow_up_consumed_handler(
        self,
        handler: Callable[[FollowUpMessage], None] | None,
    ) -> None:
        self._follow_up_consumed_handler = handler

    def get_active_mode_config(self) -> ModeConfig | None:
        """Return active mode config if mode is enabled."""
        if not self.active_mode:
            return None
        return self.available_modes.get(self.active_mode)

    def set_mode(self, mode_name: str) -> None:
        """Switch active mode.

        Raises:
            ValueError: If mode does not exist.
        """
        if mode_name not in self.available_modes:
            raise ValueError(f"Unknown mode: {mode_name}")
        self.active_mode = mode_name

    def get_active_tools(self) -> list["Tool"]:
        """Return tools visible to the LLM in current mode."""
        return [tool for tool in self.tools if self.is_tool_authorized(tool)]

    def get_blocked_tools(self) -> list["Tool"]:
        """Return tools hidden/blocked by current mode."""
        return [tool for tool in self.tools if not self.is_tool_authorized(tool)]

    def capability_tool_policy_enabled(self) -> bool:
        """Return whether effective_capabilities is an active tool boundary."""

        return bool(getattr(self, "enforce_effective_capabilities", False))

    def _effective_capabilities(self) -> dict:
        value = getattr(self, "effective_capabilities", None)
        return value if isinstance(value, dict) else {}

    def _runtime_config(self) -> "Config" | None:
        config = getattr(self, "runtime_config", None)
        if config is not None:
            return config
        return self.config

    def _current_agent_id(self) -> str:
        for attr in ("agent_config_id", "main_agent_id", "runtime_agent_id"):
            value = str(getattr(self, attr, "") or "").strip()
            if value:
                return value
        return ""

    def _current_agent_config(self) -> object | None:
        agent_id = self._current_agent_id()
        config = self._runtime_config()
        registry = getattr(config, "agent_registry", None)
        agents = getattr(registry, "agents", {}) if registry is not None else {}
        if agent_id and isinstance(agents, dict):
            return agents.get(agent_id)
        return None

    def _runtime_profile_for_agent(self, agent_config: object | None) -> dict:
        profile_id = str(
            getattr(agent_config, "runtime_profile", "")
            or getattr(self, "runtime_profile_id", "")
            or ""
        ).strip()
        config = self._runtime_config()
        profiles = getattr(config, "runtime_profiles", {}) if config is not None else {}
        profile_map = getattr(profiles, "profiles", profiles)
        profile = profile_map.get(profile_id) if isinstance(profile_map, dict) else None
        to_dict = getattr(profile, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return dict(profile) if isinstance(profile, dict) else {}

    def _runtime_approval_config(self) -> ApprovalConfig | None:
        runtime_approval = getattr(self, "runtime_approval_config", None)
        if isinstance(runtime_approval, ApprovalConfig):
            return runtime_approval
        config = self._runtime_config()
        base = getattr(config, "approval", None)
        if base is None:
            return None
        session_rules = [
            rule
            for rule in list(getattr(self, "session_approval_rules", []) or [])
            if isinstance(rule, ApprovalRuleConfig)
        ]
        if not session_rules:
            return base
        global_rules = [
            rule
            for rule in base.rules
            if not any(_same_approval_target(rule, session_rule) for session_rule in session_rules)
        ]
        return ApprovalConfig(
            default_mode=base.default_mode,
            rules=[*global_rules, *session_rules],
        )

    def _permission_subject(self) -> PermissionSubject:
        agent_config = self._current_agent_config()
        source = str(getattr(self, "permission_trigger_source", "") or "").strip()
        if not source:
            source = "chat" if str(getattr(self, "main_agent_id", "") or "").strip() else "manual"
        explicit_interactive = getattr(self, "permission_interactive", None)
        interactive = (
            bool(explicit_interactive)
            if explicit_interactive is not None
            else source in {"chat", "manual"}
        )
        return PermissionSubject(
            agent_id=self._current_agent_id(),
            role=str(getattr(agent_config, "role", "") or ""),
            visibility=str(getattr(agent_config, "visibility", "user") or "user"),
            trigger_source=source,
            interactive=interactive,
            runtime_profile_id=str(
                getattr(agent_config, "runtime_profile", "")
                or getattr(self, "runtime_profile_id", "")
                or ""
            ),
            session_id=getattr(self, "current_session_id", None),
            task_id=getattr(self, "runtime_task_id", None),
            workspace_root=getattr(self, "runtime_workspace_root", None),
        )

    def _permission_target_for_tool(self, tool: "Tool") -> PermissionTarget:
        tool_name = str(getattr(tool, "name", "") or "").strip()
        tool_source = str(getattr(tool, "tool_source", "") or "").strip()
        if not tool_source:
            tool_source = "builtin"
        if tool_source == "mcp":
            return PermissionTarget(
                kind="mcp_tool",
                name=tool_name,
                tool_source="mcp",
                mcp_server=str(getattr(tool, "server_name", "") or "") or None,
                mcp_tool=tool_name,
            )
        if tool_source == "environment_requirement":
            return PermissionTarget(
                kind="environment_requirement",
                name=tool_name,
                tool_source="environment_requirement",
                registry_path=f"envreq:executable:{tool_name}" if tool_name else "",
                component_id=f"envreq:executable:{tool_name}" if tool_name else "",
            )
        if tool_source == "skill":
            return PermissionTarget(
                kind="skill",
                name=tool_name,
                tool_source="skill",
                registry_path=f"skill:{tool_name}" if tool_name else "",
                component_id=f"skill:{tool_name}" if tool_name else "",
            )
        return PermissionTarget(
            kind="builtin_tool",
            name=tool_name,
            tool_source="builtin",
            registry_path=f"builtin:{tool_name}" if tool_name else "",
            component_id=f"builtin_tool:{tool_name}" if tool_name else "",
        )

    def _permission_metadata_for_tool(self, tool: "Tool") -> dict:
        mode = self.get_active_mode_config()
        mode_tools = list(getattr(mode, "tools", []) or []) if mode is not None else []
        return {
            "active_mode": self.active_mode or "",
            "mode_tools": mode_tools,
            "suggested_modes": self.suggest_modes_for_tool(
                str(getattr(tool, "name", "") or "")
            ),
        }

    def _dispatch_agent_lifecycle_event(
        self,
        event_name: str,
        *,
        payload: dict,
        metadata: dict | None = None,
        fail_closed_message: str | None = None,
        defer_observations: bool = False,
    ) -> list[LifecycleHookDispatchResult]:
        dispatcher = getattr(self, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return []
        context = build_lifecycle_event_context(
            event_name,
            placement="server",
            session_run_id=str(getattr(self, "current_session_id", "") or ""),
            agent_run_id=str(getattr(self, "runtime_agent_run_id", "") or ""),
            turn_id=str(getattr(self, "runtime_turn_id", "") or ""),
            trigger_source=str(getattr(self, "permission_trigger_source", "") or "chat"),
            origin="agent",
            locale=str(getattr(self, "locale", "") or ""),
            metadata=dict(metadata or {}),
            payload=dict(payload),
        )
        try:
            self._emit_lifecycle_observation(
                "dispatch_start",
                event_name,
                context,
                defer=defer_observations,
            )
            results = list(dispatch(context) or [])
            for result in results:
                output = getattr(result, "output", None)
                if isinstance(output, LifecycleHookOutput):
                    annotate_lifecycle_output_diagnostics(event_name, output)
                self._emit_lifecycle_observation(
                    "result",
                    event_name,
                    context,
                    result=result,
                    defer=defer_observations,
                )
            return results
        except Exception as exc:
            self._emit_lifecycle_observation(
                "dispatch_failed",
                event_name,
                context,
                error=str(exc),
                defer=defer_observations,
            )
            if fail_closed_message:
                return [
                    LifecycleHookDispatchResult(
                        LifecycleHookDeclaration.from_dict(
                            f"hook:system_builtin:{event_name}:dispatch_failed",
                            {
                                "event": event_name,
                                "source": "system_builtin",
                                "placement": "server",
                                "handler_type": "internal",
                                "display_name": "Lifecycle dispatch failure",
                                "summary": fail_closed_message,
                                "permissions": [],
                                "trust": "trusted",
                            },
                        ),
                        LifecycleHookOutput.from_dict(
                            {
                                "continue_flow": False,
                                "decision": "deny",
                                "reason": f"{fail_closed_message}: {exc}",
                                "user_message": fail_closed_message,
                                "diagnostics": [
                                    {
                                        "code": "lifecycle_dispatch_failed",
                                        "message": str(exc),
                                    }
                                ],
                            }
                        ),
                    )
                ]
            return []

    def _emit_lifecycle_observation(
        self,
        phase: str,
        event_name: str,
        context: object,
        *,
        result: object | None = None,
        error: str = "",
        defer: bool = False,
    ) -> None:
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
                message = _lifecycle_terminal_message(output_dict)
                if message:
                    payload["message"] = message
                if output_dict.get("artifacts"):
                    payload["artifacts"] = list(output_dict.get("artifacts") or [])
            event = AgentEvent.lifecycle_hook(payload)
            if defer:
                deferred = getattr(self, "_deferred_user_prompt_lifecycle_events", None)
                if isinstance(deferred, list):
                    deferred.append(event)
                    return
            self._emit_event(event)
        except Exception:
            return

    def _apply_user_prompt_lifecycle(
        self, user_input: str
    ) -> UserPromptLifecycleResolution:
        self._deferred_user_prompt_lifecycle_events = []
        results = self._dispatch_agent_lifecycle_event(
            "UserPromptSubmit",
            payload={"user_input": user_input},
            fail_closed_message="UserPromptSubmit lifecycle dispatch failed",
            defer_observations=True,
        )
        lifecycle_events = list(getattr(self, "_deferred_user_prompt_lifecycle_events", []) or [])
        self._deferred_user_prompt_lifecycle_events = []
        resolved_input = user_input
        additional_context: list[object] = []
        diagnostics: list[object] = []
        ask_reasons: list[str] = []
        ask_hooks: list[dict[str, str]] = []
        blocked_message = ""
        for result in results:
            declaration = getattr(result, "declaration", None)
            output = getattr(result, "output", None)
            if output is None:
                continue
            diagnostics.extend(list(getattr(output, "diagnostics", []) or []))
            additional_context.extend(list(getattr(output, "additional_context", []) or []))
            decision = str(getattr(output, "decision", "") or "none")
            reason = _lifecycle_reason_text(getattr(output, "reason", None))
            user_message = str(getattr(output, "user_message", "") or "").strip()
            if decision == "deny" or getattr(output, "continue_flow", True) is False:
                blocked_message = (
                    user_message
                    or reason
                    or "UserPromptSubmit lifecycle blocked this request."
                )
                break
            if decision == "defer":
                blocked_message = (
                    user_message
                    or reason
                    or "UserPromptSubmit lifecycle deferred this request."
                )
                break
            if decision == "ask":
                ask_reasons.append(
                    user_message
                    or reason
                    or "UserPromptSubmit lifecycle requires approval."
                )
                ask_hooks.append(
                    {
                        "hook_id": str(getattr(declaration, "id", "") or ""),
                        "display_name": str(
                            getattr(declaration, "display_name", "") or ""
                        ),
                    }
                )
            updated_input = getattr(output, "updated_input", None)
            if not isinstance(updated_input, dict):
                continue
            candidate = updated_input.get("user_input")
            if isinstance(candidate, str):
                resolved_input = candidate
        if not blocked_message and ask_reasons:
            approval_message = self._request_user_prompt_lifecycle_approval(
                resolved_input,
                reasons=ask_reasons,
                hooks=ask_hooks,
            )
            if approval_message:
                blocked_message = approval_message
        return UserPromptLifecycleResolution(
            user_input=resolved_input,
            blocked=bool(blocked_message),
            message=blocked_message,
            additional_context=additional_context,
            diagnostics=diagnostics,
            lifecycle_events=lifecycle_events,
        )

    def _apply_terminal_lifecycle_event(
        self,
        event_name: str,
        *,
        payload: dict,
    ) -> TerminalLifecycleResolution:
        results = self._dispatch_agent_lifecycle_event(event_name, payload=payload)
        return _terminal_lifecycle_resolution(results)

    def _request_user_prompt_lifecycle_approval(
        self,
        user_input: str,
        *,
        reasons: list[str],
        hooks: list[dict[str, str]],
    ) -> str:
        provider = self.approval_provider
        if provider is None:
            return "UserPromptSubmit lifecycle requires approval, but no approval provider is configured."
        reason = "\n".join(item for item in reasons if item).strip()
        try:
            decision = provider.request_approval(
                ApprovalRequest(
                    tool_name="lifecycle:UserPromptSubmit",
                    tool_args={"user_input": user_input},
                    tool_source="lifecycle_hook",
                    reason=reason or "UserPromptSubmit lifecycle requires approval.",
                    intent="Review whether this user prompt lifecycle hook may continue the turn.",
                    metadata={
                        "lifecycle_event": "UserPromptSubmit",
                        "lifecycle_hooks": hooks,
                    },
                )
            )
        except (KeyboardInterrupt, EOFError):
            return "UserPromptSubmit lifecycle approval was interrupted."
        return "" if decision.approved else (
            decision.reason
            or "UserPromptSubmit lifecycle approval was denied."
        )

    def _permission_lifecycle_outputs(
        self,
        request: PermissionRequest,
    ) -> list[dict]:
        dispatcher = getattr(self, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return []
        context = build_lifecycle_event_context(
            "PermissionRequest",
            placement="server",
            session_run_id=request.subject.session_id or "",
            agent_run_id=str(getattr(self, "runtime_agent_run_id", "") or ""),
            turn_id=str(getattr(self, "runtime_turn_id", "") or ""),
            trigger_source=request.subject.trigger_source,
            origin="agent",
            locale=str(getattr(self, "locale", "") or ""),
            metadata=dict(request.metadata or {}),
            payload=build_permission_lifecycle_payload(request),
        )
        try:
            self._emit_lifecycle_observation("dispatch_start", "PermissionRequest", context)
            results = dispatch(context)
        except Exception as exc:
            self._emit_lifecycle_observation(
                "dispatch_failed",
                "PermissionRequest",
                context,
                error=str(exc),
            )
            return [
                {
                    "hook_id": "lifecycle:PermissionRequest",
                    "display_name": "Permission lifecycle dispatch",
                    "source": "system_builtin",
                    "decision": "deny",
                    "continue_flow": False,
                    "reason": f"PermissionRequest lifecycle dispatch failed: {exc}",
                    "diagnostics": [
                        {
                            "code": "lifecycle_dispatch_failed",
                            "message": str(exc),
                        }
                    ],
                }
            ]
        output_items = []
        for result in list(results or []):
            if result is None:
                continue
            output = getattr(result, "output", None)
            if isinstance(output, LifecycleHookOutput):
                annotate_lifecycle_output_diagnostics("PermissionRequest", output)
            self._emit_lifecycle_observation(
                "result",
                "PermissionRequest",
                context,
                result=result,
            )
            output_items.append(self._permission_lifecycle_output_item(result))
        return output_items

    @staticmethod
    def _permission_lifecycle_payload(request: PermissionRequest) -> dict:
        return build_permission_lifecycle_payload(request)

    @staticmethod
    def _permission_lifecycle_output_item(
        result: LifecycleHookDispatchResult,
    ) -> dict:
        declaration = getattr(result, "declaration", None)
        output = getattr(result, "output", None)
        to_dict = getattr(output, "to_dict", None)
        output_data = to_dict() if callable(to_dict) else {}
        if not isinstance(output_data, dict):
            output_data = {}
        item = {
            "hook_id": str(getattr(declaration, "id", "") or ""),
            "display_name": str(getattr(declaration, "display_name", "") or ""),
            "source": str(getattr(declaration, "source", "") or ""),
            "decision": str(output_data.get("decision") or "none"),
            "continue_flow": bool(output_data.get("continue_flow", True)),
            "reason": output_data.get("reason"),
            "user_message": str(output_data.get("user_message") or ""),
            "diagnostics": list(output_data.get("diagnostics") or []),
            "additional_context": list(output_data.get("additional_context") or []),
        }
        if output_data.get("updated_input") is not None:
            item["updated_input"] = output_data.get("updated_input")
        if output_data.get("artifacts"):
            item["artifacts"] = list(output_data.get("artifacts") or [])
        return item

    def evaluate_tool_permission(
        self,
        tool: "Tool",
        *,
        tool_call: ToolCall | None = None,
        action: str = "execute",
    ) -> PermissionDecision:
        """Evaluate runtime permission for a tool through the unified gateway."""

        request = self._tool_permission_request(
            tool,
            tool_call=tool_call,
            action=action,
        )
        gateway = PermissionGateway()
        initial_decision = gateway.evaluate(request)
        if not self._should_dispatch_permission_lifecycle(
            request,
            initial_decision,
        ):
            return initial_decision

        lifecycle_outputs = self._permission_lifecycle_outputs(request)
        if not lifecycle_outputs:
            return initial_decision
        return gateway.evaluate(replace(request, lifecycle_outputs=lifecycle_outputs))

    def _tool_permission_request(
        self,
        tool: "Tool",
        *,
        tool_call: ToolCall | None,
        action: str,
    ) -> PermissionRequest:
        agent_config = self._current_agent_config()
        return PermissionRequest(
            subject=self._permission_subject(),
            target=self._permission_target_for_tool(tool),
            action=action,
            tool_call=tool_call,
            effective_capabilities=self._effective_capabilities(),
            approval=self._runtime_approval_config(),
            runtime_profile=self._runtime_profile_for_agent(agent_config),
            agent_config=agent_config,
            enforce_effective_capabilities=self.capability_tool_policy_enabled(),
            metadata=self._permission_metadata_for_tool(tool),
        )

    @staticmethod
    def _should_dispatch_permission_lifecycle(
        request: PermissionRequest,
        initial_decision: PermissionDecision,
    ) -> bool:
        if request.tool_call is None:
            return False
        policy = str(initial_decision.policy_matched or "").strip()
        if policy in _PERMISSION_LIFECYCLE_STATIC_POLICY_MATCHES:
            return False
        return not any(
            policy.startswith(prefix)
            for prefix in _PERMISSION_LIFECYCLE_STATIC_POLICY_PREFIXES
        )

    def is_tool_authorized(self, tool: "Tool") -> bool:
        """Return whether the unified permission gateway allows tool visibility."""

        return self.evaluate_tool_permission(tool).allowed

    def suggest_modes_for_tool(self, tool_name: str) -> list[str]:
        """Return mode names that allow the given tool."""
        suggestions: list[str] = []
        for mode_name, mode in self.available_modes.items():
            if not mode.tools or "*" in mode.tools or tool_name in set(mode.tools):
                suggestions.append(mode_name)
        return suggestions

    def is_tool_allowed_in_mode(self, tool_name: str) -> bool:
        """Return whether a tool can execute in current mode."""
        mode = self.get_active_mode_config()
        if mode is None:
            return True
        if not mode.tools or "*" in mode.tools:
            return True
        return tool_name in set(mode.tools)

    def add_event_handler(self, handler: Callable[[AgentEvent], None]) -> None:
        """Add an event handler."""
        self._event_handlers.append(handler)

    def _emit_event(self, event: AgentEvent) -> None:
        """Emit an event to all handlers."""
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception:
                pass  # Don't let handler errors break execution

    def register_hook(self, hook_point: HookPoint, hook: HookBase[object]) -> None:
        """Register a hook on the agent-scoped hook registry."""
        self.hook_registry.register(hook_point, hook)

    def list_hooks(self, hook_point: HookPoint | None = None) -> dict[str, list[str]]:
        """List registered hooks from the agent-scoped hook registry."""
        return self.hook_registry.list_hooks(hook_point)

    def add_tools(self, tools: List["Tool"]) -> None:
        """Add additional tools."""
        self.tools.extend(tools)

    def get_tool(self, name: str) -> Optional["Tool"]:
        """Look up a tool by name."""
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def chat(self, user_input: str, *, clear_stop_request: bool = True) -> str:
        """Process one user message."""
        if clear_stop_request:
            self.clear_stop_request()

        # Repair stale dangling tool calls (e.g. after previous crash/interruption)
        self.reconcile_pending_tool_calls(
            reason="Recovered from previous interrupted turn."
        )

        prompt_lifecycle = self._apply_user_prompt_lifecycle(user_input)
        user_input = prompt_lifecycle.user_input

        self._emit_event(AgentEvent.session_run_start(user_input))
        for lifecycle_event in prompt_lifecycle.lifecycle_events:
            self._emit_event(lifecycle_event)

        if prompt_lifecycle.blocked:
            message = prompt_lifecycle.message or "UserPromptSubmit lifecycle blocked this request."
            self._emit_event(AgentEvent.error(message))
            self._emit_usage_event("blocked")
            self._emit_event(AgentEvent.session_run_end(message))
            return message

        # Add model-facing messages only after lifecycle guards allow the turn.
        self.state.messages.extend(
            _lifecycle_additional_context_messages(prompt_lifecycle.additional_context)
        )
        self.state.messages.append({"role": "user", "content": user_input})

        # Run the loop
        try:
            result = self._loop.run()
        except BaseException as e:
            # Ensure tool-call/response parity before bubbling the failure upward.
            self.reconcile_pending_tool_calls(
                reason=f"Interrupted due to {type(e).__name__}."
            )
            terminal_lifecycle = self._apply_terminal_lifecycle_event(
                "StopFailure",
                payload={
                    "error": {
                        "type": type(e).__name__,
                        "message": str(e),
                    },
                    "interrupted": False,
                },
            )
            if terminal_lifecycle.user_message:
                self._emit_event(AgentEvent.error(terminal_lifecycle.user_message))
            self._emit_usage_event("error")
            raise

        interrupted = bool(getattr(self._loop, "last_run_interrupted", False))
        self._emit_usage_event(
            "interrupted"
            if interrupted
            else "cancelled" if self.stop_requested() else "done"
        )
        if interrupted:
            payload = getattr(self._loop, "last_interruption_payload", None) or {}
            self._emit_event(AgentEvent.session_run_interrupted(result, payload))
            return result
        self._apply_terminal_lifecycle_event(
            "Stop",
            payload={"result": result, "interrupted": False},
        )
        self._emit_event(
            AgentEvent.session_run_end(
                result,
                render_response=not getattr(
                    self._loop, "last_response_streamed", False
                ),
            )
        )
        return result

    def _emit_usage_event(self, run_status: str) -> None:
        llm = getattr(self, "llm", None)
        self._emit_event(
            AgentEvent.usage_update(
                prompt_tokens=self.state.total_prompt_tokens,
                completion_tokens=self.state.total_completion_tokens,
                context_tokens=self.context.get_context_tokens(self.state.messages),
                context_window=getattr(self.context, "max_tokens", None),
                max_output_tokens=getattr(llm, "max_tokens", None),
                model=getattr(llm, "model", None),
                mode=getattr(self, "active_mode", None),
                cache_read_tokens=getattr(self.state, "total_cache_read_tokens", None),
                cache_write_tokens=getattr(self.state, "total_cache_write_tokens", None),
                cost_usd=getattr(self.state, "total_cost_usd", None),
                usage_extra=getattr(self.state, "usage_extra", None),
                run_status=run_status,
            )
        )

    def reset(self) -> None:
        """Clear conversation history."""
        self.state.messages.clear()
        self.state.total_prompt_tokens = 0
        self.state.total_completion_tokens = 0
        self.state.total_cache_read_tokens = None
        self.state.total_cache_write_tokens = None
        self.state.total_cost_usd = None
        self.state.usage_extra.clear()
        self.state.current_round = 0

    @property
    def messages(self) -> list[dict]:
        """Get messages (for compatibility)."""
        return self.state.messages
