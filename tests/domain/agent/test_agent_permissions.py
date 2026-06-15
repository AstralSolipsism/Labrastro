from __future__ import annotations

from types import SimpleNamespace

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent_runtime.models import AgentConfig
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    ApprovalConfig,
    Config,
    ModeConfig,
)
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookEventContext,
    LifecycleHookOutput,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import PermissionAction


class _Tool:
    description = ""
    parameters = {}
    tool_source = "builtin"

    def __init__(self, name: str) -> None:
        self.name = name


class _RecordingPermissionDispatcher:
    def __init__(self) -> None:
        self.contexts: list[LifecycleHookEventContext] = []
        self.declaration = LifecycleHookDeclaration.from_dict(
            "hook:observe-permission",
            {
                "event": "PermissionRequest",
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "command",
                "display_name": "Permission observer",
                "summary": "Observe permission requests.",
                "permissions": [],
                "trust": "trusted",
            },
        )

    def dispatch(
        self,
        context: LifecycleHookEventContext,
    ) -> list[LifecycleHookDispatchResult]:
        self.contexts.append(context)
        return []


def _builtin_tool_name(target_ref: str) -> str:
    return target_ref.split(":", 1)[-1]


def _effective_builtin_tools(
    *target_refs: str,
    execution_policies: list[dict] | None = None,
) -> dict:
    return {
        "builtin_tool_grants": [_builtin_tool_name(ref) for ref in target_refs],
        "tool_specs": [],
        "execution_policies": list(execution_policies or []),
    }


def test_agent_tool_visibility_uses_permission_gateway_for_mode_and_capabilities() -> None:
    config = Config(
        approval=ApprovalConfig(default_mode="allow"),
        agent_registry=AgentRegistryConfig(
            agents={
                "main_chat": AgentConfig.from_dict(
                    "main_chat",
                    {"runtime_profile": "chat", "capability_refs": []},
                )
            }
        ),
    )
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("read_file"), _Tool("shell")],
        config=config,
        available_modes={
            "review": ModeConfig(name="review", tools=["read_file"]),
        },
        active_mode="review",
    )
    setattr(agent, "runtime_config", config)
    setattr(agent, "agent_config_id", "main_chat")
    setattr(
        agent,
        "effective_capabilities",
        _effective_builtin_tools("builtin:read_file", "builtin:shell"),
    )
    setattr(agent, "enforce_effective_capabilities", True)

    assert [tool.name for tool in agent.get_active_tools()] == ["read_file"]
    assert [tool.name for tool in agent.get_blocked_tools()] == ["shell"]


def test_agent_tool_visibility_does_not_dispatch_permission_request_lifecycle() -> None:
    dispatcher = _RecordingPermissionDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("read_file"), _Tool("shell")],
        config=config,
        available_modes={"review": ModeConfig(name="review", tools=["read_file"])},
        active_mode="review",
        lifecycle_dispatcher=dispatcher,
    )

    assert [tool.name for tool in agent.get_active_tools()] == ["read_file"]
    assert [tool.name for tool in agent.get_blocked_tools()] == ["shell"]
    assert dispatcher.contexts == []


def test_background_agent_with_approval_provider_returns_blocked_review() -> None:
    config = Config(approval=ApprovalConfig(default_mode="require_approval"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        approval_provider=object(),
    )
    setattr(agent, "permission_trigger_source", "taskflow")
    setattr(agent, "permission_interactive", False)

    decision = agent.evaluate_tool_permission(_Tool("shell"))

    assert decision.action == PermissionAction.BLOCKED_REVIEW


def test_background_permission_sources_are_non_interactive_by_default() -> None:
    config = Config(approval=ApprovalConfig(default_mode="require_approval"))

    for source in ("taskflow", "environment", "capability_ingest"):
        agent = Agent(
            llm=SimpleNamespace(),
            tools=[_Tool("shell")],
            config=config,
            approval_provider=object(),
        )
        setattr(agent, "permission_trigger_source", source)

        decision = agent.evaluate_tool_permission(_Tool("shell"))

        assert decision.action == PermissionAction.BLOCKED_REVIEW


def test_chat_agent_requires_approval_in_interactive_context() -> None:
    config = Config(approval=ApprovalConfig(default_mode="require_approval"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
    )
    setattr(agent, "permission_trigger_source", "chat")

    decision = agent.evaluate_tool_permission(_Tool("shell"))

    assert decision.action == PermissionAction.REQUIRE_APPROVAL


def test_manual_agent_requires_approval_by_default() -> None:
    config = Config(approval=ApprovalConfig(default_mode="require_approval"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
    )

    decision = agent.evaluate_tool_permission(_Tool("shell"))

    assert decision.action == PermissionAction.REQUIRE_APPROVAL


def test_agent_dispatches_permission_request_lifecycle_for_candidate_tool_call() -> None:
    class _LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []
            self.declaration = LifecycleHookDeclaration.from_dict(
                "hook:block-shell",
                {
                    "event": "PermissionRequest",
                    "source": "admin_managed",
                    "placement": "server",
                    "handler_type": "command",
                    "display_name": "Shell guard",
                    "summary": "Require lifecycle review before shell tools run.",
                    "permissions": [],
                    "trust": "trusted",
                },
            )

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="deny",
                        reason="shell blocked by lifecycle",
                    ),
                )
            ]

    dispatcher = _LifecycleDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "chat")
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "effective_capabilities", _effective_builtin_tools("builtin:shell"))
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "lifecycle_hook:deny"
    assert dispatcher.contexts[0].event_name == "PermissionRequest"
    assert dispatcher.contexts[0].placement == "server"
    assert dispatcher.contexts[0].session_run_id == "session-1"
    technical = dispatcher.contexts[0].payload["technical"]
    assert technical["subject"]["trigger_source"] == "chat"
    assert technical["target"]["name"] == "shell"
    assert technical["tool_call"]["name"] == "shell"


def test_agent_does_not_dispatch_permission_request_for_hard_denied_tool_call() -> None:
    dispatcher = _RecordingPermissionDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "chat")

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "rm -rf /"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "system_hard_deny"
    assert [context.event_name for context in dispatcher.contexts] == ["PermissionDenied"]
    assert dispatcher.contexts[0].payload["technical"]["permission_decision"][
        "policy_matched"
    ] == "system_hard_deny"


def test_agent_dispatches_permission_denied_lifecycle_after_static_denial() -> None:
    class _LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []
            self.declaration = LifecycleHookDeclaration.from_dict(
                "hook:permission-denied-feedback",
                {
                    "event": "PermissionDenied",
                    "source": "admin_managed",
                    "placement": "server",
                    "handler_type": "internal",
                    "display_name": "Permission denied feedback",
                    "summary": "Suggests recoverable next steps after automatic denial.",
                    "permissions": [],
                    "trust": "trusted",
                },
            )

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            if context.event_name != "PermissionDenied":
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput.from_dict(
                        {
                            "decision": "allow",
                            "user_message": "Use read_file or ask for shell capability.",
                            "diagnostics": [{"code": "recoverable_permission_denied"}],
                        }
                    ),
                )
            ]

    dispatcher = _LifecycleDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "chat")
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "runtime_agent_run_id", "agent-run-1")
    setattr(agent, "runtime_turn_id", "turn-1")
    setattr(agent, "effective_capabilities", _effective_builtin_tools("builtin:read_file"))
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.policy_matched == "effective_capabilities"
    assert [context.event_name for context in dispatcher.contexts] == [
        "PermissionDenied"
    ]
    context = dispatcher.contexts[0]
    assert context.session_run_id == "session-1"
    assert context.agent_run_id == "agent-run-1"
    assert context.turn_id == "turn-1"
    assert context.source == "chat"
    assert context.payload["tool_names"] == ["shell"]
    assert context.payload["tool_call_ids"] == ["call-shell"]
    assert context.payload["technical"]["permission_decision"]["policy_matched"] == (
        "effective_capabilities"
    )
    assert decision.audit["permission_denied_lifecycle"][0]["hook_id"] == (
        "hook:permission-denied-feedback"
    )
    assert decision.audit["permission_denied_lifecycle"][0]["user_message"] == (
        "Use read_file or ask for shell capability."
    )


def test_agent_does_not_dispatch_permission_request_for_effective_capability_boundary() -> None:
    dispatcher = _RecordingPermissionDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "chat")
    setattr(agent, "effective_capabilities", _effective_builtin_tools("builtin:read_file"))
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "effective_capabilities"
    assert [context.event_name for context in dispatcher.contexts] == ["PermissionDenied"]
    assert dispatcher.contexts[0].payload["technical"]["permission_decision"][
        "policy_matched"
    ] == "effective_capabilities"


def test_agent_does_not_dispatch_permission_request_for_execution_policy_deny() -> None:
    dispatcher = _RecordingPermissionDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "chat")
    setattr(
        agent,
        "effective_capabilities",
        _effective_builtin_tools(
            "builtin:shell",
            execution_policies=[
                {"target": "builtin_tool:shell", "policy": "deny"},
            ],
        ),
    )
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "execution_policy:deny"
    assert [context.event_name for context in dispatcher.contexts] == ["PermissionDenied"]
    assert dispatcher.contexts[0].payload["technical"]["permission_decision"][
        "policy_matched"
    ] == "execution_policy:deny"


def test_agent_dispatches_permission_request_for_background_review_candidate() -> None:
    dispatcher = _RecordingPermissionDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "taskflow")
    setattr(agent, "permission_interactive", False)
    setattr(
        agent,
        "effective_capabilities",
        _effective_builtin_tools(
            "builtin:shell",
            execution_policies=[
                {"target": "builtin_tool:shell", "policy": "require_user"},
            ],
        ),
    )
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.BLOCKED_REVIEW
    assert decision.policy_matched == "execution_policy:require_user"
    assert len(dispatcher.contexts) == 1
    assert dispatcher.contexts[0].event_name == "PermissionRequest"


def test_permission_request_lifecycle_exposes_standard_tool_matcher_fields() -> None:
    class _LifecycleDispatcher:
        def __init__(self) -> None:
            self.contexts: list[LifecycleHookEventContext] = []
            self.declaration = LifecycleHookDeclaration.from_dict(
                "hook:admin:shell-permission:PermissionRequest:0",
                {
                    "event": "PermissionRequest",
                    "source": "admin_managed",
                    "placement": "server",
                    "handler_type": "command",
                    "display_name": "Shell guard",
                    "summary": "Blocks shell permission requests.",
                    "permissions": [],
                    "trust": "trusted",
                    "matcher": {"tool_names": "shell"},
                },
            )

        def dispatch(
            self,
            context: LifecycleHookEventContext,
        ) -> list[LifecycleHookDispatchResult]:
            self.contexts.append(context)
            assert context.payload["event_name"] == "PermissionRequest"
            assert context.payload["placement"] == "server"
            assert context.payload["tool_names"] == ["shell"]
            assert context.payload["tool_call_ids"] == ["call-shell"]
            assert context.payload["tool_sources"] == ["builtin"]
            assert context.payload["mcp_servers"] == []
            assert context.payload["trigger_source"] == "chat"
            assert context.payload["session_run_id"] == "session-1"
            assert context.payload["agent_run_id"] == "agent-run-1"
            assert context.payload["turn_id"] == "turn-1"
            if "shell" not in context.payload["tool_names"]:
                return []
            return [
                LifecycleHookDispatchResult(
                    declaration=self.declaration,
                    output=LifecycleHookOutput(
                        decision="deny",
                        reason="shell blocked by standard matcher",
                    ),
                )
            ]

    dispatcher = _LifecycleDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "chat")
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "runtime_agent_run_id", "agent-run-1")
    setattr(agent, "runtime_turn_id", "turn-1")
    setattr(agent, "effective_capabilities", _effective_builtin_tools("builtin:shell"))
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.reason == "shell blocked by standard matcher"
    assert dispatcher.contexts[0].payload["technical"]["tool_call"]["name"] == "shell"
    for legacy_field in (
        "tool_" + "name",
        "tool_" + "call_id",
        "tool_" + "source",
        "mcp_" + "server",
    ):
        assert legacy_field not in dispatcher.contexts[0].payload


def test_permission_request_lifecycle_context_populates_authoritative_fields() -> None:
    dispatcher = _RecordingPermissionDispatcher()
    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "permission_trigger_source", "taskflow")
    setattr(agent, "active_mode", "coder")
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "runtime_agent_run_id", "agent-run-1")
    setattr(agent, "runtime_turn_id", "turn-1")
    setattr(agent, "locale", "zh-CN")
    setattr(agent, "effective_capabilities", _effective_builtin_tools("builtin:shell"))
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.ALLOW
    context = dispatcher.contexts[0]
    assert context.event_name == "PermissionRequest"
    assert context.session_run_id == "session-1"
    assert context.agent_run_id == "agent-run-1"
    assert context.turn_id == "turn-1"
    assert context.source == "taskflow"
    assert context.origin == "agent"
    assert context.locale == "zh-CN"
    assert context.placement == "server"
    assert context.timestamp
    assert context.metadata["round_index"] == 0
    assert context.metadata["active_mode"] == agent.active_mode
    assert context.payload["session_run_id"] == "session-1"
    assert context.payload["agent_run_id"] == "agent-run-1"
    assert context.payload["turn_id"] == "turn-1"
    assert context.payload["trigger_source"] == "taskflow"
    assert context.payload["timestamp"] == context.timestamp


def test_agent_fails_closed_when_permission_request_lifecycle_dispatch_fails() -> None:
    class _FailingLifecycleDispatcher:
        def dispatch(self, context: LifecycleHookEventContext):  # noqa: ARG002
            raise RuntimeError("hook runtime crashed")

    config = Config(approval=ApprovalConfig(default_mode="allow"))
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[_Tool("shell")],
        config=config,
        lifecycle_dispatcher=_FailingLifecycleDispatcher(),
    )
    setattr(agent, "permission_trigger_source", "chat")
    setattr(agent, "effective_capabilities", _effective_builtin_tools("builtin:shell"))
    setattr(agent, "enforce_effective_capabilities", True)

    decision = agent.evaluate_tool_permission(
        _Tool("shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.policy_matched == "lifecycle_hook:deny"
    assert "PermissionRequest lifecycle dispatch failed" in decision.reason
    assert decision.audit["lifecycle_hooks"][0]["diagnostics"][0]["code"] == (
        "lifecycle_dispatch_failed"
    )
