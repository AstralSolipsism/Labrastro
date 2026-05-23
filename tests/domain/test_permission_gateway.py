from __future__ import annotations

from dataclasses import replace

from reuleauxcoder.domain.agent_runtime.models import AgentConfig
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import (
    PermissionAction,
    PermissionGateway,
    PermissionRequest,
    PermissionSubject,
    PermissionTarget,
)


def _subject(**overrides) -> PermissionSubject:
    data = {
        "agent_id": "main_chat",
        "visibility": "user",
        "trigger_source": "chat",
        "interactive": True,
        "runtime_profile_id": "chat",
    }
    data.update(overrides)
    return PermissionSubject(**data)


def _target(**overrides) -> PermissionTarget:
    data = {
        "kind": "builtin_tool",
        "name": "read_file",
        "tool_source": "builtin",
    }
    data.update(overrides)
    return PermissionTarget(**data)


def _request(**overrides) -> PermissionRequest:
    data = {
        "subject": _subject(),
        "target": _target(),
        "tool_call": ToolCall(id="call-1", name="read_file", arguments={}),
        "effective_capabilities": {"tools": ["builtin:read_file"]},
        "approval": ApprovalConfig(default_mode="allow"),
        "enforce_effective_capabilities": True,
    }
    data.update(overrides)
    return PermissionRequest(**data)


def test_builtin_tool_allowed_when_capability_and_policy_allow() -> None:
    decision = PermissionGateway().evaluate(_request())

    assert decision.action == PermissionAction.ALLOW
    assert decision.authorized is True
    assert decision.capability_matched == "builtin:read_file"


def test_builtin_tool_denied_when_missing_from_effective_capabilities() -> None:
    decision = PermissionGateway().evaluate(
        _request(effective_capabilities={"tools": ["builtin:grep"]})
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert "effective_capabilities" in decision.reason


def test_mode_tool_whitelist_is_enforced_by_permission_gateway() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            metadata={
                "active_mode": "review",
                "mode_tools": ["read_file"],
                "suggested_modes": ["coder"],
            },
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
            effective_capabilities={"tools": ["builtin:shell"]},
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "mode.tool_whitelist"
    assert "/mode switch coder" in decision.reason


def test_hard_shell_policy_is_enforced_by_permission_gateway() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            target=_target(name="shell"),
            tool_call=ToolCall(
                id="call-shell",
                name="shell",
                arguments={"command": "rm -rf /tmp/example"},
            ),
            effective_capabilities={"tools": ["builtin:shell"]},
            approval=ApprovalConfig(default_mode="allow"),
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "system_hard_deny"
    assert "shell policy" in decision.reason


def test_mcp_tool_allowed_by_server_or_tool_capability() -> None:
    gateway = PermissionGateway()
    server_decision = gateway.evaluate(
        _request(
            subject=_subject(agent_id="reviewer"),
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="github",
            ),
            tool_call=ToolCall(id="call-mcp", name="search", arguments={}),
            effective_capabilities={"mcp_servers": ["github"], "tools": ["mcp:github"]},
        )
    )
    tool_decision = gateway.evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="docs",
            ),
            tool_call=ToolCall(id="call-mcp-tool", name="search", arguments={}),
            effective_capabilities={"mcp_tools": ["search"]},
        )
    )

    assert server_decision.action == PermissionAction.ALLOW
    assert server_decision.capability_matched == "mcp:github"
    assert tool_decision.action == PermissionAction.ALLOW
    assert tool_decision.capability_matched == "mcp_tool:search"


def test_execution_policy_deny_overrides_approval_allow() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            effective_capabilities={
                "tools": ["builtin:shell"],
                "execution_policies": [
                    {
                        "target": "builtin_tool:shell",
                        "target_type": "capability_component",
                        "kind": "builtin_tool",
                        "policy": "deny",
                    }
                ],
            },
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
            approval=ApprovalConfig(default_mode="allow"),
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "execution_policy:deny"


def test_require_user_policy_becomes_approval_for_chat_and_blocked_review_for_background() -> None:
    request = _request(
        effective_capabilities={
            "tools": ["builtin:shell"],
            "execution_policies": [
                {
                    "target": "builtin_tool:shell",
                    "target_type": "capability_component",
                    "kind": "builtin_tool",
                    "policy": "require_user",
                }
            ],
        },
        target=_target(name="shell"),
        tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
    )

    interactive = PermissionGateway().evaluate(request)
    background = PermissionGateway().evaluate(
        replace(
            request,
            subject=_subject(
                agent_id="worker",
                trigger_source="taskflow",
                interactive=False,
            ),
        )
    )

    assert interactive.action == PermissionAction.REQUIRE_APPROVAL
    assert background.action == PermissionAction.BLOCKED_REVIEW


def test_escalate_execution_policy_warns_and_inherit_defers_to_approval() -> None:
    escalated = PermissionGateway().evaluate(
        _request(
            effective_capabilities={
                "tools": ["builtin:shell"],
                "execution_policies": [
                    {"target": "builtin_tool:shell", "policy": "escalate"}
                ],
            },
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
            approval=ApprovalConfig(default_mode="allow"),
        )
    )
    inherited = PermissionGateway().evaluate(
        _request(
            effective_capabilities={
                "tools": ["builtin:write_file"],
                "execution_policies": [
                    {"target": "builtin_tool:write_file", "policy": "inherit"}
                ],
            },
            target=_target(name="write_file"),
            tool_call=ToolCall(id="call-write", name="write_file", arguments={}),
            approval=ApprovalConfig(default_mode="deny"),
        )
    )

    assert escalated.action == PermissionAction.WARN
    assert escalated.warning
    assert inherited.action == PermissionAction.DENY
    assert inherited.policy_matched == "approval_policy:deny"


def test_approval_allow_prevents_runtime_profile_default_from_requiring_approval() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            approval=ApprovalConfig(default_mode="allow"),
            runtime_profile={"approval_mode": "full"},
        )
    )

    assert decision.action == PermissionAction.ALLOW
    assert decision.policy_matched == "approval_policy:allow"


def test_approval_require_approval_never_waits_in_background() -> None:
    interactive = PermissionGateway().evaluate(
        _request(
            approval=ApprovalConfig(
                default_mode="allow",
                rules=[ApprovalRuleConfig(tool_name="shell", action="require_approval")],
            ),
            effective_capabilities={"tools": ["builtin:shell"]},
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
        )
    )
    background = PermissionGateway().evaluate(
        _request(
            subject=_subject(agent_id="worker", trigger_source="taskflow", interactive=False),
            approval=ApprovalConfig(
                default_mode="allow",
                rules=[ApprovalRuleConfig(tool_name="shell", action="require_approval")],
            ),
            effective_capabilities={"tools": ["builtin:shell"]},
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
        )
    )

    assert interactive.action == PermissionAction.REQUIRE_APPROVAL
    assert background.action == PermissionAction.BLOCKED_REVIEW


def test_internal_agent_only_allows_declared_system_flow() -> None:
    agent = AgentConfig.from_dict(
        "capability_packager",
        {
            "visibility": "internal",
            "system_flow_only": ["capability_ingest"],
            "capability_refs": ["environment"],
        },
    )

    allowed = PermissionGateway().evaluate_agent_invocation(
        agent,
        source="capability_ingest",
        interactive=False,
    )
    denied = PermissionGateway().evaluate_agent_invocation(
        agent,
        source="taskflow",
        interactive=False,
    )

    assert allowed.action == PermissionAction.ALLOW
    assert denied.action == PermissionAction.DENY
    assert "system flow" in denied.reason
