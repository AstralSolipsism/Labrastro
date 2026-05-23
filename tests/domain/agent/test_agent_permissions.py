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
from reuleauxcoder.domain.permission_gateway import PermissionAction


class _Tool:
    description = ""
    parameters = {}
    tool_source = "builtin"

    def __init__(self, name: str) -> None:
        self.name = name


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
    setattr(agent, "effective_capabilities", {"tools": ["builtin:read_file", "builtin:shell"]})
    setattr(agent, "enforce_effective_capabilities", True)

    assert [tool.name for tool in agent.get_active_tools()] == ["read_file"]
    assert [tool.name for tool in agent.get_blocked_tools()] == ["shell"]


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
