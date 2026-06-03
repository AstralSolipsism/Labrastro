from __future__ import annotations

from pathlib import Path

import pytest

from reuleauxcoder.domain.agent_runtime.models import (
    CapabilityComponentConfig,
    CapabilityPackageConfig,
)
from reuleauxcoder.domain.config.models import (
    Config,
    MCPServerConfig,
    SkillRegistrationConfig,
    SkillsConfig,
)
from reuleauxcoder.domain.hooks.lifecycle import (
    LIFECYCLE_HOOK_EVENTS,
    LIFECYCLE_HOOK_HANDLER_TYPES,
    LIFECYCLE_HOOK_MATCHER_FIELDS,
    LIFECYCLE_HOOK_PLACEMENTS,
    LIFECYCLE_HOOK_SOURCES,
    LIFECYCLE_HOOK_TRUST_STATES,
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookEventContext,
    LifecycleHookOutput,
    LifecycleHookRegistry,
    build_lifecycle_event_context,
    build_permission_lifecycle_payload,
    build_tool_batch_lifecycle_payload,
    build_tool_lifecycle_payload,
    lifecycle_declarations_from_config_hooks,
    lifecycle_registry_from_config,
    system_builtin_lifecycle_declarations_from_hook_specs,
)


def _declaration_payload(**overrides):
    payload = {
        "event": "PreToolUse",
        "source": "capability_package",
        "placement": "server",
        "handler_type": "mcp_tool",
        "handler_ref": "mcp__audit__record",
        "matcher": {"tool_names": "deploy"},
        "permissions": ["audit.write"],
        "display_name": "Record deployment result",
        "summary": "Writes deployment results to audit after a deploy tool succeeds.",
        "trust": "pending_review",
        "risk_level": "medium",
        "technical": {"raw": {"handler_ref": "mcp__audit__record"}},
    }
    payload.update(overrides)
    return payload


def test_lifecycle_hook_declaration_roundtrip_preserves_public_contract() -> None:
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:capability-package:deploy-audit",
        _declaration_payload(),
    )

    assert declaration.id == "hook:capability-package:deploy-audit"
    assert declaration.event == "PreToolUse"
    assert declaration.source == "capability_package"
    assert declaration.placement == "server"
    assert declaration.handler_type == "mcp_tool"
    assert declaration.handler_ref == "mcp__audit__record"
    assert declaration.permissions == ["audit.write"]
    assert declaration.display_name == "Record deployment result"
    assert declaration.summary.startswith("Writes deployment results")
    assert declaration.trust == "pending_review"
    assert declaration.to_dict() == {
        "id": "hook:capability-package:deploy-audit",
        **_declaration_payload(),
        "owner_id": "",
        "owner_enabled": True,
        "owner_status": "installed",
    }


def test_lifecycle_hook_declaration_rejects_missing_user_facing_fields() -> None:
    for field in ["display_name", "summary", "placement", "permissions"]:
        payload = _declaration_payload()
        payload.pop(field)

        with pytest.raises(ValueError, match=field):
            LifecycleHookDeclaration.from_dict("hook:missing-field", payload)


def test_lifecycle_hook_declaration_rejects_invalid_schema_values() -> None:
    invalid_cases = [
        ("event", "before_tool_execute"),
        ("source", "python_plugin"),
        ("placement", "local"),
        ("placement", "local_peer"),
        ("handler_type", "python_class"),
        ("trust", "allowed"),
    ]

    for field, value in invalid_cases:
        with pytest.raises(ValueError, match=field):
            LifecycleHookDeclaration.from_dict(
                f"hook:invalid:{field}:{value}",
                _declaration_payload(**{field: value}),
            )


def test_lifecycle_hook_declaration_rejects_unknown_and_legacy_matcher_fields() -> None:
    with pytest.raises(ValueError, match="matcher"):
        LifecycleHookDeclaration.from_dict(
            "hook:invalid:matcher",
            _declaration_payload(matcher={"tool": {"name": "deploy"}}),
        )

    with pytest.raises(ValueError, match="unknown_field"):
        LifecycleHookDeclaration.from_dict(
            "hook:invalid:unknown-matcher",
            _declaration_payload(matcher={"unknown_field": "deploy"}),
        )

    for legacy_field in (
        "tool_" + "name",
        "tool_" + "call_id",
        "tool_" + "source",
        "mcp_" + "server",
    ):
        with pytest.raises(ValueError, match=legacy_field):
            LifecycleHookDeclaration.from_dict(
                f"hook:invalid:{legacy_field}",
                _declaration_payload(matcher={legacy_field: "deploy"}),
            )


def test_lifecycle_hook_declaration_restricts_internal_handlers_to_builtin_sources() -> None:
    with pytest.raises(ValueError, match="internal"):
        LifecycleHookDeclaration.from_dict(
            "hook:capability-package:internal",
            _declaration_payload(handler_type="internal", handler_ref="memory_context"),
        )

    declaration = LifecycleHookDeclaration.from_dict(
        "hook:system:memory-context",
        _declaration_payload(
            source="system_builtin",
            handler_type="internal",
            handler_ref="memory_context",
            trust="trusted",
            display_name="Inject private memory",
            summary="Adds scoped memory context before model requests.",
            permissions=[],
        ),
    )

    assert declaration.source == "system_builtin"
    assert declaration.handler_type == "internal"
    assert declaration.trust == "trusted"


def test_lifecycle_hook_authoritative_constants_match_adr_boundaries() -> None:
    assert {"UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse"}.issubset(
        LIFECYCLE_HOOK_EVENTS
    )
    assert LIFECYCLE_HOOK_PLACEMENTS == {"server", "peer", "both"}
    assert "local" not in LIFECYCLE_HOOK_PLACEMENTS
    assert "local_peer" not in LIFECYCLE_HOOK_PLACEMENTS
    assert {"capability_package", "skill", "mcp_server", "system_builtin"}.issubset(
        LIFECYCLE_HOOK_SOURCES
    )
    assert "memory_provider" not in LIFECYCLE_HOOK_SOURCES
    assert {"command", "http", "mcp_tool", "prompt", "agent", "internal"}.issubset(
        LIFECYCLE_HOOK_HANDLER_TYPES
    )
    assert LIFECYCLE_HOOK_TRUST_STATES == {
        "pending_review",
        "trusted",
        "disabled",
        "blocked",
    }
    assert {
        "tool_names",
        "tool_call_ids",
        "tool_sources",
        "mcp_servers",
    }.issubset(LIFECYCLE_HOOK_MATCHER_FIELDS)
    assert not {
        "tool_" + "name",
        "tool_" + "call_id",
        "tool_" + "source",
        "mcp_" + "server",
    }.intersection(LIFECYCLE_HOOK_MATCHER_FIELDS)


def test_lifecycle_hook_tests_do_not_reintroduce_legacy_tool_matcher_fields() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    scanned_paths = [
        repo_root / "reuleauxcoder" / "domain" / "hooks" / "lifecycle.py",
        repo_root / "tests" / "domain" / "hooks" / "test_lifecycle.py",
        repo_root / "tests" / "domain" / "agent" / "test_agent_permissions.py",
        repo_root / "tests" / "domain" / "agent" / "test_tool_execution.py",
        repo_root / "tests" / "labrastro_server" / "services" / "test_admin_service.py",
        repo_root / "tests" / "labrastro_server" / "services" / "test_capability_packages.py",
    ]
    legacy_name = "tool_" + "name"
    legacy_call_id = "tool_" + "call_id"
    forbidden_patterns = [
        f'"matcher": {{"{legacy_name}"',
        f"matcher={{{legacy_name!r}",
        f'payload["{legacy_name}"]',
        f'"matcher": {{"{legacy_call_id}"',
        f"matcher={{{legacy_call_id!r}",
        f'payload["{legacy_call_id}"]',
    ]

    for path in scanned_paths:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern not in text, f"{pattern} found in {path}"


def test_lifecycle_hook_config_paths_do_not_silently_strip_view_fields() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    scanned_paths = [
        repo_root / "reuleauxcoder" / "domain" / "hooks" / "lifecycle.py",
        repo_root / "labrastro_server" / "services" / "admin" / "service.py",
    ]
    forbidden_patterns = [
        'raw.pop("id"',
        'raw.pop("source"',
        'raw.pop("owner_id"',
        'item.pop("hook_views"',
    ]

    for path in scanned_paths:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern not in text, f"{pattern} found in {path}"


def test_lifecycle_hook_output_roundtrip_keeps_full_updated_input() -> None:
    output = LifecycleHookOutput.from_dict({
        "continue_flow": False,
        "decision": "deny",
        "reason": {"code": "unsafe_target"},
        "user_message": "Deployment target is not trusted.",
        "additional_context": [{"role": "system", "content": "Use staging only."}],
        "updated_input": {"target": "staging", "force": False},
        "diagnostics": [{"level": "warning", "message": "blocked production"}],
        "artifacts": [{"artifact_id": "artifact-1"}],
    })

    assert output.continue_flow is False
    assert output.decision == "deny"
    assert output.updated_input == {"target": "staging", "force": False}
    assert output.to_dict()["updated_input"] == {"target": "staging", "force": False}


def test_lifecycle_hook_output_rejects_partial_updated_input_patch() -> None:
    with pytest.raises(ValueError, match="updated_input"):
        LifecycleHookOutput.from_dict({"updated_input": [{"op": "replace", "path": "/target"}]})


def test_lifecycle_hook_registry_queries_by_event_source_placement_and_trust() -> None:
    registry = LifecycleHookRegistry()
    registry.register_dict("hook:deploy-audit", _declaration_payload(trust="trusted"))
    registry.register_dict(
        "hook:both",
        _declaration_payload(
            source="system_builtin",
            placement="both",
            handler_type="internal",
            handler_ref="both",
            trust="trusted",
        ),
    )
    registry.register_dict(
        "hook:skill-review",
        _declaration_payload(
            event="PostToolUse",
            source="skill",
            placement="peer",
            handler_type="command",
            handler_ref="python review.py",
            trust="pending_review",
            display_name="Review tool output",
            summary="Checks tool output after a Skill runs.",
            permissions=["process.spawn"],
        ),
    )

    assert [item.id for item in registry.query(event="PreToolUse")] == [
        "hook:deploy-audit",
        "hook:both",
    ]
    assert [item.id for item in registry.query(source="skill")] == ["hook:skill-review"]
    assert [item.id for item in registry.query(placement="server")] == [
        "hook:deploy-audit",
        "hook:both",
    ]
    assert [item.id for item in registry.query(placement="peer")] == [
        "hook:both",
        "hook:skill-review",
    ]
    assert [item.id for item in registry.query(trust="trusted")] == [
        "hook:deploy-audit",
        "hook:both",
    ]


def test_lifecycle_hook_registry_executable_hooks_include_only_trusted_hooks() -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:trusted",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="trusted",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:pending",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="pending",
                trust="pending_review",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:disabled",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="disabled",
                trust="disabled",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:blocked",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="blocked",
                trust="blocked",
            ),
        ),
    ])

    assert [item.id for item in registry.executable(event="PreToolUse")] == ["hook:trusted"]
    assert registry.executable(event="PostToolUse") == []


def test_lifecycle_hook_registry_executable_requires_enabled_owner() -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:trusted-enabled",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="enabled",
                trust="trusted",
                owner_id="enabled-skill",
                owner_enabled=True,
                owner_status="installed",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:trusted-disabled-owner",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="disabled-owner",
                trust="trusted",
                owner_id="disabled-skill",
                owner_enabled=False,
                owner_status="installed",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:trusted-stopped-owner",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="stopped-owner",
                trust="trusted",
                owner_id="stopped-package",
                owner_enabled=True,
                owner_status="stopped",
            ),
        ),
    ])

    assert [item.id for item in registry.executable(event="PreToolUse")] == [
        "hook:trusted-enabled"
    ]
    dashboard = {item["id"]: item for item in registry.dashboard_items()}
    assert dashboard["hook:trusted-disabled-owner"]["trust"] == "trusted"
    assert dashboard["hook:trusted-disabled-owner"]["executable"] is False
    assert dashboard["hook:trusted-disabled-owner"]["enabled"] is False
    assert dashboard["hook:trusted-disabled-owner"]["owner_id"] == "disabled-skill"
    assert dashboard["hook:trusted-disabled-owner"]["owner_enabled"] is False
    assert dashboard["hook:trusted-stopped-owner"]["owner_status"] == "stopped"
    assert dashboard["hook:trusted-stopped-owner"]["executable"] is False


def test_lifecycle_hook_registry_rejects_duplicate_ids() -> None:
    registry = LifecycleHookRegistry()
    declaration = LifecycleHookDeclaration.from_dict("hook:duplicate", _declaration_payload())
    registry.register(declaration)

    with pytest.raises(ValueError, match="duplicate"):
        registry.register(declaration)


def test_lifecycle_hook_registry_dashboard_items_separate_technical_details() -> None:
    registry = LifecycleHookRegistry()
    registry.register_dict("hook:deploy-audit", _declaration_payload(trust="trusted"))

    items = registry.dashboard_items()

    assert items == [{
        "id": "hook:deploy-audit",
        "event": "PreToolUse",
        "source": "capability_package",
        "owner_id": "",
        "owner_enabled": True,
        "owner_status": "installed",
        "placement": "server",
        "handler_type": "mcp_tool",
        "display_name": "Record deployment result",
        "summary": "Writes deployment results to audit after a deploy tool succeeds.",
        "trust": "trusted",
        "enabled": False,
        "executable": False,
        "can_manage": True,
        "unavailable_reason": "handler_unavailable:mcp_tool",
        "placement_runtime": {
            "server": {
                "executable": False,
                "unavailable_reason": "handler_unavailable:mcp_tool",
            },
        },
        "permissions": ["audit.write"],
        "risk_level": "medium",
        "technical": {
            "handler_ref": "mcp__audit__record",
            "matcher": {"tool_names": "deploy"},
            "raw": {"handler_ref": "mcp__audit__record"},
        },
    }]
    assert "handler_ref" not in {
        key for key in items[0].keys() if key != "technical"
    }


def test_lifecycle_hook_dashboard_executable_requires_handler_runtime() -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:prompt",
            _declaration_payload(
                handler_type="prompt",
                handler_ref="skill://rewrite",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:internal",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="memory_context",
                trust="trusted",
                permissions=[],
            ),
        ),
    ])

    items = {item["id"]: item for item in registry.dashboard_items()}

    assert registry.executable(event="PreToolUse") == [
        registry.query(event="PreToolUse", source="system_builtin")[0]
    ]
    assert items["hook:prompt"]["trust"] == "trusted"
    assert items["hook:prompt"]["executable"] is False
    assert items["hook:prompt"]["unavailable_reason"] == "handler_unavailable:prompt"
    assert items["hook:internal"]["executable"] is True
    assert items["hook:internal"]["unavailable_reason"] == ""


def test_system_builtin_lifecycle_declarations_wrap_existing_hook_specs() -> None:
    from reuleauxcoder.domain.hooks.discovery import discover_hook_specs

    declarations = system_builtin_lifecycle_declarations_from_hook_specs(
        discover_hook_specs()
    )
    by_ref = {declaration.handler_ref: declaration for declaration in declarations}

    assert by_ref["MemoryContextHook"].event == "UserPromptSubmit"
    assert by_ref["MemoryContextHook"].source == "system_builtin"
    assert by_ref["MemoryContextHook"].handler_type == "internal"
    assert by_ref["MemoryContextHook"].trust == "trusted"
    assert by_ref["MemoryContextHook"].technical["old_hook_point"] == "before_llm_request"
    assert by_ref["ToolOutputTruncationHook"].event == "PostToolUse"
    assert by_ref["ProjectContextStartupNotifier"].event == "SessionStart"
    assert all(declaration.placement == "server" for declaration in declarations)

    registry = LifecycleHookRegistry(declarations)
    assert "MemoryContextHook" in {
        declaration.handler_ref
        for declaration in registry.executable(event="UserPromptSubmit")
    }


def test_lifecycle_dispatcher_runs_only_trusted_matching_hooks() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict("hook:trusted", _declaration_payload(trust="trusted")),
        LifecycleHookDeclaration.from_dict(
            "hook:pending",
            _declaration_payload(handler_ref="pending", trust="pending_review"),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:other-tool",
            _declaration_payload(
                handler_ref="other",
                matcher={"tool_names": "destroy"},
                trust="trusted",
            ),
        ),
    ])
    dispatcher = LifecycleHookDispatcher(
        registry,
        handlers={
            "mcp_tool": lambda declaration, _context: (
                calls.append(declaration.id)
                or LifecycleHookOutput.from_dict({"decision": "allow"})
            )
        },
    )

    results = dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PreToolUse",
            placement="server",
            payload={"tool_names": ["deploy"]},
        )
    )

    assert calls == ["hook:trusted"]
    assert [result.declaration.id for result in results] == ["hook:trusted"]
    assert results[0].output.decision == "allow"


def test_lifecycle_dispatcher_matches_standard_context_fields_only() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:tool-name",
            _declaration_payload(trust="trusted", matcher={"tool_names": "deploy"}),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:mcp-server",
            _declaration_payload(
                handler_ref="mcp",
                trust="trusted",
                matcher={"mcp_servers": ["github", "gitlab"]},
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:tool-list",
            _declaration_payload(
                handler_ref="tool-list",
                trust="trusted",
                matcher={"tool_names": ["write_file", "deploy"]},
            ),
        ),
    ])
    dispatcher = LifecycleHookDispatcher(
        registry,
        handlers={
            "mcp_tool": lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        },
    )

    dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PreToolUse",
            placement="server",
            payload={
                "tool_names": ["deploy"],
                "mcp_servers": ["github"],
                "tool": {"name": "not-deploy"},
                "tool_call": {"name": "not-deploy"},
            },
        )
    )

    assert calls == ["hook:tool-name", "hook:mcp-server", "hook:tool-list"]


def test_lifecycle_dispatcher_runs_both_on_server_and_reports_peer_side_separately() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:both",
            _declaration_payload(
                source="system_builtin",
                placement="both",
                handler_type="internal",
                handler_ref="both",
                trust="trusted",
            ),
        )
    ])
    dispatcher = LifecycleHookDispatcher(
        registry,
        handlers={
            "internal": lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        },
    )

    dashboard = registry.dashboard_items()[0]
    assert dashboard["executable"] is True
    assert dashboard["unavailable_reason"] == ""
    assert dashboard["placement_runtime"]["server"] == {
        "executable": True,
        "unavailable_reason": "",
    }
    assert dashboard["placement_runtime"]["peer"] == {
        "executable": False,
        "unavailable_reason": "peer_runtime_unavailable",
    }

    server_results = dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PreToolUse",
            placement="server",
            payload={"tool_names": ["deploy"]},
        )
    )
    peer_results = dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PreToolUse",
            placement="peer",
            payload={"tool_names": ["deploy"]},
        )
    )

    assert [item.declaration.id for item in server_results] == ["hook:both"]
    assert peer_results == []
    assert calls == ["hook:both"]


def test_lifecycle_dispatcher_reports_missing_handler_as_diagnostic_output() -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict("hook:trusted", _declaration_payload(trust="trusted"))
    ])
    dispatcher = LifecycleHookDispatcher(registry, handlers={})

    results = dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PreToolUse",
            placement="server",
            payload={"tool_names": ["deploy"]},
        )
    )

    assert len(results) == 1
    assert results[0].declaration.id == "hook:trusted"
    assert results[0].output.continue_flow is True
    assert results[0].output.diagnostics == [{
        "code": "handler_unavailable",
        "handler_type": "mcp_tool",
    }]


def test_lifecycle_builders_emit_list_tool_fields_only() -> None:
    tool = type(
        "_Tool",
        (),
        {
            "name": "shell",
            "description": "Run shell commands.",
            "parameters": {},
            "tool_source": "builtin",
            "server_name": "",
        },
    )()
    tool_call = type(
        "_ToolCall",
        (),
        {"id": "call-shell", "name": "shell", "arguments": {"command": "pwd"}},
    )()

    payload = build_tool_lifecycle_payload(
        "PreToolUse",
        tool_call=tool_call,
        tool=tool,
        tool_source="builtin",
    )

    assert payload["tool_names"] == ["shell"]
    assert payload["tool_call_ids"] == ["call-shell"]
    assert payload["tool_sources"] == ["builtin"]
    assert payload["mcp_servers"] == []
    assert set(payload) == {
        "tool_names",
        "tool_call_ids",
        "tool_sources",
        "mcp_servers",
        "technical",
    }
    for legacy_field in (
        "tool_" + "name",
        "tool_" + "call_id",
        "tool_" + "source",
        "mcp_" + "server",
    ):
        assert legacy_field not in payload
    assert payload["technical"]["tool_call"]["name"] == "shell"
    assert payload["technical"]["tool"]["name"] == "shell"


def test_lifecycle_permission_builder_emits_list_tool_fields_only() -> None:
    request = type(
        "_Request",
        (),
        {
            "tool_call": type(
                "_ToolCall",
                (),
                {"id": "call-shell", "name": "shell", "arguments": {"command": "pwd"}},
            )(),
            "target": type(
                "_Target",
                (),
                {"name": "shell", "tool_source": "builtin", "mcp_server": ""},
            )(),
            "subject": type(
                "_Subject",
                (),
                {
                    "trigger_source": "chat",
                    "session_id": "session-1",
                    "agent_id": "agent",
                    "role": "",
                    "visibility": "user",
                    "interactive": True,
                    "runtime_profile_id": "",
                    "task_id": "",
                    "workspace_root": "",
                },
            )(),
            "action": "execute",
            "effective_capabilities": {},
            "runtime_profile": {},
            "metadata": {},
        },
    )()

    payload = build_permission_lifecycle_payload(request)

    assert payload["tool_names"] == ["shell"]
    assert payload["tool_call_ids"] == ["call-shell"]
    assert payload["tool_sources"] == ["builtin"]
    assert payload["mcp_servers"] == []
    assert set(payload) == {
        "tool_names",
        "tool_call_ids",
        "tool_sources",
        "mcp_servers",
        "technical",
    }
    for legacy_field in (
        "tool_" + "name",
        "tool_" + "call_id",
        "tool_" + "source",
        "mcp_" + "server",
    ):
        assert legacy_field not in payload
    assert payload["technical"]["tool_call"]["name"] == "shell"
    assert payload["technical"]["target"]["name"] == "shell"


def test_lifecycle_event_context_payload_cannot_override_authoritative_fields() -> None:
    context = build_lifecycle_event_context(
        "PreToolUse",
        placement="server",
        trigger_source="chat",
        session_run_id="session-authority",
        agent_run_id="agent-authority",
        turn_id="turn-authority",
        payload={
            "event_name": "Stop",
            "placement": "peer",
            "trigger_source": "settings",
            "session_run_id": "session-payload",
            "agent_run_id": "agent-payload",
            "turn_id": "turn-payload",
            "tool_names": ["deploy"],
        },
    )

    assert context.event_name == "PreToolUse"
    assert context.placement == "server"
    assert context.source == "chat"
    assert context.session_run_id == "session-authority"
    assert context.agent_run_id == "agent-authority"
    assert context.turn_id == "turn-authority"
    assert context.payload["event_name"] == "PreToolUse"
    assert context.payload["placement"] == "server"
    assert context.payload["trigger_source"] == "chat"
    assert context.payload["session_run_id"] == "session-authority"
    assert context.payload["agent_run_id"] == "agent-authority"
    assert context.payload["turn_id"] == "turn-authority"


def test_lifecycle_matcher_ignores_technical_details_even_when_names_match() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:deploy",
            _declaration_payload(trust="trusted", matcher={"tool_names": "deploy"}),
        )
    ])
    dispatcher = LifecycleHookDispatcher(
        registry,
        handlers={
            "mcp_tool": lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        },
    )

    dispatcher.dispatch(
        build_lifecycle_event_context(
            "PreToolUse",
            placement="server",
            payload={
                "tool_names": ["read_file"],
                "technical": {
                    "tool": {"name": "deploy"},
                    "tool_call": {"name": "deploy"},
                },
            },
        )
    )

    assert calls == []


def test_lifecycle_batch_tool_matcher_uses_same_list_semantics() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:batch-read",
            _declaration_payload(
                event="PostToolBatch",
                trust="trusted",
                matcher={"tool_names": "read_file"},
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:batch-write",
            _declaration_payload(
                event="PostToolBatch",
                handler_ref="write",
                trust="trusted",
                matcher={"tool_names": ["write_file", "edit_file"]},
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:batch-missing",
            _declaration_payload(
                event="PostToolBatch",
                handler_ref="missing",
                trust="trusted",
                matcher={"tool_names": "shell"},
            ),
        ),
    ])
    dispatcher = LifecycleHookDispatcher(
        registry,
        handlers={
            "mcp_tool": lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        },
    )

    dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PostToolBatch",
            placement="server",
            payload=build_tool_batch_lifecycle_payload(
                tool_calls=[
                    type("_ToolCall", (), {"id": "call-a", "name": "read_file", "arguments": {}})(),
                    type("_ToolCall", (), {"id": "call-b", "name": "write_file", "arguments": {}})(),
                ],
                results=["a", "b"],
                tool_sources=["builtin", "builtin"],
            ),
        )
    )

    assert calls == ["hook:batch-read", "hook:batch-write"]


def test_lifecycle_declarations_from_config_hooks_default_to_pending_review() -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="code-review",
        source="skill",
        hooks=[
            {
                "event": "UserPromptSubmit",
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "skills/code-review/SKILL.md",
                "display_name": "Code review prompt context",
                "summary": "Adds code review context when prompts match.",
                "permissions": [],
            }
        ],
    )

    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.id == "hook:skill:code-review:UserPromptSubmit:0"
    assert declaration.source == "skill"
    assert declaration.trust == "pending_review"
    assert declaration.handler_ref == "skills/code-review/SKILL.md"


def test_lifecycle_declarations_from_config_hooks_reject_view_and_unknown_fields() -> None:
    base_hook = {
        "event": "UserPromptSubmit",
        "placement": "server",
        "handler_type": "prompt",
        "handler_ref": "skills/code-review/SKILL.md",
        "display_name": "Code review prompt context",
        "summary": "Adds code review context when prompts match.",
        "permissions": [],
    }
    forbidden_fields = {
        "id": "author-supplied-id",
        "source": "skill",
        "owner_id": "code-review",
        "owner_enabled": True,
        "owner_status": "installed",
        "enabled": True,
        "executable": True,
        "can_manage": True,
        "unavailable_reason": "",
        "hook_views": [],
        "unexpected": "value",
    }

    for field, value in forbidden_fields.items():
        with pytest.raises(ValueError, match=field):
            lifecycle_declarations_from_config_hooks(
                owner_id="code-review",
                source="skill",
                hooks=[{**base_hook, field: value}],
            )


def test_lifecycle_declarations_from_config_hooks_reject_protocol_fields_inside_technical() -> None:
    base_hook = {
        "event": "UserPromptSubmit",
        "placement": "server",
        "handler_type": "prompt",
        "handler_ref": "skills/code-review/SKILL.md",
        "display_name": "Code review prompt context",
        "summary": "Adds code review context when prompts match.",
        "permissions": [],
    }

    for field in (
        "matcher",
        "handler_ref",
        "id",
        "source",
        "owner_id",
        "executable",
        "placement_runtime",
    ):
        with pytest.raises(ValueError, match=field):
            lifecycle_declarations_from_config_hooks(
                owner_id="code-review",
                source="skill",
                hooks=[{**base_hook, "technical": {field: "value"}}],
            )


def test_lifecycle_declarations_from_config_hooks_do_not_trust_raw_source() -> None:
    with pytest.raises(ValueError, match="source"):
        lifecycle_declarations_from_config_hooks(
            owner_id="code-review",
            source="skill",
            hooks=[
                {
                    "event": "PreToolUse",
                    "source": "system_builtin",
                    "placement": "server",
                    "handler_type": "internal",
                    "handler_ref": "MemoryContextHook",
                    "display_name": "Forged internal hook",
                    "summary": "A Skill must not be able to become a system hook.",
                    "permissions": [],
                    "trust": "trusted",
                }
            ],
        )


def test_build_lifecycle_event_context_sets_canonical_timestamp() -> None:
    context = build_lifecycle_event_context(
        "PreToolUse",
        placement="server",
        payload={"timestamp": "caller-supplied"},
    )

    assert context.timestamp
    assert context.timestamp.endswith("Z")
    assert context.payload["timestamp"] == context.timestamp
    assert context.payload["timestamp"] != "caller-supplied"


def test_lifecycle_declarations_from_config_hooks_reject_internal_capability_handler() -> None:
    with pytest.raises(ValueError, match="internal handlers"):
        lifecycle_declarations_from_config_hooks(
            owner_id="pack",
            source="capability_package",
            hooks=[
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "internal",
                    "handler_ref": "UnsafeInternalHook",
                    "display_name": "Unsafe hook",
                    "summary": "Should not be accepted from a capability package.",
                    "permissions": [],
                }
            ],
        )


def test_lifecycle_registry_from_config_collects_skill_mcp_package_and_component_hooks() -> None:
    config = type(
        "_Config",
        (),
        {
            "skills": SkillsConfig(
                items={
                    "code-review": SkillRegistrationConfig(
                        name="code-review",
                        hooks=[
                            {
                                "event": "UserPromptSubmit",
                                "handler_type": "prompt",
                                "handler_ref": "skills/code-review/SKILL.md",
                                "display_name": "Code review prompt context",
                                "summary": "Adds code review context.",
                                "permissions": [],
                            }
                        ],
                    )
                }
            ),
            "mcp_servers": [
                MCPServerConfig(
                    name="github",
                    command="github-mcp-server",
                    hooks=[
                        {
                            "event": "PostToolUse",
                            "handler_type": "mcp_tool",
                            "handler_ref": "github.audit",
                            "display_name": "GitHub audit",
                            "summary": "Records GitHub MCP tool results.",
                            "permissions": ["audit.write"],
                        }
                    ],
                )
            ],
            "capability_packages": {
                "review": CapabilityPackageConfig(
                    id="review",
                    hooks=[
                        {
                            "event": "SessionStart",
                            "handler_type": "prompt",
                            "handler_ref": "package:review/session-start",
                            "display_name": "Review package startup",
                            "summary": "Adds review startup context.",
                            "permissions": [],
                        }
                    ],
                )
            },
            "capability_components": {
                "skill:package-review": CapabilityComponentConfig(
                    id="skill:package-review",
                    kind="skill",
                    name="package-review",
                    hooks=[
                        {
                            "event": "PreToolUse",
                            "handler_type": "agent",
                            "handler_ref": "agent:review-policy",
                            "display_name": "Package review policy",
                            "summary": "Checks package review tool use.",
                            "permissions": [],
                        }
                    ],
                )
            },
        },
    )()

    registry = lifecycle_registry_from_config(config)
    items = {item["id"]: item for item in registry.dashboard_items()}

    assert items["hook:skill:code-review:UserPromptSubmit:0"]["source"] == "skill"
    assert items["hook:mcp_server:github:PostToolUse:0"]["source"] == "mcp_server"
    assert items["hook:capability_package:review:SessionStart:0"]["source"] == (
        "capability_package"
    )
    assert items["hook:skill:skill:package-review:PreToolUse:0"]["source"] == "skill"
    assert all(item["trust"] == "pending_review" for item in items.values())
    assert registry.executable(event="PostToolUse", placement="server") == []


def test_lifecycle_registry_from_config_marks_disabled_owners_non_executable() -> None:
    hook = {
        "event": "PreToolUse",
        "handler_type": "agent",
        "handler_ref": "agent:policy",
        "display_name": "Policy",
        "summary": "Checks tool use.",
        "permissions": [],
        "trust": "trusted",
    }
    config = type(
        "_Config",
        (),
        {
            "skills": SkillsConfig(
                items={
                    "disabled-skill": SkillRegistrationConfig(
                        name="disabled-skill",
                        enabled=False,
                        hooks=[hook],
                    )
                }
            ),
            "mcp_servers": [
                MCPServerConfig(
                    name="disabled-mcp",
                    command="mcp",
                    enabled=False,
                    hooks=[hook],
                )
            ],
            "capability_packages": {
                "stopped-package": CapabilityPackageConfig(
                    id="stopped-package",
                    status="stopped",
                    hooks=[hook],
                )
            },
            "capability_components": {
                "skill:stopped-component": CapabilityComponentConfig(
                    id="skill:stopped-component",
                    kind="skill",
                    name="stopped-component",
                    status="stopped",
                    hooks=[hook],
                )
            },
        },
    )()

    registry = lifecycle_registry_from_config(config)

    assert registry.executable(event="PreToolUse", placement="server") == []
    items = {item["id"]: item for item in registry.dashboard_items()}
    assert items["hook:skill:disabled-skill:PreToolUse:0"]["owner_enabled"] is False
    assert items["hook:mcp_server:disabled-mcp:PreToolUse:0"]["owner_enabled"] is False
    assert items["hook:capability_package:stopped-package:PreToolUse:0"]["owner_status"] == "stopped"
    assert items["hook:skill:skill:stopped-component:PreToolUse:0"]["owner_status"] == "stopped"
    assert all(item["executable"] is False for item in items.values())
    assert items["hook:skill:disabled-skill:PreToolUse:0"]["unavailable_reason"] == (
        "owner_disabled"
    )
    assert items["hook:capability_package:stopped-package:PreToolUse:0"][
        "unavailable_reason"
    ] == "owner_status:stopped"


def test_config_validate_rejects_invalid_lifecycle_hooks_before_agent_runtime() -> None:
    config = Config(
        skills=SkillsConfig(
            items={
                "code-review": SkillRegistrationConfig(
                    name="code-review",
                    hooks=[
                        {
                            "event": "UserPromptSubmit",
                            "placement": "server",
                            "handler_type": "prompt",
                            "display_name": "Code review prompt context",
                            "summary": "Adds code review context.",
                            "permissions": [],
                            "matcher": {"tool": {"name": "shell"}},
                        }
                    ],
                )
            }
        )
    )

    assert any("skills.code-review.hooks" in error and "matcher" in error for error in config.validate())
