"""Core agent - the main agent class."""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, List
from dataclasses import dataclass, field
import threading

if TYPE_CHECKING:
    from reuleauxcoder.domain.approval import ApprovalProvider
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.extensions.tools.base import Tool
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig, ModeConfig
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.hooks import HookBase, HookPoint, HookRegistry
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


@dataclass
class FollowUpMessage:
    followup_id: str
    text: str


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

    def evaluate_tool_permission(
        self,
        tool: "Tool",
        *,
        tool_call: ToolCall | None = None,
        action: str = "execute",
    ) -> PermissionDecision:
        """Evaluate runtime permission for a tool through the unified gateway."""

        agent_config = self._current_agent_config()
        return PermissionGateway().evaluate(
            PermissionRequest(
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

        self._emit_event(AgentEvent.session_run_start(user_input))

        # Add user message
        self.state.messages.append({"role": "user", "content": user_input})

        # Run the loop
        try:
            result = self._loop.run()
        except BaseException as e:
            # Ensure tool-call/response parity before bubbling the failure upward.
            self.reconcile_pending_tool_calls(
                reason=f"Interrupted due to {type(e).__name__}."
            )
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
