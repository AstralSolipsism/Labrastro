from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
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
    FunctionLifecycleHookRuntimeAdapter,
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
    LifecycleHookRuntimeAdapterRegistry,
    annotate_lifecycle_output_diagnostics,
    bind_lifecycle_runtime_adapters_to_agent,
    build_lifecycle_event_context,
    build_permission_lifecycle_payload,
    build_tool_batch_lifecycle_payload,
    build_tool_lifecycle_payload,
    default_lifecycle_hook_catalog_runtime_adapters,
    lifecycle_declarations_from_config_hooks,
    lifecycle_registry_from_config,
    default_lifecycle_hook_runtime_adapters,
    system_builtin_lifecycle_declarations_from_hook_registry,
    system_builtin_lifecycle_declarations_from_hook_specs,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision


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


def _runtime_adapters(**handlers):
    registry = LifecycleHookRuntimeAdapterRegistry()
    for handler_type, handler in handlers.items():
        registry.register(
            FunctionLifecycleHookRuntimeAdapter(
                handler_type=handler_type,
                handler=handler,
            )
        )
    return registry


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


def test_lifecycle_output_diagnostics_mark_unsupported_event_fields() -> None:
    output = LifecycleHookOutput.from_dict({
        "continue_flow": False,
        "decision": "deny",
        "reason": "stop",
        "user_message": "Stop",
        "additional_context": [{"role": "system", "content": "ignored"}],
        "updated_input": {"result": "rewritten", "extra": True},
        "artifacts": [{"artifact_id": "artifact-1"}],
    })

    annotate_lifecycle_output_diagnostics("PostToolUse", output)

    diagnostics = output.to_dict()["diagnostics"]
    ignored_fields = {
        item["field"]
        for item in diagnostics
        if item["code"] == "lifecycle_output_field_ignored"
    }
    assert ignored_fields == {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "additional_context",
        "artifacts",
    }
    assert {
        item["field"]
        for item in diagnostics
        if item["code"] == "lifecycle_updated_input_field_ignored"
    } == {"extra"}


def test_diagnostics_only_lifecycle_events_ignore_control_fields() -> None:
    output = LifecycleHookOutput.from_dict({
        "continue_flow": False,
        "decision": "deny",
        "reason": "try another round",
        "user_message": "Retry",
        "additional_context": [{"role": "system", "content": "ignored"}],
        "updated_input": {"result": "rewritten"},
        "artifacts": [{"artifact_id": "artifact-1"}],
        "diagnostics": [{"code": "observed"}],
    })

    annotate_lifecycle_output_diagnostics("PostToolBatch", output)

    diagnostics = output.to_dict()["diagnostics"]
    ignored_fields = {
        item["field"]
        for item in diagnostics
        if item["code"] == "lifecycle_output_field_ignored"
    }
    assert ignored_fields == {
        "continue_flow",
        "decision",
        "reason",
        "user_message",
        "additional_context",
        "updated_input",
        "artifacts",
    }
    assert {"code": "observed"} in diagnostics


def test_terminal_lifecycle_events_support_messages_and_artifacts_without_flow_control() -> None:
    for event_name in ("Stop", "StopFailure"):
        output = LifecycleHookOutput.from_dict({
            "continue_flow": False,
            "decision": "deny",
            "reason": "try another round",
            "user_message": "Retry",
            "additional_context": [{"role": "system", "content": "ignored"}],
            "updated_input": {"result": "rewritten"},
            "artifacts": [{"artifact_id": "artifact-1"}],
            "diagnostics": [{"code": "observed"}],
        })

        annotate_lifecycle_output_diagnostics(event_name, output)

        diagnostics = output.to_dict()["diagnostics"]
        ignored_fields = {
            item["field"]
            for item in diagnostics
            if item["code"] == "lifecycle_output_field_ignored"
        }
        assert ignored_fields == {
            "continue_flow",
            "decision",
            "additional_context",
            "updated_input",
        }
        assert {"code": "observed"} in diagnostics


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
        "unavailable_reason": "runtime_unavailable:agent_context",
        "placement_runtime": {
            "server": {
                "executable": False,
                "unavailable_reason": "runtime_unavailable:agent_context",
            },
        },
        "runtime_context_required": ["agent"],
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
    assert items["hook:prompt"]["unavailable_reason"] == "runtime_unavailable:prompt_model"
    assert items["hook:internal"]["executable"] is True
    assert items["hook:internal"]["unavailable_reason"] == ""


def test_lifecycle_hook_catalog_dashboard_marks_context_bound_handlers_available() -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:prompt",
            _declaration_payload(
                event="UserPromptSubmit",
                handler_type="prompt",
                handler_ref="Rewrite prompts.",
                trust="trusted",
                matcher="*",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:mcp",
            _declaration_payload(
                handler_type="mcp_tool",
                handler_ref="mcp__audit__record",
                trust="trusted",
                matcher="*",
            ),
        ),
    ])

    items = {
        item["id"]: item
        for item in registry.dashboard_items(
            runtime_adapters=default_lifecycle_hook_catalog_runtime_adapters()
        )
    }

    assert items["hook:prompt"]["executable"] is True
    assert items["hook:prompt"]["runtime_context_required"] == ["prompt_model"]
    assert items["hook:mcp"]["executable"] is True
    assert items["hook:mcp"]["runtime_context_required"] == ["agent"]


def test_external_config_cannot_declare_internal_lifecycle_handler() -> None:
    with pytest.raises(ValueError, match="internal handlers"):
        lifecycle_declarations_from_config_hooks(
            owner_id="unsafe-package",
            source="capability_package",
            hooks=[
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "internal",
                    "handler_ref": "memory_context",
                    "display_name": "Unsafe internal hook",
                    "summary": "Attempts to use a core-only runtime adapter.",
                    "permissions": [],
                    "trust": "trusted",
                }
            ],
        )


def test_external_config_cannot_declare_unwired_lifecycle_event() -> None:
    with pytest.raises(ValueError, match="SessionStart"):
        lifecycle_declarations_from_config_hooks(
            owner_id="review",
            source="capability_package",
            hooks=[
                {
                    "event": "SessionStart",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "package:review/session-start",
                    "display_name": "Review package startup",
                    "summary": "SessionStart is internal-only until wired.",
                    "permissions": [],
                }
            ],
        )


def test_internal_runtime_adapter_refuses_external_internal_declaration() -> None:
    declaration = LifecycleHookDeclaration(
        id="hook:unsafe-internal",
        event="PreToolUse",
        source="capability_package",
        placement="server",
        handler_type="internal",
        display_name="Unsafe internal hook",
        summary="Simulates a declaration that bypassed config validation.",
        permissions=[],
        matcher="*",
        handler_ref="memory_context",
        trust="trusted",
    )
    registry = LifecycleHookRegistry([declaration])

    items = {item["id"]: item for item in registry.dashboard_items()}
    results = LifecycleHookDispatcher(
        registry,
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    ).dispatch(build_lifecycle_event_context("PreToolUse"))

    assert results == []
    assert registry.executable(event="PreToolUse") == []
    assert items["hook:unsafe-internal"]["executable"] is False
    assert items["hook:unsafe-internal"]["unavailable_reason"] == (
        "handler_source_unavailable:internal"
    )


def test_admin_managed_internal_lifecycle_handler_can_use_internal_adapter_output() -> None:
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:admin-internal",
        _declaration_payload(
            source="admin_managed",
            handler_type="internal",
            handler_ref="admin_policy",
            matcher="*",
            trust="trusted",
            permissions=[],
            technical={"output": {"diagnostics": [{"code": "admin_policy"}]}},
        ),
    )
    registry = LifecycleHookRegistry([declaration])
    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    )

    results = dispatcher.dispatch(build_lifecycle_event_context("PreToolUse"))

    assert len(results) == 1
    assert results[0].output.diagnostics == [{"code": "admin_policy"}]


def test_prompt_lifecycle_runtime_adapter_uses_model_json_output() -> None:
    class PromptLLM:
        def __init__(self) -> None:
            self.messages = []

        def chat(self, messages, **kwargs):  # noqa: ARG002
            self.messages = messages
            return type(
                "_Response",
                (),
                {
                    "content": (
                        '{"updated_input":{"user_input":"rewritten by prompt"},'
                        '"additional_context":[{"role":"system","content":"extra"}]}'
                    )
                },
            )()

    prompt_llm = PromptLLM()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Rewrite prompts when useful.",
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(
            prompt_llm=prompt_llm,
        ),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert len(results) == 1
    assert results[0].output.updated_input == {"user_input": "rewritten by prompt"}
    assert results[0].output.additional_context == [
        {"role": "system", "content": "extra"}
    ]
    assert "LifecycleHookOutput" in prompt_llm.messages[0]["content"]
    assert "original" in prompt_llm.messages[1]["content"]


def test_prompt_lifecycle_runtime_adapter_fail_closes_invalid_model_output() -> None:
    class PromptLLM:
        def chat(self, messages, **kwargs):  # noqa: ARG002
            return type("_Response", (), {"content": "not json"})()

    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(
            prompt_llm=PromptLLM(),
        ),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "prompt_output_invalid"


def _agent_bound_dispatcher(
    agent,
    declarations: list[LifecycleHookDeclaration],
) -> LifecycleHookDispatcher:
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry(declarations),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    )
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)
    return dispatcher


class _AllowingAdapterAgent:
    runtime_working_directory = ""

    def __init__(self) -> None:
        self.permission_checks = []

    def evaluate_tool_permission(self, tool, *, tool_call=None):
        self.permission_checks.append((getattr(tool, "name", ""), tool_call))
        return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)


class _ApprovalProvider:
    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.requests = []

    def request_approval(self, request):
        self.requests.append(request)
        return self.decision


class _ApprovalRequiredAdapterAgent(_AllowingAdapterAgent):
    def __init__(
        self,
        *,
        approval_provider: object | None,
        runtime_working_directory: str = "",
    ) -> None:
        super().__init__()
        self.approval_provider = approval_provider
        self.runtime_working_directory = runtime_working_directory

    def evaluate_tool_permission(self, tool, *, tool_call=None):
        self.permission_checks.append((getattr(tool, "name", ""), tool_call))
        return PermissionDecision(
            action=PermissionAction.REQUIRE_APPROVAL,
            authorized=True,
            reason="lifecycle command requires review",
        )


def test_command_lifecycle_runtime_adapter_runs_permission_gated_json_command() -> None:
    agent = _AllowingAdapterAgent()
    command = (
        f'"{sys.executable}" -c "import json; '
        "print(json.dumps({"
        "'diagnostics': [{'code': 'command_ok'}], "
        "'updated_input': {'user_input': 'from command'}"
        '}))"'
    )
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:command",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="command",
            handler_ref=command,
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.updated_input == {"user_input": "from command"}
    assert results[0].output.diagnostics == [{"code": "command_ok"}]
    assert agent.permission_checks[0][0] == "shell"


def test_command_lifecycle_runtime_adapter_requests_approval_before_execution(tmp_path: Path) -> None:
    approval = _ApprovalProvider(ApprovalDecision.allow_once("approved"))
    agent = _ApprovalRequiredAdapterAgent(
        approval_provider=approval,
        runtime_working_directory=str(tmp_path),
    )
    command = (
        f'"{sys.executable}" -c "import json, pathlib; '
        "pathlib.Path('marker.txt').write_text('ran'); "
        "print(json.dumps({'diagnostics': [{'code': 'approved_command'}]}))"
        '"'
    )
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:command",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="command",
            handler_ref=command,
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context("UserPromptSubmit", payload={"user_input": "original"})
    )

    assert results[0].output.diagnostics == [{"code": "approved_command"}]
    assert (tmp_path / "marker.txt").read_text() == "ran"
    assert approval.requests[0].tool_name == "shell"
    assert approval.requests[0].tool_args["command"] == command
    assert approval.requests[0].metadata["lifecycle_hook_id"] == "hook:command"


def test_command_lifecycle_runtime_adapter_stops_when_approval_is_denied(tmp_path: Path) -> None:
    approval = _ApprovalProvider(ApprovalDecision.deny_once("denied"))
    agent = _ApprovalRequiredAdapterAgent(
        approval_provider=approval,
        runtime_working_directory=str(tmp_path),
    )
    command = (
        f'"{sys.executable}" -c "import pathlib; '
        "pathlib.Path('marker.txt').write_text('ran')"
        '"'
    )
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:command",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="command",
            handler_ref=command,
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context("UserPromptSubmit", payload={"user_input": "original"})
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "approval_denied"
    assert not (tmp_path / "marker.txt").exists()
    assert len(approval.requests) == 1


def test_command_lifecycle_runtime_adapter_fails_closed_without_approval_provider() -> None:
    agent = _ApprovalRequiredAdapterAgent(approval_provider=None)
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:command",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="command",
            handler_ref=f'"{sys.executable}" -c "print(1)"',
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context("UserPromptSubmit", payload={"user_input": "original"})
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "approval_provider_missing"


def test_http_lifecycle_runtime_adapter_posts_context_and_reads_json_output() -> None:
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            seen["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = json.dumps({"diagnostics": [{"code": "http_ok"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):  # noqa: D401
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = _AllowingAdapterAgent()
        declaration = LifecycleHookDeclaration.from_dict(
            "hook:http",
            _declaration_payload(
                event="UserPromptSubmit",
                handler_type="http",
                handler_ref=f"http://127.0.0.1:{server.server_port}/hook",
                trust="trusted",
                matcher="*",
            ),
        )
        dispatcher = _agent_bound_dispatcher(agent, [declaration])

        results = dispatcher.dispatch(
            build_lifecycle_event_context(
                "UserPromptSubmit",
                payload={"user_input": "original"},
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert results[0].output.diagnostics == [{"code": "http_ok"}]
    assert seen["body"]["context"]["payload"]["user_input"] == "original"
    assert agent.permission_checks[0][0] == "http_request"


def test_mcp_tool_lifecycle_runtime_adapter_invokes_agent_visible_tool() -> None:
    class Tool:
        name = "mcp__audit__record"
        tool_source = "mcp"
        server_name = "audit"

        def __init__(self) -> None:
            self.arguments = None

        def execute(self, **kwargs):
            self.arguments = kwargs
            return '{"diagnostics":[{"code":"mcp_ok"}]}'

        def preflight_validate(self, **_kwargs):
            return None

    class Agent(_AllowingAdapterAgent):
        def __init__(self) -> None:
            super().__init__()
            self.tool = Tool()
            self.state = SimpleNamespace(current_round=0)
            self.active_mode = "coder"
            self.approval_provider = None
            self.events = []

        def get_tool(self, name):
            return self.tool if name == self.tool.name else None

        def is_tool_allowed_in_mode(self, _name):
            return True

        def suggest_modes_for_tool(self, _name):
            return []

        def get_active_mode_config(self):
            return SimpleNamespace(prompt_append="")

        def _emit_event(self, event):
            self.events.append(event)

    agent = Agent()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:mcp",
        _declaration_payload(
            event="PostToolUse",
            handler_type="mcp_tool",
            handler_ref="mcp__audit__record",
            trust="trusted",
            matcher="*",
            technical={"arguments": {"status": "done"}},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context("PostToolUse", payload={"tool_names": ["shell"]})
    )

    assert results[0].output.diagnostics == [{"code": "mcp_ok"}]
    assert agent.tool.arguments == {"status": "done"}
    assert agent.permission_checks[0][0] == "mcp__audit__record"
    assert [event.event_type.value for event in agent.events] == [
        "tool_call_start",
        "tool_call_end",
    ]


def test_agent_lifecycle_runtime_adapter_submits_agent_run_reference() -> None:
    class Run:
        id = "run-1"
        agent_id = "reviewer"

    class ControlPlane:
        def __init__(self) -> None:
            self.requests = []

        def submit_agent_run(self, request):
            self.requests.append(request)
            return Run()

    class Agent(_AllowingAdapterAgent):
        def __init__(self) -> None:
            super().__init__()
            self.agent_run_control_plane = ControlPlane()
            self.runtime_config = type(
                "_Config",
                (),
                {
                    "agent_registry": type(
                        "_Registry",
                        (),
                        {"agents": {"reviewer": AgentConfig(id="reviewer")}},
                    )()
                },
            )()

    agent = Agent()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:agent",
        _declaration_payload(
            event="Stop",
            handler_type="agent",
            handler_ref="reviewer",
            trust="trusted",
            matcher="*",
            technical={
                "prompt": "review lifecycle output",
                "budget": {"token_budget": "5000", "max_turns": 2},
            },
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "Stop",
            agent_run_id="parent-run",
            session_run_id="session-1",
            turn_id="turn-1",
        )
    )

    assert results[0].output.diagnostics[0]["code"] == "agent_run_submitted"
    assert results[0].output.artifacts == [{"kind": "agent_run", "id": "run-1"}]
    request = agent.agent_run_control_plane.requests[0]
    assert request.agent_id == "reviewer"
    assert request.source.value == "delegation"
    assert request.parent_run_id == "parent-run"
    assert request.delegated_by_run_id == "parent-run"
    assert request.budget == {"token_budget": 5000, "max_turns": 2}
    assert "budget" not in request.metadata
    assert request.metadata["parent_session_id"] == "session-1"
    assert request.metadata["parent_turn_id"] == "turn-1"


def test_agent_lifecycle_runtime_adapter_rejects_unknown_target_agent() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.requests = []

        def submit_agent_run(self, request):  # noqa: ARG002
            self.requests.append(request)
            raise AssertionError("unknown agent should not be submitted")

    class Agent(_AllowingAdapterAgent):
        def __init__(self) -> None:
            super().__init__()
            self.agent_run_control_plane = ControlPlane()
            self.runtime_config = type(
                "_Config",
                (),
                {"agent_registry": type("_Registry", (), {"agents": {}})()},
            )()

    agent = Agent()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:agent",
        _declaration_payload(
            event="Stop",
            handler_type="agent",
            handler_ref="missing-agent",
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle output"},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(build_lifecycle_event_context("Stop"))

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "agent_not_found"
    assert agent.agent_run_control_plane.requests == []


def test_system_builtin_lifecycle_declarations_wrap_existing_hook_specs() -> None:
    from reuleauxcoder.domain.hooks.discovery import discover_hook_specs, instantiate_hooks

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
    assert registry.executable(event="UserPromptSubmit") == []

    hook_registry = HookRegistry()
    for hook_point, hook in instantiate_hooks(discover_hook_specs(), Config()):
        hook_registry.register(hook_point, hook)
    runtime_registry = LifecycleHookRegistry(
        system_builtin_lifecycle_declarations_from_hook_registry(hook_registry)
    )
    assert "memory_context" in {
        declaration.handler_ref
        for declaration in runtime_registry.executable(
            event="UserPromptSubmit",
            runtime_adapters=default_lifecycle_hook_runtime_adapters(
                hook_registry=hook_registry,
            ),
        )
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
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda declaration, _context: (
                calls.append(declaration.id)
                or LifecycleHookOutput.from_dict({"decision": "allow"})
            )
        ),
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
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        ),
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
        runtime_adapters=_runtime_adapters(
            internal=lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        ),
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


def test_lifecycle_dispatcher_skips_unavailable_runtime_adapters() -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict("hook:trusted", _declaration_payload(trust="trusted"))
    ])
    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry(),
    )

    results = dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name="PreToolUse",
            placement="server",
            payload={"tool_names": ["deploy"]},
        )
    )

    assert results == []
    dashboard = registry.dashboard_items(
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry(),
    )[0]
    assert dashboard["executable"] is False
    assert dashboard["unavailable_reason"] == "handler_unavailable:mcp_tool"


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
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        ),
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
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda declaration, _context: (
                calls.append(declaration.id) or LifecycleHookOutput()
            )
        ),
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
                            "event": "UserPromptSubmit",
                            "handler_type": "prompt",
                            "handler_ref": "package:review/prompt",
                            "display_name": "Review package prompt",
                            "summary": "Adds review prompt context.",
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
    assert items["hook:capability_package:review:UserPromptSubmit:0"]["source"] == (
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
