from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.agent.events import AgentEventType
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
    AgentLifecycleHookRuntimeAdapter,
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
    MCPToolLifecycleHookRuntimeAdapter,
    annotate_lifecycle_output_diagnostics,
    bind_lifecycle_runtime_adapters_to_agent,
    build_lifecycle_event_context,
    build_permission_lifecycle_payload,
    build_tool_batch_lifecycle_payload,
    build_tool_lifecycle_payload,
    default_lifecycle_hook_catalog_runtime_adapters,
    dispatch_internal_lifecycle_hook_point,
    lifecycle_event_catalog_items,
    lifecycle_declarations_from_config_hooks,
    lifecycle_registry_from_config,
    default_lifecycle_hook_runtime_adapters,
    system_builtin_lifecycle_declarations_from_hook_registry,
    system_builtin_lifecycle_declarations_from_hook_specs,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import HookPoint, SessionSaveContext, SessionStartContext
from reuleauxcoder.domain.hooks.lifecycle_policy import (
    lifecycle_gate_output_is_terminal,
)
from reuleauxcoder.domain.llm.models import LLMResponse
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
        "credentials": [],
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
        "owner_activation_state": "active",
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
    for source in (
        "user_config",
        "project_config",
        "local_project_config",
        "capability_package",
        "skill",
        "mcp_server",
        "session",
    ):
        with pytest.raises(ValueError, match="internal"):
            LifecycleHookDeclaration.from_dict(
                f"hook:{source}:internal",
                _declaration_payload(
                    source=source,
                    handler_type="internal",
                    handler_ref="memory_context",
                ),
            )

    for source in ("system_builtin", "admin_managed"):
        declaration = LifecycleHookDeclaration.from_dict(
            f"hook:{source}:memory-context",
            _declaration_payload(
                source=source,
                handler_type="internal",
                handler_ref="memory_context",
                trust="trusted",
                display_name="Inject private memory",
                summary="Adds scoped memory context before model requests.",
                permissions=[],
            ),
        )

        assert declaration.source == source
        assert declaration.handler_type == "internal"
        assert declaration.trust == "trusted"


def test_lifecycle_hook_authoritative_constants_match_adr_boundaries() -> None:
    assert LIFECYCLE_HOOK_EVENTS == {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
        "PreCompact",
        "PostCompact",
        "ConfigChange",
        "CwdChanged",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "Elicitation",
        "ElicitationResult",
        "Notification",
    }
    assert LIFECYCLE_HOOK_PLACEMENTS == {"server", "peer", "both"}
    assert "local" not in LIFECYCLE_HOOK_PLACEMENTS
    assert "local_peer" not in LIFECYCLE_HOOK_PLACEMENTS
    assert LIFECYCLE_HOOK_SOURCES == {
        "system_builtin",
        "admin_managed",
        "user_config",
        "project_config",
        "local_project_config",
        "capability_package",
        "skill",
        "mcp_server",
        "session",
    }
    assert "memory_provider" not in LIFECYCLE_HOOK_SOURCES
    assert LIFECYCLE_HOOK_HANDLER_TYPES == {
        "command",
        "http",
        "mcp_tool",
        "prompt",
        "agent",
        "internal",
    }
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


def test_default_lifecycle_runtime_registries_cover_public_handler_types() -> None:
    assert default_lifecycle_hook_runtime_adapters().handler_types() == (
        LIFECYCLE_HOOK_HANDLER_TYPES
    )
    assert default_lifecycle_hook_catalog_runtime_adapters().handler_types() == (
        LIFECYCLE_HOOK_HANDLER_TYPES
    )


@pytest.mark.parametrize("source", sorted(LIFECYCLE_HOOK_SOURCES))
def test_lifecycle_config_projection_preserves_required_contract_fields(source: str) -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id=f"{source}-owner",
        source=source,
        hooks=[
            {
                "event": "PreToolUse",
                "placement": "both",
                "handler_type": "command",
                "handler_ref": "python -m hook",
                "matcher": {"tool_names": ["shell"], "mcp_servers": ["github"]},
                "display_name": "Contract check",
                "summary": "Preserves public lifecycle contract fields.",
                "permissions": ["audit.write"],
                "technical": {"timeout_sec": 3, "risk": "command"},
                "risk_level": "high",
                "trust": "trusted",
            }
        ],
        owner_enabled=False,
        owner_status="disabled",
    )

    declaration = declarations[0]

    assert declaration.source == source
    assert declaration.owner_id == f"{source}-owner"
    assert declaration.owner_enabled is False
    assert declaration.owner_status == "disabled"
    assert declaration.event == "PreToolUse"
    assert declaration.placement == "both"
    assert declaration.handler_type == "command"
    assert declaration.handler_ref == "python -m hook"
    assert declaration.matcher == {"tool_names": ["shell"], "mcp_servers": ["github"]}
    assert declaration.display_name == "Contract check"
    assert declaration.summary == "Preserves public lifecycle contract fields."
    assert declaration.permissions == ["audit.write"]
    assert declaration.technical == {"timeout_sec": 3, "risk": "command"}
    assert declaration.risk_level == "high"
    assert declaration.trust == "trusted"


def test_lifecycle_event_catalog_reports_external_wiring_status() -> None:
    items = {item["event"]: item for item in lifecycle_event_catalog_items()}
    externally_supported = {
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PermissionRequest",
        "PermissionDenied",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "SubagentStart",
        "TaskCreated",
        "PreCompact",
        "PostCompact",
        "ConfigChange",
        "CwdChanged",
        "FileChanged",
        "Notification",
        "Stop",
        "StopFailure",
    }
    audit_only = {"TaskCompleted", "SubagentStop", "WorktreeCreate", "WorktreeRemove"}

    assert set(items) == LIFECYCLE_HOOK_EVENTS
    for event_name, item in items.items():
        assert item["event"] == event_name
        assert item["in_adr_catalog"] is True
        if event_name in externally_supported:
            assert item["external_config_supported"] is True
            assert item["runtime_status"] == "external_config_supported"
        elif event_name in audit_only:
            assert item["external_config_supported"] is False
            assert item["runtime_status"] == "control_plane_audit_only"
        else:
            assert item["external_config_supported"] is False
            assert item["runtime_status"] == "external_event_unwired"

    assert items["TaskCompleted"]["external_config_supported"] is False
    assert items["TaskCompleted"]["runtime_status"] == "control_plane_audit_only"
    assert items["TaskCompleted"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert items["SubagentStop"]["external_config_supported"] is False
    assert items["SubagentStop"]["runtime_status"] == "control_plane_audit_only"
    assert items["SubagentStop"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert items["WorktreeCreate"]["external_config_supported"] is False
    assert items["WorktreeCreate"]["runtime_status"] == "control_plane_audit_only"
    assert items["WorktreeCreate"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert items["WorktreeRemove"]["external_config_supported"] is False
    assert items["WorktreeRemove"]["runtime_status"] == "control_plane_audit_only"
    assert items["WorktreeRemove"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )


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


def test_lifecycle_hook_output_rejects_non_boolean_continue_flow() -> None:
    with pytest.raises(ValueError, match="continue_flow"):
        LifecycleHookOutput.from_dict({"continue_flow": "false"})


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


def test_permission_denied_lifecycle_output_supports_feedback_without_flow_control() -> None:
    output = LifecycleHookOutput.from_dict({
        "continue_flow": False,
        "decision": "allow",
        "reason": "Use an allowed capability.",
        "user_message": "Use read_file or request shell access.",
        "additional_context": [{"role": "system", "content": "ignored"}],
        "updated_input": {"user_input": "rewritten"},
        "artifacts": [{"artifact_id": "artifact-1"}],
        "diagnostics": [{"code": "recoverable_permission_denied"}],
    })

    annotate_lifecycle_output_diagnostics("PermissionDenied", output)

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
        "artifacts",
    }
    assert {"code": "recoverable_permission_denied"} in diagnostics


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


def test_lifecycle_dispatcher_bounds_oversized_hook_output_with_artifact_reference() -> None:
    huge = "oversized-output-" * 1000
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:oversized",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="mcp_tool",
            handler_ref="oversized",
            matcher="*",
            trust="trusted",
        ),
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda _declaration, _context: LifecycleHookOutput.from_dict(
                {
                    "decision": "deny",
                    "reason": huge,
                    "user_message": huge,
                    "additional_context": [{"role": "system", "content": huge}],
                    "diagnostics": [{"code": "raw", "message": huge}],
                    "artifacts": [{"kind": "raw", "content": huge}],
                }
            )
        ),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    output = results[0].output.to_dict()
    rendered = json.dumps(output, ensure_ascii=False)
    assert huge not in rendered
    assert len(output["reason"]) < 5000
    assert len(output["user_message"]) < 5000
    assert {
        artifact["kind"]
        for artifact in output["artifacts"]
    } >= {"lifecycle_output_overflow"}
    overflow_artifacts = [
        artifact
        for artifact in output["artifacts"]
        if artifact.get("kind") == "lifecycle_output_overflow"
    ]
    assert {artifact["field"] for artifact in overflow_artifacts} >= {
        "reason",
        "user_message",
        "diagnostics[0].message",
        "additional_context[0].content",
        "artifacts[0].content",
    }
    assert any(
        item.get("code") == "lifecycle_output_overflow"
        for item in output["diagnostics"]
    )
    runtime_artifacts = getattr(results[0].output, "runtime_artifacts", [])
    assert {
        artifact["metadata"]["field"]
        for artifact in runtime_artifacts
    } >= {
        "reason",
        "user_message",
        "diagnostics[0].message",
        "additional_context[0].content",
        "artifacts[0].content",
    }
    assert all(artifact["type"] == "log" for artifact in runtime_artifacts)
    assert all(
        artifact["metadata"]["kind"] == "lifecycle_output_overflow"
        for artifact in runtime_artifacts
    )
    assert any(artifact["content"] == huge for artifact in runtime_artifacts)


def test_lifecycle_dispatcher_bounds_aggregate_hook_output_size_with_artifact_references() -> None:
    chunks = [f"chunk-{index:02d}-" + ("x" * 900) for index in range(70)]
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:aggregate-output",
        _declaration_payload(
            event="PostCompact",
            handler_type="mcp_tool",
            handler_ref="aggregate-output",
            matcher="*",
            trust="trusted",
        ),
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda _declaration, _context: LifecycleHookOutput.from_dict(
                {
                    "diagnostics": [
                        {"code": f"diag-{index}", "message": chunk}
                        for index, chunk in enumerate(chunks)
                    ]
                }
            )
        ),
    )

    results = dispatcher.dispatch(build_lifecycle_event_context("PostCompact"))

    output = results[0].output.to_dict()
    rendered = json.dumps(output, ensure_ascii=False)
    assert chunks[0] in rendered
    assert chunks[-1] not in rendered
    overflow_artifacts = [
        artifact
        for artifact in output["artifacts"]
        if artifact.get("kind") == "lifecycle_output_overflow"
    ]
    assert overflow_artifacts
    assert any(
        str(artifact.get("field", "")).startswith("diagnostics[")
        for artifact in overflow_artifacts
    )
    assert any(
        item.get("code") == "lifecycle_output_overflow"
        for item in output["diagnostics"]
    )


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
                permissions=[],
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:pending",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="pending",
                permissions=[],
                trust="pending_review",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:disabled",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="disabled",
                permissions=[],
                trust="disabled",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:blocked",
            _declaration_payload(
                source="system_builtin",
                handler_type="internal",
                handler_ref="blocked",
                permissions=[],
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
                permissions=[],
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
                permissions=[],
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
                permissions=[],
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
        "owner_activation_state": "active",
        "placement": "server",
        "handler_type": "mcp_tool",
        "display_name": "Record deployment result",
        "summary": "Writes deployment results to audit after a deploy tool succeeds.",
        "trust": "trusted",
        "enabled": False,
        "executable": False,
        "can_manage": True,
        "management_actions": [
            {
                "trust": "pending_review",
                "label": "Mark pending review",
                "endpoint": "admin.lifecycle_hooks.trust",
            },
            {
                "trust": "disabled",
                "label": "Disable hook",
                "endpoint": "admin.lifecycle_hooks.trust",
            },
            {
                "trust": "blocked",
                "label": "Block hook",
                "endpoint": "admin.lifecycle_hooks.trust",
            },
        ],
        "unavailable_reason": "runtime_unavailable:agent_context",
        "placement_runtime": {
            "server": {
                "executable": False,
                "unavailable_reason": "runtime_unavailable:agent_context",
            },
        },
        "runtime_context_required": ["agent"],
        "permissions": ["audit.write"],
        "credentials": [],
        "risk_level": "medium",
        "recent_result": {
            "status": "unrecorded",
            "summary": "No lifecycle executions recorded.",
        },
        "technical": {
            "handler_ref": "mcp__audit__record",
            "matcher": {"tool_names": "deploy"},
            "raw": {"handler_ref": "mcp__audit__record"},
        },
    }]
    assert "handler_ref" not in {
        key for key in items[0].keys() if key != "technical"
    }


def test_lifecycle_hook_registry_dashboard_items_expose_credentials_and_recent_result() -> None:
    registry = LifecycleHookRegistry()
    registry.register_dict(
        "hook:deploy-audit",
        _declaration_payload(
            trust="trusted",
            credentials=["DEPLOY_TOKEN"],
        ),
    )

    items = registry.dashboard_items(
        recent_results={
            "hook:deploy-audit": {
                "status": "denied",
                "summary": "Denied deploy command",
                "session_run_id": "session-run-1",
            }
        }
    )

    assert items[0]["credentials"] == ["DEPLOY_TOKEN"]
    assert items[0]["recent_result"] == {
        "status": "denied",
        "summary": "Denied deploy command",
        "session_run_id": "session-run-1",
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


def test_external_config_can_declare_permission_denied_lifecycle_event() -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="review",
        source="capability_package",
        hooks=[
            {
                "event": "PermissionDenied",
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "package:review/permission-denied",
                "display_name": "Permission denied recovery",
                "summary": "Suggests recoverable alternatives after automatic denial.",
                "permissions": [],
            }
        ],
    )

    assert declarations[0].event == "PermissionDenied"


def test_external_config_can_declare_user_prompt_expansion_lifecycle_event() -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="commands",
        source="skill",
        hooks=[
            {
                "event": "UserPromptExpansion",
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "skills/command-guard/SKILL.md",
                "display_name": "Command guard",
                "summary": "Checks slash commands before command expansion.",
                "permissions": ["prompt.read"],
            }
        ],
    )

    assert declarations[0].event == "UserPromptExpansion"
    assert declarations[0].trust == "pending_review"


@pytest.mark.parametrize("event_name", ["TaskCreated", "SubagentStart"])
def test_external_config_can_declare_wired_subagent_lifecycle_events(
    event_name: str,
) -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="delegation",
        source="capability_package",
        hooks=[
            {
                "event": event_name,
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "package:delegation/audit",
                "display_name": f"{event_name} audit",
                "summary": f"Audits {event_name} around delegated AgentRuns.",
                "permissions": ["prompt.read"],
            }
        ],
    )

    assert declarations[0].event == event_name
    assert declarations[0].trust == "pending_review"


@pytest.mark.parametrize("event_name", ["PreCompact", "PostCompact"])
def test_external_config_can_declare_wired_compaction_lifecycle_events(
    event_name: str,
) -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="compression",
        source="capability_package",
        hooks=[
            {
                "event": event_name,
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "package:compression/audit",
                "display_name": f"{event_name} audit",
                "summary": f"Audits {event_name} around context compression.",
                "permissions": ["prompt.read"],
            }
        ],
    )

    assert declarations[0].event == event_name
    assert declarations[0].trust == "pending_review"


def test_external_config_can_declare_wired_cwd_changed_lifecycle_event() -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="workspace",
        source="project_config",
        hooks=[
            {
                "event": "CwdChanged",
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "project:cwd/audit",
                "display_name": "CWD changed audit",
                "summary": "Refreshes project context when AgentRun cwd changes.",
                "permissions": ["prompt.read"],
            }
        ],
    )

    assert declarations[0].event == "CwdChanged"
    assert declarations[0].trust == "pending_review"


@pytest.mark.parametrize("event_name", ["WorktreeCreate", "WorktreeRemove"])
def test_external_config_rejects_control_plane_worktree_audit_events(
    event_name: str,
) -> None:
    with pytest.raises(ValueError, match=event_name):
        lifecycle_declarations_from_config_hooks(
            owner_id="workspace",
            source="admin_managed",
            hooks=[
                {
                    "event": event_name,
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "admin:worktree/audit",
                    "display_name": f"{event_name} audit",
                    "summary": "Audits daemon AgentRun worktree lifecycle.",
                    "permissions": ["prompt.read"],
                }
            ],
        )


@pytest.mark.parametrize("event_name", ["ConfigChange", "FileChanged"])
def test_external_config_can_declare_wired_admin_file_lifecycle_events(
    event_name: str,
) -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="workspace",
        source="admin_managed",
        hooks=[
            {
                "event": event_name,
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": f"admin:{event_name}/audit",
                "display_name": f"{event_name} audit",
                "summary": f"Audits {event_name} runtime changes.",
                "permissions": ["prompt.read"],
            }
        ],
    )

    assert declarations[0].event == event_name
    assert declarations[0].trust == "pending_review"


def test_external_config_can_declare_wired_notification_lifecycle_event() -> None:
    declarations = lifecycle_declarations_from_config_hooks(
        owner_id="notifications",
        source="admin_managed",
        hooks=[
            {
                "event": "Notification",
                "placement": "server",
                "handler_type": "prompt",
                "handler_ref": "admin:notifications/audit",
                "display_name": "Notification audit",
                "summary": "Audits user-facing notifications without changing them.",
                "permissions": ["prompt.read"],
            }
        ],
    )

    assert declarations[0].event == "Notification"
    assert declarations[0].trust == "pending_review"


def test_notification_lifecycle_output_ignores_control_fields() -> None:
    output = LifecycleHookOutput.from_dict({
        "continue_flow": False,
        "decision": "deny",
        "reason": "show notification details",
        "user_message": "Notification observed.",
        "additional_context": [{"role": "system", "content": "ignored"}],
        "updated_input": {"message": "rewritten"},
        "artifacts": [{"artifact_id": "notification-audit"}],
        "diagnostics": [{"code": "observed"}],
    })

    annotate_lifecycle_output_diagnostics("Notification", output)

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


@pytest.mark.parametrize("event_name", ["TaskCompleted", "SubagentStop"])
def test_external_config_still_rejects_unwired_subagent_completion_events(
    event_name: str,
) -> None:
    with pytest.raises(ValueError, match=event_name):
        lifecycle_declarations_from_config_hooks(
            owner_id="delegation",
            source="capability_package",
            hooks=[
                {
                    "event": event_name,
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "package:delegation/audit",
                    "display_name": f"{event_name} audit",
                    "summary": f"Audits {event_name} around delegated AgentRuns.",
                    "permissions": ["prompt.read"],
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


def test_internal_lifecycle_runtime_adapter_enforces_declared_permissions_before_output() -> None:
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:admin-internal",
        _declaration_payload(
            source="admin_managed",
            handler_type="internal",
            handler_ref="admin_policy",
            matcher="*",
            trust="trusted",
            permissions=["audit.write"],
            technical={"output": {"diagnostics": [{"code": "admin_policy"}]}},
        ),
    )
    agent = _DeclaredPermissionDenyingAdapterAgent()
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    )
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(build_lifecycle_event_context("PreToolUse"))

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_deny"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "deny"
    assert results[0].output.diagnostics[0]["handler_type"] == "internal"
    assert results[0].output.diagnostics[0]["permission"]["reason"] == (
        "declared lifecycle permission denied"
    )
    assert agent.permission_checks[0][0] == "lifecycle_internal"
    assert agent.permission_checks[0][1].arguments["permissions"] == ["audit.write"]


def test_internal_lifecycle_runtime_adapter_blocks_background_approval_without_output() -> None:
    class ApprovalProvider:
        def request_approval(self, _request):
            raise AssertionError("background internal lifecycle approval must not prompt")

    declaration = LifecycleHookDeclaration.from_dict(
        "hook:admin-internal",
        _declaration_payload(
            source="admin_managed",
            handler_type="internal",
            handler_ref="admin_policy",
            matcher="*",
            trust="trusted",
            permissions=["audit.write"],
            technical={"output": {"diagnostics": [{"code": "admin_policy"}]}},
        ),
    )
    agent = _ApprovalRequiredAdapterAgent(approval_provider=ApprovalProvider())
    agent.permission_interactive = False
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    )
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(build_lifecycle_event_context("PreToolUse"))

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_blocked_review"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "blocked_review"
    assert results[0].output.diagnostics[0]["handler_type"] == "internal"


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
            permissions=[],
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


def test_prompt_lifecycle_runtime_adapter_respects_declared_permissions() -> None:
    class PromptLLM:
        def __init__(self) -> None:
            self.called = False

        def chat(self, messages, **kwargs):  # noqa: ARG002
            self.called = True
            return type("_Response", (), {"content": '{"diagnostics":[{"code":"called"}]}'})()

    class DenyingAgent:
        def __init__(self) -> None:
            self.permission_checks = []

        def evaluate_tool_permission(self, tool, *, tool_call=None):
            self.permission_checks.append((getattr(tool, "name", ""), tool_call))
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason="prompt lifecycle denied",
            )

    prompt_llm = PromptLLM()
    agent = DenyingAgent()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            permissions=["lifecycle.prompt"],
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
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_deny"
    assert prompt_llm.called is False
    assert agent.permission_checks[0][0] == "lifecycle_prompt"
    assert agent.permission_checks[0][1].arguments["permissions"] == ["lifecycle.prompt"]


def test_prompt_lifecycle_runtime_adapter_blocks_background_approval_without_model_call() -> None:
    class PromptLLM:
        def chat(self, messages, **kwargs):  # noqa: ARG002
            raise AssertionError("background lifecycle prompt approval must not call model")

    class ApprovalProvider:
        def request_approval(self, _request):
            raise AssertionError("background lifecycle prompt approval must not prompt")

    prompt_llm = PromptLLM()
    agent = _ApprovalRequiredAdapterAgent(approval_provider=ApprovalProvider())
    agent.permission_interactive = False
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            permissions=["lifecycle.prompt"],
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
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_blocked_review"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "blocked_review"


def test_prompt_lifecycle_runtime_adapter_blocks_runtime_budget_before_model_call() -> None:
    class PromptLLM:
        def __init__(self) -> None:
            self.called = False

        def chat(self, messages, **kwargs):  # noqa: ARG002
            self.called = True
            return type("_Response", (), {"content": '{"diagnostics":[{"code":"called"}]}'})()

    prompt_llm = PromptLLM()
    agent = _AllowingAdapterAgent()
    _exhaust_runtime_timeout(agent)
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            permissions=[],
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
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "runtime_budget_exceeded"
    assert prompt_llm.called is False


def test_prompt_lifecycle_runtime_adapter_records_model_token_usage() -> None:
    class PromptLLM:
        def chat(self, messages, **kwargs):  # noqa: ARG002
            return LLMResponse(
                content='{"diagnostics":[{"code":"prompt_ok"}]}',
                prompt_tokens=2,
                completion_tokens=3,
                usage_extra={"lifecycle_prompt": {"provider": "test"}},
            )

    agent = _AllowingAdapterAgent()
    agent.state = SimpleNamespace(
        total_prompt_tokens=5,
        total_completion_tokens=7,
        usage_extra={},
    )
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            permissions=[],
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
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.diagnostics == [{"code": "prompt_ok"}]
    assert agent.state.total_prompt_tokens == 7
    assert agent.state.total_completion_tokens == 10
    assert agent.state.usage_extra == {"lifecycle_prompt": {"provider": "test"}}


def test_prompt_lifecycle_runtime_adapter_blocks_when_model_call_exhausts_token_budget() -> None:
    class PromptLLM:
        def chat(self, messages, **kwargs):  # noqa: ARG002
            return LLMResponse(
                content='{"updated_input":{"user_input":"rewritten"}}',
                prompt_tokens=1,
                completion_tokens=0,
            )

    agent = _AllowingAdapterAgent()
    agent.runtime_budget = {"token_budget": 5}
    agent.runtime_token_budget = 5
    agent.state = SimpleNamespace(
        total_prompt_tokens=3,
        total_completion_tokens=1,
        usage_extra={},
    )
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            permissions=[],
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
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "runtime_budget_exceeded"
    assert results[0].output.updated_input is None
    assert agent.state.total_prompt_tokens == 4
    assert agent.state.total_completion_tokens == 1


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
            permissions=[],
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


def test_prompt_lifecycle_runtime_adapter_fail_opens_observation_invalid_model_output() -> None:
    class PromptLLM:
        def chat(self, messages, **kwargs):  # noqa: ARG002
            return type("_Response", (), {"content": "not json"})()

    declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt",
        _declaration_payload(
            event="PostToolUse",
            handler_type="prompt",
            handler_ref="Summarize tool output as JSON.",
            permissions=[],
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
            "PostToolUse",
            payload={"tool_names": ["read_file"]},
        )
    )

    output = results[0].output
    assert output.continue_flow is True
    assert output.decision == "none"
    assert output.diagnostics[0]["code"] == "prompt_output_invalid"
    assert output.diagnostics[0]["failure_policy"] == "fail_open"


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


class _DeclaredPermissionDenyingAdapterAgent(_AllowingAdapterAgent):
    def __init__(self, *, runtime_working_directory: str = "") -> None:
        super().__init__()
        self.runtime_working_directory = runtime_working_directory

    def evaluate_tool_permission(self, tool, *, tool_call=None):
        self.permission_checks.append((getattr(tool, "name", ""), tool_call))
        arguments = getattr(tool_call, "arguments", {}) or {}
        if "audit.write" in list(arguments.get("permissions") or []):
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason="declared lifecycle permission denied",
            )
        return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)


def _bind_adapter_runtime_boundary(agent: object, tmp_path: Path) -> dict[str, str]:
    workspace_root = str(tmp_path / "agent-worktree")
    working_directory = str(tmp_path / "agent-worktree" / "src")
    agent.runtime_agent_run_id = "parent-run"
    agent.runtime_workspace_root = workspace_root
    agent.runtime_working_directory = working_directory
    agent.runtime_execution_target = "remote_peer"
    return {
        "agent_run_id": "parent-run",
        "runtime_workspace_root": workspace_root,
        "runtime_working_directory": working_directory,
        "execution_target": "remote_peer",
        "path_space": "agent_run_worktree",
    }


def _assert_permission_arguments_runtime_boundary(
    arguments: dict[str, object],
    expected: dict[str, str],
) -> None:
    for key, value in expected.items():
        assert arguments[key] == value


def _exhaust_runtime_timeout(agent) -> None:
    agent.runtime_budget = {"timeout_sec": 1}
    agent.runtime_timeout_sec = 1
    agent.runtime_deadline = 0.0


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


def test_command_lifecycle_runtime_adapter_enforces_declared_permissions_before_side_effect(
    tmp_path: Path,
) -> None:
    agent = _DeclaredPermissionDenyingAdapterAgent(
        runtime_working_directory=str(tmp_path)
    )
    command = (
        f'"{sys.executable}" -c "import json, pathlib; '
        "pathlib.Path('marker.txt').write_text('ran'); "
        "print(json.dumps({'diagnostics': [{'code': 'command_ok'}]}))"
        '"'
    )
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:command",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="command",
            handler_ref=command,
            permissions=["audit.write"],
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
    assert results[0].output.diagnostics[0]["code"] == "permission_deny"
    assert not (tmp_path / "marker.txt").exists()
    assert agent.permission_checks[0][1].arguments["permissions"] == ["audit.write"]


def test_permission_gated_lifecycle_adapters_pass_runtime_boundary_to_permission_gate(
    tmp_path: Path,
) -> None:
    expected_boundary: dict[str, str] | None = None
    permission_arguments: dict[str, dict[str, object]] = {}

    class PromptLLM:
        def chat(self, _messages, **_kwargs):
            raise AssertionError("permission denial must block lifecycle prompt model call")

    prompt_agent = _DeclaredPermissionDenyingAdapterAgent()
    expected_boundary = _bind_adapter_runtime_boundary(prompt_agent, tmp_path)
    prompt_declaration = LifecycleHookDeclaration.from_dict(
        "hook:prompt-runtime-boundary",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="prompt",
            handler_ref="Decide whether the prompt may continue.",
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
        ),
    )
    prompt_dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([prompt_declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(
            prompt_llm=PromptLLM()
        ),
    )
    prompt_agent.lifecycle_dispatcher = prompt_dispatcher
    bind_lifecycle_runtime_adapters_to_agent(prompt_agent)
    prompt_dispatcher.dispatch(
        build_lifecycle_event_context("UserPromptSubmit", payload={"user_input": "x"})
    )
    permission_arguments["prompt"] = prompt_agent.permission_checks[0][1].arguments

    internal_agent = _DeclaredPermissionDenyingAdapterAgent()
    _bind_adapter_runtime_boundary(internal_agent, tmp_path)
    internal_declaration = LifecycleHookDeclaration.from_dict(
        "hook:internal-runtime-boundary",
        _declaration_payload(
            source="admin_managed",
            event="PreToolUse",
            handler_type="internal",
            handler_ref="admin_policy",
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
            technical={"output": {"diagnostics": [{"code": "admin_policy"}]}},
        ),
    )
    _agent_bound_dispatcher(internal_agent, [internal_declaration]).dispatch(
        build_lifecycle_event_context("PreToolUse")
    )
    permission_arguments["internal"] = (
        internal_agent.permission_checks[0][1].arguments
    )

    command_agent = _DeclaredPermissionDenyingAdapterAgent()
    _bind_adapter_runtime_boundary(command_agent, tmp_path)
    command_declaration = LifecycleHookDeclaration.from_dict(
        "hook:command-runtime-boundary",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="command",
            handler_ref=f'"{sys.executable}" -c "print(1)"',
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
        ),
    )
    _agent_bound_dispatcher(command_agent, [command_declaration]).dispatch(
        build_lifecycle_event_context("UserPromptSubmit", payload={"user_input": "x"})
    )
    permission_arguments["command"] = command_agent.permission_checks[0][1].arguments

    http_agent = _DeclaredPermissionDenyingAdapterAgent()
    _bind_adapter_runtime_boundary(http_agent, tmp_path)
    http_declaration = LifecycleHookDeclaration.from_dict(
        "hook:http-runtime-boundary",
        _declaration_payload(
            event="UserPromptSubmit",
            handler_type="http",
            handler_ref="http://127.0.0.1:1/hook",
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
        ),
    )
    _agent_bound_dispatcher(http_agent, [http_declaration]).dispatch(
        build_lifecycle_event_context("UserPromptSubmit", payload={"user_input": "x"})
    )
    permission_arguments["http"] = http_agent.permission_checks[0][1].arguments

    class MCPTool:
        name = "mcp__audit__record"
        tool_source = "mcp"
        server_name = "audit"

    class MCPAgent(_DeclaredPermissionDenyingAdapterAgent):
        def __init__(self) -> None:
            super().__init__()
            self.tool = MCPTool()

        def get_tool(self, name):
            return self.tool if name == self.tool.name else None

    mcp_agent = MCPAgent()
    _bind_adapter_runtime_boundary(mcp_agent, tmp_path)
    mcp_declaration = LifecycleHookDeclaration.from_dict(
        "hook:mcp-runtime-boundary",
        _declaration_payload(
            event="PostToolUse",
            handler_type="mcp_tool",
            handler_ref="mcp__audit__record",
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
            technical={"arguments": {"status": "done"}},
        ),
    )
    _agent_bound_dispatcher(mcp_agent, [mcp_declaration]).dispatch(
        build_lifecycle_event_context("PostToolUse", payload={"tool_names": ["shell"]})
    )
    permission_arguments["mcp_tool"] = mcp_agent.permission_checks[0][1].arguments

    class ControlPlane:
        def submit_agent_run(self, _request):
            raise AssertionError("permission denial must block child AgentRun submit")

    class AgentAdapterAgent(_DeclaredPermissionDenyingAdapterAgent):
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

    agent_adapter_agent = AgentAdapterAgent()
    _bind_adapter_runtime_boundary(agent_adapter_agent, tmp_path)
    agent_declaration = LifecycleHookDeclaration.from_dict(
        "hook:agent-runtime-boundary",
        _declaration_payload(
            event="Stop",
            handler_type="agent",
            handler_ref="reviewer",
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle output"},
        ),
    )
    _agent_bound_dispatcher(agent_adapter_agent, [agent_declaration]).dispatch(
        build_lifecycle_event_context("Stop")
    )
    permission_arguments["agent"] = (
        agent_adapter_agent.permission_checks[0][1].arguments
    )

    assert expected_boundary is not None
    for arguments in permission_arguments.values():
        _assert_permission_arguments_runtime_boundary(arguments, expected_boundary)


def test_command_lifecycle_runtime_adapter_blocks_runtime_budget_before_subprocess(
    tmp_path: Path,
) -> None:
    agent = _AllowingAdapterAgent()
    agent.runtime_working_directory = str(tmp_path)
    _exhaust_runtime_timeout(agent)
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
    assert results[0].output.diagnostics[0]["code"] == "runtime_budget_exceeded"
    assert not (tmp_path / "marker.txt").exists()


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


def test_command_lifecycle_runtime_adapter_uses_workspace_root_when_working_directory_missing(
    tmp_path: Path,
) -> None:
    agent = _AllowingAdapterAgent()
    agent.runtime_workspace_root = str(tmp_path)
    command = (
        f'"{sys.executable}" -c "import json, pathlib; '
        "print(json.dumps({'diagnostics': [{'code': 'cwd', 'cwd': str(pathlib.Path.cwd())}]}))"
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

    assert Path(results[0].output.diagnostics[0]["cwd"]) == tmp_path


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


def test_command_lifecycle_runtime_adapter_blocks_background_approval_without_prompting(
    tmp_path: Path,
) -> None:
    class ApprovalProvider:
        def request_approval(self, _request):
            raise AssertionError("background lifecycle adapter approval must not prompt")

    agent = _ApprovalRequiredAdapterAgent(
        approval_provider=ApprovalProvider(),
        runtime_working_directory=str(tmp_path),
    )
    agent.permission_interactive = False
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
    assert results[0].output.diagnostics[0]["code"] == "permission_blocked_review"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "blocked_review"
    assert not (tmp_path / "marker.txt").exists()


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


def test_command_lifecycle_runtime_adapter_fail_opens_observation_failure() -> None:
    agent = _AllowingAdapterAgent()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:command",
        _declaration_payload(
            event="PostToolUse",
            handler_type="command",
            handler_ref=f'"{sys.executable}" -c "import sys; sys.exit(7)"',
            trust="trusted",
            matcher="*",
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "PostToolUse",
            payload={"tool_names": ["read_file"]},
        )
    )

    output = results[0].output
    assert output.continue_flow is True
    assert output.decision == "none"
    assert output.diagnostics[0]["code"] == "command_nonzero_exit"
    assert output.diagnostics[0]["failure_policy"] == "fail_open"
    assert output.diagnostics[0]["event_name"] == "PostToolUse"


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


def test_http_lifecycle_runtime_adapter_fail_opens_observation_status_error() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(500)
            self.end_headers()

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
                event="PostToolUse",
                handler_type="http",
                handler_ref=f"http://127.0.0.1:{server.server_port}/hook",
                trust="trusted",
                matcher="*",
            ),
        )
        dispatcher = _agent_bound_dispatcher(agent, [declaration])

        results = dispatcher.dispatch(
            build_lifecycle_event_context(
                "PostToolUse",
                payload={"tool_names": ["read_file"]},
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    output = results[0].output
    assert output.continue_flow is True
    assert output.decision == "none"
    assert output.diagnostics[0]["code"] == "http_status_error"
    assert output.diagnostics[0]["failure_policy"] == "fail_open"


def test_http_lifecycle_runtime_adapter_enforces_declared_permissions_before_request() -> None:
    seen = {"called": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["called"] = True
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
        agent = _DeclaredPermissionDenyingAdapterAgent()
        declaration = LifecycleHookDeclaration.from_dict(
            "hook:http",
            _declaration_payload(
                event="UserPromptSubmit",
                handler_type="http",
                handler_ref=f"http://127.0.0.1:{server.server_port}/hook",
                permissions=["audit.write"],
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

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_deny"
    assert seen["called"] is False
    assert agent.permission_checks[0][1].arguments["permissions"] == ["audit.write"]


def test_http_lifecycle_runtime_adapter_blocks_background_approval_without_request() -> None:
    seen = {"called": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["called"] = True
            payload = json.dumps({"diagnostics": [{"code": "http_ok"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):  # noqa: D401
            return

    class ApprovalProvider:
        def request_approval(self, _request):
            raise AssertionError("background lifecycle http approval must not prompt")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = _ApprovalRequiredAdapterAgent(approval_provider=ApprovalProvider())
        agent.permission_interactive = False
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

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_blocked_review"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "blocked_review"
    assert seen["called"] is False


def test_http_lifecycle_runtime_adapter_blocks_runtime_budget_before_request() -> None:
    seen = {"called": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["called"] = True
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
        _exhaust_runtime_timeout(agent)
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

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "runtime_budget_exceeded"
    assert seen["called"] is False


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


def test_mcp_tool_lifecycle_runtime_adapter_fail_opens_observation_invalid_tool_output() -> None:
    class Tool:
        name = "mcp__audit__record"
        tool_source = "mcp"
        server_name = "audit"

        def execute(self, **_kwargs):
            return "not json"

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
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context("PostToolUse", payload={"tool_names": ["shell"]})
    )

    output = results[0].output
    assert output.continue_flow is True
    assert output.decision == "none"
    assert output.diagnostics[0]["code"] == "mcp_tool_output_invalid"
    assert output.diagnostics[0]["failure_policy"] == "fail_open"


def test_mcp_tool_lifecycle_runtime_adapter_suppresses_recursive_tool_lifecycle() -> None:
    recursive_events: list[str] = []

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

    class RecursiveProbeDispatcher:
        def dispatch(self, context):
            recursive_events.append(context.event_name)
            return []

    class Agent(_AllowingAdapterAgent):
        def __init__(self) -> None:
            super().__init__()
            self.tool = Tool()
            self.state = SimpleNamespace(current_round=0)
            self.active_mode = "coder"
            self.approval_provider = None
            self.events = []
            self.lifecycle_dispatcher = RecursiveProbeDispatcher()

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

    output = MCPToolLifecycleHookRuntimeAdapter(agent=agent).dispatch(
        declaration,
        build_lifecycle_event_context("PostToolUse", payload={"tool_names": ["shell"]}),
    )

    assert output.diagnostics == [{"code": "mcp_ok"}]
    assert agent.tool.arguments == {"status": "done"}
    assert recursive_events == []


def test_mcp_tool_lifecycle_runtime_adapter_enforces_declared_permissions_before_tool() -> None:
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

    class Agent(_DeclaredPermissionDenyingAdapterAgent):
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
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
            technical={"arguments": {"status": "done"}},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context("PostToolUse", payload={"tool_names": ["shell"]})
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_deny"
    assert agent.tool.arguments is None
    assert agent.permission_checks[0][1].arguments["permissions"] == ["audit.write"]


def test_mcp_tool_lifecycle_runtime_adapter_blocks_background_approval_without_tool() -> None:
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

    class ApprovalProvider:
        def request_approval(self, _request):
            raise AssertionError("background lifecycle mcp approval must not prompt")

    class Agent(_ApprovalRequiredAdapterAgent):
        def __init__(self) -> None:
            super().__init__(approval_provider=ApprovalProvider())
            self.permission_interactive = False
            self.tool = Tool()
            self.state = SimpleNamespace(current_round=0)
            self.active_mode = "coder"
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

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_blocked_review"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "blocked_review"
    assert agent.tool.arguments is None


def test_mcp_tool_lifecycle_runtime_adapter_blocks_runtime_budget_before_tool() -> None:
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
            _exhaust_runtime_timeout(self)

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

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "runtime_budget_exceeded"
    assert agent.tool.arguments is None


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


def _agent_adapter_task_lifecycle_dispatcher(agent, declarations, handler):
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry(declarations),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
            AgentLifecycleHookRuntimeAdapter(),
            FunctionLifecycleHookRuntimeAdapter("internal", handler),
        ]),
    )
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)
    return dispatcher


def test_agent_lifecycle_runtime_adapter_task_created_denial_blocks_submit() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.requests = []

        def submit_agent_run(self, request):
            self.requests.append(request)
            raise AssertionError("TaskCreated denial must block child AgentRun submit")

    class Agent(_AllowingAdapterAgent):
        def __init__(self) -> None:
            super().__init__()
            self._events = []
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

        def _emit_event(self, event):
            self._events.append(event)

    agent_declaration = LifecycleHookDeclaration.from_dict(
        "hook:agent",
        _declaration_payload(
            event="Stop",
            handler_type="agent",
            handler_ref="reviewer",
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle output"},
        ),
    )
    task_declaration = LifecycleHookDeclaration.from_dict(
        "hook:task-created",
        _declaration_payload(
            event="TaskCreated",
            handler_type="internal",
            handler_ref="task-created",
            source="admin_managed",
            trust="trusted",
            matcher="*",
        ),
    )

    def handler(_declaration, _context):
        return LifecycleHookOutput(
            decision="deny",
            user_message="Child AgentRun creation denied.",
        )

    agent = Agent()
    dispatcher = _agent_adapter_task_lifecycle_dispatcher(
        agent,
        [agent_declaration, task_declaration],
        handler,
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context("Stop", agent_run_id="parent-run")
    )

    assert results[0].output.decision == "deny"
    assert results[0].output.continue_flow is False
    assert results[0].output.user_message == "Child AgentRun creation denied."
    assert agent.agent_run_control_plane.requests == []
    lifecycle_events = [
        event.data
        for event in agent._events
        if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert lifecycle_events[-1]["event_name"] == "TaskCreated"
    assert lifecycle_events[-1]["decision"] == "deny"


def test_agent_lifecycle_runtime_adapter_emits_task_and_subagent_lifecycle_audit() -> None:
    class Run:
        id = "child-run"
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
            self._events = []
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

        def _emit_event(self, event):
            self._events.append(event)

    contexts = []
    agent_declaration = LifecycleHookDeclaration.from_dict(
        "hook:agent",
        _declaration_payload(
            event="Stop",
            handler_type="agent",
            handler_ref="reviewer",
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle output"},
        ),
    )
    task_declaration = LifecycleHookDeclaration.from_dict(
        "hook:task-created",
        _declaration_payload(
            event="TaskCreated",
            handler_type="internal",
            handler_ref="task-created",
            source="admin_managed",
            trust="trusted",
            matcher="*",
        ),
    )
    subagent_declaration = LifecycleHookDeclaration.from_dict(
        "hook:subagent-start",
        _declaration_payload(
            event="SubagentStart",
            handler_type="internal",
            handler_ref="subagent-start",
            source="admin_managed",
            trust="trusted",
            matcher="*",
        ),
    )

    def handler(_declaration, context):
        contexts.append(context)
        return LifecycleHookOutput(diagnostics=[{"code": context.event_name}])

    agent = Agent()
    dispatcher = _agent_adapter_task_lifecycle_dispatcher(
        agent,
        [agent_declaration, task_declaration, subagent_declaration],
        handler,
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "Stop",
            agent_run_id="parent-run",
            session_run_id="session-1",
            turn_id="turn-1",
        )
    )

    assert results[0].output.diagnostics[0]["code"] == "agent_run_submitted"
    assert [context.event_name for context in contexts] == [
        "TaskCreated",
        "SubagentStart",
    ]
    assert contexts[0].payload["agent_id"] == "reviewer"
    assert contexts[0].payload["parent_run_id"] == "parent-run"
    assert contexts[1].payload["child_agent_run_id"] == "child-run"
    lifecycle_events = [
        event.data
        for event in agent._events
        if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert [event["event_name"] for event in lifecycle_events] == [
        "TaskCreated",
        "SubagentStart",
    ]
    assert lifecycle_events[1]["payload"]["child_agent_run_id"] == "child-run"


def test_agent_lifecycle_runtime_adapter_enforces_declared_permissions_before_submit() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.requests = []

        def submit_agent_run(self, request):
            self.requests.append(request)
            raise AssertionError("declared permission denial must block AgentRun submit")

    class Agent(_DeclaredPermissionDenyingAdapterAgent):
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
            permissions=["audit.write"],
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle output"},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(build_lifecycle_event_context("Stop"))

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_deny"
    assert agent.agent_run_control_plane.requests == []
    assert agent.permission_checks[0][1].arguments["permissions"] == ["audit.write"]


def test_agent_lifecycle_runtime_adapter_blocks_background_approval_without_submit() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.requests = []

        def submit_agent_run(self, request):
            self.requests.append(request)
            raise AssertionError("background lifecycle agent approval must not submit")

    class ApprovalProvider:
        def request_approval(self, _request):
            raise AssertionError("background lifecycle agent approval must not prompt")

    class Agent(_ApprovalRequiredAdapterAgent):
        def __init__(self) -> None:
            super().__init__(approval_provider=ApprovalProvider())
            self.permission_interactive = False
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
            technical={"prompt": "review lifecycle output"},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(build_lifecycle_event_context("Stop"))

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "permission_blocked_review"
    assert results[0].output.diagnostics[0]["permission"]["action"] == "blocked_review"
    assert agent.agent_run_control_plane.requests == []


def test_agent_lifecycle_runtime_adapter_blocks_runtime_budget_before_submit() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.requests = []

        def submit_agent_run(self, request):
            self.requests.append(request)
            raise AssertionError("runtime budget exhaustion must block AgentRun submit")

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
            _exhaust_runtime_timeout(self)

    agent = Agent()
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:agent",
        _declaration_payload(
            event="Stop",
            handler_type="agent",
            handler_ref="reviewer",
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle output"},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(build_lifecycle_event_context("Stop"))

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "runtime_budget_exceeded"
    assert agent.agent_run_control_plane.requests == []


def test_agent_lifecycle_runtime_adapter_uses_executor_task_id_as_parent_run_id() -> None:
    class Run:
        id = "child-run"
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
            self.runtime_task_id = "parent-task"
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
            technical={"prompt": "review lifecycle output"},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    dispatcher.dispatch(build_lifecycle_event_context("Stop"))

    request = agent.agent_run_control_plane.requests[0]
    assert request.parent_run_id == "parent-task"
    assert request.delegated_by_run_id == "parent-task"


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

    assert results[0].output.continue_flow is True
    assert results[0].output.decision == "none"
    assert results[0].output.diagnostics[0]["code"] == "agent_not_found"
    assert results[0].output.diagnostics[0]["failure_policy"] == "fail_open"
    assert agent.agent_run_control_plane.requests == []


def test_agent_lifecycle_runtime_adapter_fail_closes_gate_unknown_target_agent() -> None:
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
            event="UserPromptSubmit",
            handler_type="agent",
            handler_ref="missing-agent",
            trust="trusted",
            matcher="*",
            technical={"prompt": "review lifecycle input"},
        ),
    )
    dispatcher = _agent_bound_dispatcher(agent, [declaration])

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert results[0].output.continue_flow is False
    assert results[0].output.decision == "deny"
    assert results[0].output.diagnostics[0]["code"] == "agent_not_found"
    assert results[0].output.diagnostics[0]["failure_policy"] == "fail_closed"
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


def test_internal_lifecycle_bridge_dispatches_session_start_hook_point() -> None:
    contexts: list[LifecycleHookEventContext] = []
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:session-start",
        {
            "event": "SessionStart",
            "source": "system_builtin",
            "placement": "server",
            "handler_type": "internal",
            "handler_ref": "session_start_observer",
            "display_name": "Session start observer",
            "summary": "Observes session startup through lifecycle.",
            "permissions": [],
            "trust": "trusted",
        },
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=_runtime_adapters(
            internal=lambda _declaration, context: (
                contexts.append(context)
                or LifecycleHookOutput.from_dict({"diagnostics": [{"code": "observed"}]})
            )
        ),
    )

    result = dispatch_internal_lifecycle_hook_point(
        dispatcher,
        HookPoint.SESSION_START,
        SessionStartContext(
            hook_point=HookPoint.SESSION_START,
            session_id="session-1",
            metadata={"source": "restore"},
        ),
        trigger_source="startup",
        origin="runner",
    )

    assert result.blocked is False
    assert result.diagnostics == [{"code": "observed"}]
    assert contexts[0].event_name == "SessionStart"
    assert contexts[0].session_run_id == "session-1"
    assert contexts[0].source == "startup"
    assert contexts[0].origin == "runner"
    assert contexts[0].metadata == {"source": "restore"}
    assert contexts[0].payload["technical"]["old_hook_point"] == "session_start"


def test_internal_lifecycle_bridge_dispatches_session_save_as_session_end() -> None:
    contexts: list[LifecycleHookEventContext] = []
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:session-end",
        {
            "event": "SessionEnd",
            "source": "system_builtin",
            "placement": "server",
            "handler_type": "internal",
            "handler_ref": "session_end_observer",
            "display_name": "Session end observer",
            "summary": "Observes session save through lifecycle.",
            "permissions": [],
            "trust": "trusted",
        },
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=_runtime_adapters(
            internal=lambda _declaration, context: (
                contexts.append(context) or LifecycleHookOutput()
            )
        ),
    )

    result = dispatch_internal_lifecycle_hook_point(
        dispatcher,
        HookPoint.SESSION_SAVE,
        SessionSaveContext(
            hook_point=HookPoint.SESSION_SAVE,
            session_id="session-2",
            session_data={"turns": 3},
        ),
        trigger_source="save",
        origin="session",
    )

    assert result.blocked is False
    assert contexts[0].event_name == "SessionEnd"
    assert contexts[0].session_run_id == "session-2"
    assert contexts[0].source == "save"
    assert contexts[0].origin == "session"
    assert contexts[0].payload["technical"]["old_hook_point"] == "session_save"


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


@pytest.mark.parametrize(
    "event_name",
    [
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PermissionRequest",
        "PreToolUse",
        "TaskCreated",
        "PreCompact",
    ],
)
@pytest.mark.parametrize(
    "terminal_output",
    [
        {"decision": "deny", "reason": "blocked"},
        {"decision": "defer", "reason": "needs review"},
        {"continue_flow": False, "reason": "stopped"},
    ],
)
def test_lifecycle_dispatcher_short_circuits_gate_events_on_terminal_output(
    event_name: str,
    terminal_output: dict,
) -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:blocker",
            _declaration_payload(
                event=event_name,
                handler_ref="blocker",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:side-effect",
            _declaration_payload(
                event=event_name,
                handler_ref="side-effect",
                matcher="*",
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, _context):
        calls.append(declaration.id)
        if declaration.id == "hook:blocker":
            return LifecycleHookOutput.from_dict(terminal_output)
        return LifecycleHookOutput.from_dict({"diagnostics": [{"code": "side_effect"}]})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    results = dispatcher.dispatch(
        LifecycleHookEventContext(
            event_name=event_name,
            placement="server",
            payload={"tool_names": ["deploy"], "user_input": "original"},
        )
    )

    assert calls == ["hook:blocker"]
    assert [result.declaration.id for result in results] == ["hook:blocker"]


def test_lifecycle_gate_policy_does_not_treat_ask_as_terminal() -> None:
    assert lifecycle_gate_output_is_terminal(LifecycleHookOutput(decision="ask")) is False


def test_lifecycle_dispatcher_continues_gate_events_after_ask_output() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:ask",
            _declaration_payload(
                event="PreToolUse",
                handler_ref="ask",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:observe",
            _declaration_payload(
                event="PreToolUse",
                handler_ref="observe",
                matcher="*",
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, _context):
        calls.append(declaration.id)
        if declaration.id == "hook:ask":
            return LifecycleHookOutput.from_dict(
                {"decision": "ask", "reason": "review requested"}
            )
        return LifecycleHookOutput.from_dict({"decision": "allow"})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "PreToolUse",
            payload={"tool_names": ["read_file"]},
        )
    )

    assert calls == ["hook:ask", "hook:observe"]
    assert [result.declaration.id for result in results] == [
        "hook:ask",
        "hook:observe",
    ]


def test_lifecycle_dispatcher_fail_closes_gate_handler_exception() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:broken-gate",
            _declaration_payload(
                event="PreToolUse",
                handler_ref="broken-gate",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:side-effect",
            _declaration_payload(
                event="PreToolUse",
                handler_ref="side-effect",
                matcher="*",
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, _context):
        calls.append(declaration.id)
        if declaration.id == "hook:broken-gate":
            raise RuntimeError("gate unavailable")
        return LifecycleHookOutput.from_dict({"decision": "allow"})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "PreToolUse",
            payload={"tool_names": ["read_file"]},
        )
    )

    assert calls == ["hook:broken-gate"]
    assert [result.declaration.id for result in results] == ["hook:broken-gate"]
    output = results[0].output
    assert output.continue_flow is False
    assert output.decision == "deny"
    assert output.diagnostics[0]["code"] == "lifecycle_hook_failed_closed"
    assert output.diagnostics[0]["event_name"] == "PreToolUse"


def test_lifecycle_dispatcher_fail_opens_observation_handler_exception() -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:broken-observer",
            _declaration_payload(
                event="PostToolUse",
                handler_ref="broken-observer",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:second-observer",
            _declaration_payload(
                event="PostToolUse",
                handler_ref="second-observer",
                matcher="*",
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, _context):
        calls.append(declaration.id)
        if declaration.id == "hook:broken-observer":
            raise RuntimeError("observer unavailable")
        return LifecycleHookOutput.from_dict({"diagnostics": [{"code": "second"}]})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "PostToolUse",
            payload={"tool_names": ["read_file"]},
        )
    )

    assert calls == ["hook:broken-observer", "hook:second-observer"]
    assert [result.declaration.id for result in results] == [
        "hook:broken-observer",
        "hook:second-observer",
    ]
    failed_output = results[0].output
    assert failed_output.continue_flow is True
    assert failed_output.decision == "none"
    assert failed_output.diagnostics[0]["code"] == "lifecycle_hook_failed_open"
    assert failed_output.diagnostics[0]["event_name"] == "PostToolUse"


@pytest.mark.parametrize(
    ("event_name", "payload", "updated_input"),
    [
        (
            "UserPromptSubmit",
            {"user_input": "original"},
            {"user_input": "rewritten"},
        ),
        (
            "PreToolUse",
            {
                "tool_names": ["read_file"],
                "tool_call_ids": ["call-read"],
                "technical": {
                    "tool_call": {
                        "id": "call-read",
                        "name": "read_file",
                        "arguments": {},
                    }
                },
            },
            {
                "tool_call": {
                    "id": "call-read",
                    "name": "apply_patch",
                    "arguments": {},
                }
            },
        ),
    ],
)
def test_lifecycle_dispatcher_does_not_apply_updated_input_from_terminal_gate_output(
    event_name: str,
    payload: dict,
    updated_input: dict,
) -> None:
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:blocker",
            _declaration_payload(
                event=event_name,
                handler_ref="blocker",
                matcher="*",
                trust="trusted",
            ),
        )
    ])

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(
            mcp_tool=lambda _declaration, _context: LifecycleHookOutput.from_dict(
                {
                    "decision": "deny",
                    "reason": "blocked",
                    "updated_input": updated_input,
                }
            )
        ),
    )
    context = build_lifecycle_event_context(event_name, payload=payload)

    dispatcher.dispatch(context)

    if event_name == "UserPromptSubmit":
        assert context.payload["user_input"] == "original"
    else:
        assert context.payload["tool_names"] == ["read_file"]
        assert context.payload["technical"]["tool_call"]["name"] == "read_file"


def test_lifecycle_dispatcher_passes_updated_user_input_to_next_gate_hook() -> None:
    seen_user_inputs: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:rewrite",
            _declaration_payload(
                event="UserPromptSubmit",
                handler_ref="rewrite",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:observe",
            _declaration_payload(
                event="UserPromptSubmit",
                handler_ref="observe",
                matcher="*",
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, context):
        if declaration.id == "hook:rewrite":
            return LifecycleHookOutput.from_dict(
                {"updated_input": {"user_input": "rewritten"}}
            )
        seen_user_inputs.append(context.payload["user_input"])
        return LifecycleHookOutput.from_dict({"decision": "allow"})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptSubmit",
            payload={"user_input": "original"},
        )
    )

    assert seen_user_inputs == ["rewritten"]


def test_lifecycle_dispatcher_passes_updated_command_text_to_next_expansion_hook() -> None:
    seen_command_texts: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:rewrite",
            _declaration_payload(
                event="UserPromptExpansion",
                handler_ref="rewrite",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:observe",
            _declaration_payload(
                event="UserPromptExpansion",
                handler_ref="observe",
                matcher="*",
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, context):
        if declaration.id == "hook:rewrite":
            return LifecycleHookOutput.from_dict(
                {"updated_input": {"command_text": "/safe"}}
            )
        seen_command_texts.append(context.payload["command_text"])
        return LifecycleHookOutput.from_dict({"decision": "allow"})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    dispatcher.dispatch(
        build_lifecycle_event_context(
            "UserPromptExpansion",
            payload={"command_text": "/unsafe"},
        )
    )

    assert seen_command_texts == ["/safe"]


@pytest.mark.parametrize(
    ("event_name", "updated_input"),
    [
        ("PermissionRequest", {"tool_names": ["shell"]}),
        ("UserPromptSubmit", {"tool_names": ["shell"]}),
        ("PreToolUse", {"tool_names": ["shell"]}),
    ],
)
def test_lifecycle_dispatcher_does_not_consume_ignored_updated_input_fields(
    event_name: str,
    updated_input: dict,
) -> None:
    calls: list[str] = []
    diagnostics_by_hook: dict[str, list[object]] = {}
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:rewrite",
            _declaration_payload(
                event=event_name,
                handler_ref="rewrite",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:shell-only",
            _declaration_payload(
                event=event_name,
                handler_ref="shell-only",
                matcher={"tool_names": "shell"},
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, _context):
        calls.append(declaration.id)
        if declaration.id == "hook:rewrite":
            output = LifecycleHookOutput.from_dict({"updated_input": updated_input})
            annotate_lifecycle_output_diagnostics(event_name, output)
            diagnostics_by_hook[declaration.id] = list(output.diagnostics)
            return output
        return LifecycleHookOutput.from_dict({"diagnostics": [{"code": "side_effect"}]})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )
    context = build_lifecycle_event_context(
        event_name,
        payload={"tool_names": ["read_file"], "user_input": "original"},
    )

    results = dispatcher.dispatch(context)

    assert calls == ["hook:rewrite"]
    assert [result.declaration.id for result in results] == ["hook:rewrite"]
    assert context.payload["tool_names"] == ["read_file"]
    assert diagnostics_by_hook["hook:rewrite"]


def test_lifecycle_dispatcher_passes_updated_tool_call_to_next_pre_tool_hook() -> None:
    seen_tool_names: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:rewrite",
            _declaration_payload(
                event="PreToolUse",
                handler_ref="rewrite",
                matcher="*",
                trust="trusted",
            ),
        ),
        LifecycleHookDeclaration.from_dict(
            "hook:observe",
            _declaration_payload(
                event="PreToolUse",
                handler_ref="observe",
                matcher={"tool_names": "apply_patch"},
                trust="trusted",
            ),
        ),
    ])

    def handler(declaration, context):
        if declaration.id == "hook:rewrite":
            return LifecycleHookOutput.from_dict(
                {
                    "updated_input": {
                        "tool_call": {
                            "id": "call-original",
                            "name": "apply_patch",
                            "arguments": {"file_path": "out.txt"},
                        }
                    }
                }
            )
        seen_tool_names.append(context.payload["tool_names"][0])
        return LifecycleHookOutput.from_dict({"decision": "allow"})

    dispatcher = LifecycleHookDispatcher(
        registry,
        runtime_adapters=_runtime_adapters(mcp_tool=handler),
    )

    dispatcher.dispatch(
        build_lifecycle_event_context(
            "PreToolUse",
            payload={
                "tool_names": ["read_file"],
                "tool_call_ids": ["call-original"],
                "technical": {
                    "tool_call": {
                        "id": "call-original",
                        "name": "read_file",
                        "arguments": {},
                    }
                },
            },
        )
    )

    assert seen_tool_names == ["apply_patch"]


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
                matcher={"tool_names": ["apply_patch", "deploy"]},
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
                permissions=[],
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


def test_lifecycle_failure_and_batch_builders_emit_list_tool_fields_only() -> None:
    first_call = type(
        "_ToolCall",
        (),
        {"id": "call-shell", "name": "shell", "arguments": {"command": "pwd"}},
    )()
    second_call = type(
        "_ToolCall",
        (),
        {"id": "call-read", "name": "read_file", "arguments": {"path": "README.md"}},
    )()
    tool = type(
        "_Tool",
        (),
        {
            "name": "shell",
            "description": "Run shell commands.",
            "parameters": {},
            "server_name": "github",
        },
    )()

    failure_payload = build_tool_lifecycle_payload(
        "PostToolUseFailure",
        tool_call=first_call,
        tool=tool,
        tool_source="mcp",
        error={"message": "failed"},
    )
    batch_payload = build_tool_batch_lifecycle_payload(
        tool_calls=[first_call, second_call],
        results=["failed", "ok"],
        tool_sources=["mcp", "builtin"],
        mcp_servers=["github"],
    )

    assert failure_payload["tool_names"] == ["shell"]
    assert failure_payload["tool_call_ids"] == ["call-shell"]
    assert failure_payload["tool_sources"] == ["mcp"]
    assert failure_payload["mcp_servers"] == ["github"]
    assert failure_payload["technical"]["error"] == {"message": "failed"}
    assert batch_payload["tool_names"] == ["shell", "read_file"]
    assert batch_payload["tool_call_ids"] == ["call-shell", "call-read"]
    assert batch_payload["tool_sources"] == ["mcp", "builtin"]
    assert batch_payload["mcp_servers"] == ["github"]
    assert batch_payload["technical"]["results"] == ["failed", "ok"]
    for payload in (failure_payload, batch_payload):
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


@pytest.mark.parametrize(
    "event_name",
    ["PermissionRequest", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch"],
)
def test_lifecycle_matcher_ignores_technical_details_even_when_names_match(
    event_name: str,
) -> None:
    calls: list[str] = []
    registry = LifecycleHookRegistry([
        LifecycleHookDeclaration.from_dict(
            "hook:deploy",
            _declaration_payload(
                event=event_name,
                trust="trusted",
                matcher={"tool_names": "deploy"},
            ),
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
            event_name,
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
                matcher={"tool_names": ["apply_patch", "apply_patch"]},
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
                    type("_ToolCall", (), {"id": "call-b", "name": "apply_patch", "arguments": {}})(),
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


def test_build_lifecycle_event_context_payload_cannot_override_authoritative_fields() -> None:
    context = build_lifecycle_event_context(
        "PreToolUse",
        placement="server",
        trigger_source="chat",
        session_run_id="session-real",
        agent_run_id="run-real",
        turn_id="turn-real",
        origin="agent",
        locale="en-US",
        metadata={"source": "metadata-source"},
        payload={
            "event_name": "PostToolUse",
            "placement": "peer",
            "trigger_source": "payload-trigger",
            "session_run_id": "session-payload",
            "agent_run_id": "run-payload",
            "turn_id": "turn-payload",
            "source": "payload-source",
            "origin": "payload-origin",
            "locale": "zh-CN",
            "metadata": {"unsafe": True},
            "safe": "kept",
        },
    )

    assert context.event_name == "PreToolUse"
    assert context.placement == "server"
    assert context.source == "chat"
    assert context.session_run_id == "session-real"
    assert context.agent_run_id == "run-real"
    assert context.turn_id == "turn-real"
    assert context.origin == "agent"
    assert context.locale == "en-US"
    assert context.metadata == {"source": "metadata-source"}
    assert context.payload == {
        "safe": "kept",
        "event_name": "PreToolUse",
        "placement": "server",
        "trigger_source": "chat",
        "session_run_id": "session-real",
        "agent_run_id": "run-real",
        "turn_id": "turn-real",
        "timestamp": context.timestamp,
    }


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


def test_config_derived_lifecycle_dispatcher_executes_only_trusted_public_hooks() -> None:
    def hooks(prefix: str) -> list[dict[str, object]]:
        return [
            {
                "event": "PreToolUse",
                "handler_type": "prompt",
                "handler_ref": f"{prefix}:{trust}",
                "display_name": f"{prefix} {trust}",
                "summary": f"{prefix} {trust} hook.",
                "permissions": [],
                "trust": trust,
            }
            for trust in ("pending_review", "trusted", "disabled", "blocked")
        ]

    config = SimpleNamespace(
        skills=SkillsConfig(
            items={
                "code-review": SkillRegistrationConfig(
                    name="code-review",
                    hooks=hooks("skill"),
                )
            }
        ),
        mcp_servers=[
            MCPServerConfig(
                name="github",
                command="github-mcp-server",
                hooks=hooks("mcp"),
            )
        ],
        capability_packages={
            "review": CapabilityPackageConfig(
                id="review",
                hooks=hooks("package"),
            )
        },
        capability_components={
            "skill:package-review": CapabilityComponentConfig(
                id="skill:package-review",
                kind="skill",
                name="package-review",
                hooks=hooks("component"),
            )
        },
    )
    calls: list[str] = []

    def handler(declaration, context):  # noqa: ARG001
        calls.append(declaration.handler_ref)
        return LifecycleHookOutput.from_dict({"diagnostics": [{"code": "seen"}]})

    dispatcher = LifecycleHookDispatcher(
        lifecycle_registry_from_config(config),
        runtime_adapters=_runtime_adapters(prompt=handler),
    )

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "PreToolUse",
            placement="server",
            payload={"tool_names": ["read_file"]},
        )
    )

    assert set(calls) == {
        "skill:trusted",
        "mcp:trusted",
        "package:trusted",
        "component:trusted",
    }
    assert {result.declaration.trust for result in results} == {"trusted"}


def test_lifecycle_registry_keeps_mcp_tool_hooks_separate_from_memory_provider_adapters() -> None:
    config = SimpleNamespace(
        skills=SimpleNamespace(items={}),
        mcp_servers=[
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
        memory=SimpleNamespace(
            providers={
                "github_memory": SimpleNamespace(
                    adapter="mcp_memory",
                    mcp_server="github",
                    hooks=[
                        {
                            "event": "UserPromptSubmit",
                            "handler_type": "mcp_tool",
                            "handler_ref": "github.memory_audit",
                            "display_name": "Memory audit",
                            "summary": "This must not become a lifecycle hook.",
                            "permissions": ["memory.read"],
                        }
                    ],
                )
            }
        ),
        capability_packages={},
        capability_components={},
    )

    registry = lifecycle_registry_from_config(config)
    items = {item["id"]: item for item in registry.dashboard_items()}

    assert list(items) == ["hook:mcp_server:github:PostToolUse:0"]
    assert items["hook:mcp_server:github:PostToolUse:0"]["source"] == "mcp_server"
    assert all(item["source"] != "memory_provider" for item in items.values())
    assert "github_memory" not in " ".join(items)


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


def test_lifecycle_registry_gates_capability_package_hooks_by_activation_state() -> None:
    hook = {
        "event": "PreToolUse",
        "handler_type": "agent",
        "handler_ref": "agent:policy",
        "display_name": "Policy",
        "summary": "Checks tool use.",
        "permissions": [],
        "trust": "trusted",
    }
    inactive_config = SimpleNamespace(
        skills=SkillsConfig(items={}),
        mcp_servers=[],
        capability_packages={
            "review": CapabilityPackageConfig(
                id="review",
                enabled=False,
                status="installed",
                hooks=[hook],
            )
        },
        capability_components={},
    )
    active_config = SimpleNamespace(
        skills=SkillsConfig(items={}),
        mcp_servers=[],
        capability_packages={
            "review": CapabilityPackageConfig(
                id="review",
                enabled=True,
                status="installed",
                hooks=[hook],
            )
        },
        capability_components={},
    )

    inactive_registry = lifecycle_registry_from_config(inactive_config)
    inactive_items = {
        item["id"]: item for item in inactive_registry.dashboard_items()
    }
    inactive_hook = inactive_items["hook:capability_package:review:PreToolUse:0"]

    assert inactive_hook["owner_activation_state"] == "inactive"
    assert inactive_hook["executable"] is False
    assert inactive_hook["unavailable_reason"] == "owner_activation:inactive"
    assert inactive_registry.executable(event="PreToolUse", placement="server") == []

    active_registry = lifecycle_registry_from_config(active_config)
    assert [
        item.id
        for item in active_registry.executable(
            event="PreToolUse",
            placement="server",
            runtime_adapters=_runtime_adapters(
                agent=lambda _declaration, _context: LifecycleHookOutput()
            ),
        )
    ] == ["hook:capability_package:review:PreToolUse:0"]


def test_lifecycle_registry_gates_package_component_hooks_by_owner_activation() -> None:
    hook = {
        "event": "PreToolUse",
        "handler_type": "agent",
        "handler_ref": "agent:policy",
        "display_name": "Policy",
        "summary": "Checks tool use.",
        "permissions": [],
        "trust": "trusted",
    }
    config = SimpleNamespace(
        skills=SkillsConfig(items={}),
        mcp_servers=[],
        capability_packages={
            "review": CapabilityPackageConfig(
                id="review",
                enabled=False,
                status="installed",
                components=["skill:package-review"],
            )
        },
        capability_components={
            "skill:package-review": CapabilityComponentConfig(
                id="skill:package-review",
                kind="skill",
                name="package-review",
                enabled=True,
                status="installed",
                package_ids=["review"],
                hooks=[hook],
            )
        },
    )

    registry = lifecycle_registry_from_config(config)
    items = {item["id"]: item for item in registry.dashboard_items()}
    component_hook = items["hook:skill:skill:package-review:PreToolUse:0"]

    assert component_hook["owner_enabled"] is True
    assert component_hook["owner_activation_state"] == "inactive"
    assert component_hook["executable"] is False
    assert component_hook["unavailable_reason"] == "owner_activation:inactive"
    assert registry.executable(event="PreToolUse", placement="server") == []


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
