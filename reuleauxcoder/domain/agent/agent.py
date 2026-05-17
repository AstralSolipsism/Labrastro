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
from reuleauxcoder.domain.config.models import ModeConfig
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.hooks import HookBase, HookPoint, HookRegistry
from reuleauxcoder.infrastructure.platform import get_platform_info
from reuleauxcoder.services.prompt.builder import system_prompt


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
        mode = self.get_active_mode_config()
        if mode is None:
            return self.tools

        if not mode.tools or "*" in mode.tools:
            return self.tools

        allowed = set(mode.tools)
        return [tool for tool in self.tools if tool.name in allowed]

    def get_blocked_tools(self) -> list["Tool"]:
        """Return tools hidden/blocked by current mode."""
        mode = self.get_active_mode_config()
        if mode is None or not mode.tools or "*" in mode.tools:
            return []
        allowed = set(mode.tools)
        return [tool for tool in self.tools if tool.name not in allowed]

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

        self._emit_event(AgentEvent.chat_start(user_input))

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

        self._emit_usage_event(
            "cancelled" if self.stop_requested() else "done"
        )
        self._emit_event(
            AgentEvent.chat_end(
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
