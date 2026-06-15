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


_STALE_BUILTIN_SOURCE_TYPE = "builtin_" + "tool"


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


def _tool_spec(
    target_tool_ref: str,
    *,
    source_type: str = "mcp_tool",
    name: str | None = None,
    policy: str = "inherit",
) -> dict:
    tool_name = name or target_tool_ref.split(":", 1)[-1]
    tool_id = f"capability:test:{source_type}:{tool_name}"
    if source_type == "mcp_tool" and target_tool_ref.startswith("mcp:"):
        tool_id = f"capability:test:{target_tool_ref}"
    return {
        "tool_id": tool_id,
        "name": tool_name,
        "namespace": "capability",
        "target_tool_ref": target_tool_ref,
        "source_type": source_type,
        "exposure": "deferred",
        "permission": {"policy": policy},
    }


def _effective_tool_specs(*specs: dict) -> dict:
    return {"tool_specs": list(specs)}


def _effective_builtin_tools(
    *names: str,
    execution_policies: list[dict] | None = None,
) -> dict:
    return {
        "builtin_tool_grants": list(names),
        "tool_specs": [],
        "execution_policies": list(execution_policies or []),
    }


def _request(**overrides) -> PermissionRequest:
    data = {
        "subject": _subject(),
        "target": _target(),
        "tool_call": ToolCall(id="call-1", name="read_file", arguments={}),
        "effective_capabilities": _effective_builtin_tools("read_file"),
        "approval": ApprovalConfig(default_mode="allow"),
        "enforce_effective_capabilities": True,
    }
    data.update(overrides)
    return PermissionRequest(**data)


def test_builtin_tool_allowed_when_capability_and_policy_allow() -> None:
    decision = PermissionGateway().evaluate(_request())

    assert decision.action == PermissionAction.ALLOW
    assert decision.authorized is True
    assert decision.capability_matched == "builtin_tool:read_file"


def test_stale_builtin_tool_spec_does_not_authorize_builtin_tool() -> None:
    stale_tool_id = (
        f"capability:review:{_STALE_BUILTIN_SOURCE_TYPE}:read_file"
    )
    decision = PermissionGateway().evaluate(
        _request(
            effective_capabilities={
                "tool_specs": [
                    {
                        "tool_id": stale_tool_id,
                        "name": "read_file",
                        "namespace": "capability",
                        "target_tool_ref": "builtin:read_file",
                        "source_type": _STALE_BUILTIN_SOURCE_TYPE,
                        "exposure": "deferred",
                        "permission": {"policy": "allow"},
                    }
                ]
            }
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.capability_matched == ""


def test_legacy_tools_string_does_not_authorize_builtin_tool() -> None:
    decision = PermissionGateway().evaluate(
        _request(effective_capabilities={"tools": ["builtin:read_file"]})
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert "effective_capabilities" in decision.reason


def test_builtin_tool_denied_when_missing_from_effective_capabilities() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            effective_capabilities=_effective_builtin_tools("grep")
        )
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
            effective_capabilities=_effective_builtin_tools("shell"),
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
            effective_capabilities=_effective_builtin_tools("shell"),
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
            effective_capabilities={"mcp_servers": ["github"], "tool_specs": []},
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
            effective_capabilities=_effective_tool_specs(
                _tool_spec(
                    "mcp:docs:search",
                    source_type="mcp_tool",
                    name="search",
                )
            ),
        )
    )

    assert server_decision.action == PermissionAction.ALLOW
    assert server_decision.capability_matched == "mcp:github"
    assert tool_decision.action == PermissionAction.ALLOW
    assert tool_decision.capability_matched == "capability:test:mcp:docs:search"


def test_mcp_tool_allowed_by_exact_mcp_tools_grant() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="docs",
                mcp_tool="search",
            ),
            tool_call=ToolCall(id="call-docs-search", name="search", arguments={}),
            effective_capabilities={"mcp_tools": ["mcp:docs:search"]},
        )
    )

    assert decision.action == PermissionAction.ALLOW
    assert decision.capability_matched == "mcp:docs:search"


def test_mcp_server_scope_does_not_authorize_other_servers() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="docs",
                mcp_tool="search",
            ),
            tool_call=ToolCall(id="call-mcp-docs", name="search", arguments={}),
            effective_capabilities={"mcp_servers": ["github"], "tool_specs": []},
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.capability_matched == ""


def test_mcp_tool_spec_matches_server_scoped_tool_identity_only() -> None:
    gateway = PermissionGateway()
    effective_capabilities = _effective_tool_specs(
        _tool_spec(
            "mcp:docs:search",
            source_type="mcp_tool",
            name="search",
        )
    )

    docs_decision = gateway.evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="docs",
                mcp_tool="search",
            ),
            tool_call=ToolCall(id="call-docs-search", name="search", arguments={}),
            effective_capabilities=effective_capabilities,
        )
    )
    github_decision = gateway.evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="github",
                mcp_tool="search",
            ),
            tool_call=ToolCall(id="call-github-search", name="search", arguments={}),
            effective_capabilities=effective_capabilities,
        )
    )

    assert docs_decision.action == PermissionAction.ALLOW
    assert docs_decision.capability_matched == "capability:test:mcp:docs:search"
    assert github_decision.action == PermissionAction.DENY
    assert github_decision.authorized is False
    assert github_decision.capability_matched == ""


def test_mcp_tool_without_server_does_not_authorize_server_scoped_tool() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_server="github",
                mcp_tool="search",
            ),
            tool_call=ToolCall(id="call-github-search", name="search", arguments={}),
            effective_capabilities=_effective_tool_specs(
                _tool_spec(
                    "mcp:docs:search",
                    source_type="mcp_tool",
                    name="search",
                )
            ),
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.capability_matched == ""


def test_mcp_tool_without_server_is_denied() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            target=PermissionTarget(
                kind="mcp_tool",
                name="search",
                tool_source="mcp",
                mcp_tool="search",
            ),
            tool_call=ToolCall(id="call-unscoped-search", name="search", arguments={}),
            effective_capabilities=_effective_tool_specs(
                _tool_spec(
                    "mcp:docs:search",
                    source_type="mcp_tool",
                    name="search",
                )
            ),
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.capability_matched == ""


def test_mcp_tool_user_review_policy_blocks_background_runs() -> None:
    request = _request(
        subject=_subject(
            agent_id="capability_packager",
            trigger_source="capability_ingest",
            interactive=False,
        ),
        target=PermissionTarget(
            kind="mcp_tool",
            name="search",
            tool_source="mcp",
            mcp_server="github",
        ),
        tool_call=ToolCall(id="call-mcp", name="search", arguments={}),
        effective_capabilities={
            "mcp_servers": ["github"],
            "tool_specs": [],
            "execution_policies": [
                {
                    "target": "mcp:github",
                    "target_type": "capability_component",
                    "kind": "mcp",
                    "policy": "require_user",
                }
            ],
        },
        approval=ApprovalConfig(default_mode="allow"),
    )

    decision = PermissionGateway().evaluate(request)

    assert decision.action == PermissionAction.BLOCKED_REVIEW
    assert decision.authorized is False
    assert decision.capability_matched == "mcp:github"
    assert decision.policy_matched == "execution_policy:require_user"
    assert decision.audit["mcp_server"] == "github"


def test_execution_policy_deny_overrides_approval_allow() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            effective_capabilities=_effective_builtin_tools(
                "shell",
                execution_policies=[
                    {
                        "target": "builtin_tool:shell",
                        "target_type": "capability_component",
                        "kind": "builtin_tool",
                        "policy": "deny",
                    }
                ],
            ),
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
            approval=ApprovalConfig(default_mode="allow"),
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.policy_matched == "execution_policy:deny"


def test_lifecycle_allow_cannot_override_gateway_deny() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            effective_capabilities=_effective_builtin_tools("grep"),
            lifecycle_outputs=[
                {
                    "hook_id": "hook:allow-read",
                    "display_name": "Allow read",
                    "decision": "allow",
                    "reason": "lifecycle wants to allow",
                }
            ],
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert "effective_capabilities" in decision.reason


def test_lifecycle_deny_is_resolved_by_permission_gateway() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            lifecycle_outputs=[
                {
                    "hook_id": "hook:block-sensitive-read",
                    "display_name": "Sensitive read guard",
                    "decision": "deny",
                    "reason": "blocked by lifecycle policy",
                }
            ],
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.reason == "blocked by lifecycle policy"
    assert decision.policy_matched == "lifecycle_hook:deny"
    assert decision.audit["lifecycle_hooks"][0]["hook_id"] == "hook:block-sensitive-read"


def test_lifecycle_defer_is_resolved_by_permission_gateway() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            lifecycle_outputs=[
                {
                    "hook_id": "hook:defer-read",
                    "display_name": "Deferred read guard",
                    "decision": "defer",
                    "reason": "read_file deferred by lifecycle policy",
                }
            ],
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.reason == "read_file deferred by lifecycle policy"
    assert decision.policy_matched == "lifecycle_hook:defer"
    assert decision.audit["lifecycle_hooks"][0]["hook_id"] == "hook:defer-read"


def test_lifecycle_continue_flow_false_is_resolved_by_permission_gateway() -> None:
    decision = PermissionGateway().evaluate(
        _request(
            lifecycle_outputs=[
                {
                    "hook_id": "hook:stop-read",
                    "display_name": "Stopped read guard",
                    "decision": "allow",
                    "continue_flow": False,
                    "reason": "read_file stopped by lifecycle policy",
                }
            ],
        )
    )

    assert decision.action == PermissionAction.DENY
    assert decision.authorized is False
    assert decision.reason == "read_file stopped by lifecycle policy"
    assert decision.policy_matched == "lifecycle_hook:deny"
    assert decision.audit["lifecycle_hooks"][0]["hook_id"] == "hook:stop-read"


def test_lifecycle_ask_uses_existing_interactive_and_background_review_boundary() -> None:
    request = _request(
        lifecycle_outputs=[
            {
                "hook_id": "hook:ask-before-shell",
                "display_name": "Shell review",
                "decision": "ask",
                "reason": "shell requires lifecycle review",
            }
        ],
        effective_capabilities=_effective_builtin_tools("shell"),
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
    assert interactive.policy_matched == "lifecycle_hook:ask"
    assert background.action == PermissionAction.BLOCKED_REVIEW
    assert background.policy_matched == "lifecycle_hook:ask"


def test_require_user_policy_becomes_approval_for_chat_and_blocked_review_for_background() -> None:
    request = _request(
        effective_capabilities=_effective_builtin_tools(
            "shell",
            execution_policies=[
                {
                    "target": "builtin_tool:shell",
                    "target_type": "capability_component",
                    "kind": "builtin_tool",
                    "policy": "require_user",
                }
            ],
        ),
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
            effective_capabilities=_effective_builtin_tools(
                "shell",
                execution_policies=[
                    {"target": "builtin_tool:shell", "policy": "escalate"}
                ],
            ),
            target=_target(name="shell"),
            tool_call=ToolCall(id="call-shell", name="shell", arguments={"command": "pwd"}),
            approval=ApprovalConfig(default_mode="allow"),
        )
    )
    inherited = PermissionGateway().evaluate(
        _request(
            effective_capabilities=_effective_builtin_tools(
                "apply_patch",
                execution_policies=[
                    {"target": "builtin_tool:apply_patch", "policy": "inherit"}
                ],
            ),
            target=_target(name="apply_patch"),
            tool_call=ToolCall(id="call-write", name="apply_patch", arguments={}),
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
            effective_capabilities=_effective_builtin_tools("shell"),
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
            effective_capabilities=_effective_builtin_tools("shell"),
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
