from __future__ import annotations

import pytest

from reuleauxcoder.domain.hooks.builtin.memory_context import (
    MemoryContextHook,
    MemorySessionSaveHook,
    MemoryToolCaptureHook,
)
from reuleauxcoder.domain.hooks.discovery import discover_hook_specs
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    HookPoint,
    SessionSaveContext,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.memory import (
    MemoryBundle,
    MemoryBundleFragment,
    MemoryCaptureEvent,
    MemoryProviderUnavailable,
    MemoryProvideRequest,
    MemoryScope,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


def _context(metadata: dict) -> BeforeLLMRequestContext:
    return BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[
            {"role": "system", "content": "system"},
            {"role": "system", "content": "[Project Context]\nproject"},
            {"role": "user", "content": "What should I remember?"},
        ],
        metadata=metadata,
    )


class RecordingRuntime:
    token_budget_default = 800

    def __init__(self) -> None:
        self.scopes: list[MemoryScope] = []
        self.requests: list[MemoryProvideRequest] = []
        self.policies: list[dict | None] = []

    def provide_for_llm_request(
        self, scope: MemoryScope, request: MemoryProvideRequest, *, policy=None
    ) -> MemoryBundle:
        self.scopes.append(scope)
        self.requests.append(request)
        self.policies.append(policy)
        return MemoryBundle(
            scope=scope,
            fragments=[
                MemoryBundleFragment(
                    id="mem-1",
                    text="This agent prefers pytest for verification.",
                    source_provider="fake",
                    source_kind="project",
                    trust_tier="user",
                    score=1.0,
                    token_estimate=12,
                )
            ],
            token_estimate=12,
            provenance={"scope_version": 7},
        )


class UnavailableRuntime:
    token_budget_default = 800

    def provide_for_llm_request(
        self, scope: MemoryScope, request: MemoryProvideRequest, *, policy=None
    ) -> MemoryBundle:
        raise MemoryProviderUnavailable("provider unavailable")


class EmptyRuntime:
    token_budget_default = 800

    def provide_for_llm_request(
        self, scope: MemoryScope, request: MemoryProvideRequest, *, policy=None
    ) -> MemoryBundle:
        return MemoryBundle(
            scope=scope,
            fragments=[],
            token_estimate=0,
            provenance={"scope_version": 3},
        )


class CaptureRuntime:
    token_budget_default = 800
    capture_default = True

    def __init__(self) -> None:
        self.captures: list[tuple[MemoryScope, MemoryCaptureEvent, dict | None]] = []

    def capture_event(
        self,
        scope: MemoryScope,
        event: MemoryCaptureEvent,
        *,
        policy=None,
    ) -> None:
        self.captures.append((scope, event, policy))


def test_memory_context_hook_injects_after_existing_system_messages() -> None:
    runtime = RecordingRuntime()
    hook = MemoryContextHook(runtime=runtime)
    context = _context(
        {
            "owner_agent_id": "agent-a",
            "memory_namespace": "agent-a",
            "project_id": "project-1",
            "workspace_id": "workspace-1",
        }
    )

    result = hook.run(context)

    assert result.messages[2]["role"] == "system"
    assert "Private Agent Memory" in result.messages[2]["content"]
    assert "pytest for verification" in result.messages[2]["content"]
    assert result.messages[3]["role"] == "user"
    assert runtime.scopes[0].owner_agent_id == "agent-a"
    assert runtime.requests[0].query == "What should I remember?"
    assert result.metadata["memory"]["provided_items"] == 1
    assert result.metadata["memory"]["scope_version"] == 7


def test_memory_context_hook_delegates_policy_to_runtime() -> None:
    runtime = RecordingRuntime()
    hook = MemoryContextHook(runtime=runtime)

    result = hook.run(_context({
        "owner_agent_id": "agent-a",
        "memory_namespace": "agent-a",
        "memory_policy": {
            "primary_provider": "project-index",
            "read_providers": ["project-index"],
            "capture": False,
        },
    }))

    assert result.metadata["memory"]["status"] == "provided"
    assert runtime.policies == [{
        "primary_provider": "project-index",
        "read_providers": ["project-index"],
        "capture": False,
    }]


def test_memory_context_hook_emits_memory_context_event_with_rendered_prompt() -> None:
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)
    hook = MemoryContextHook(runtime=RecordingRuntime())
    context = _context(
        {
            "owner_agent_id": "agent-a",
            "memory_namespace": "agent-a",
            "project_id": "project-1",
            "round_index": 2,
        }
    )
    context.ui_bus = ui_bus

    result = hook.run(context)

    memory_events = [
        event
        for event in seen
        if event.kind == UIEventKind.CONTEXT
        and event.data.get("schema") == "memory_context.v1"
    ]
    assert len(memory_events) == 1
    payload = memory_events[0].data
    assert payload["context_kind"] == "memory_injection"
    assert payload["status"] == "provided"
    assert payload["round_index"] == 2
    assert payload["provided_items"] == 1
    assert payload["token_estimate"] == 12
    assert payload["scope"]["owner_agent_id"] == "agent-a"
    assert payload["scope_version"] == 7
    assert payload["fragments"][0]["source_kind"] == "project"
    assert payload["fragments"][0]["text"] == "This agent prefers pytest for verification."
    assert payload["rendered_context"] == result.messages[2]["content"]


def test_memory_context_hook_does_not_emit_event_for_empty_memory() -> None:
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)
    hook = MemoryContextHook(runtime=EmptyRuntime())
    context = _context({"owner_agent_id": "agent-a", "memory_namespace": "agent-a"})
    context.ui_bus = ui_bus

    result = hook.run(context)

    assert len(result.messages) == 3
    assert result.metadata["memory"]["status"] == "empty"
    assert seen == []


def test_memory_context_hook_fail_closed_without_owner_agent_id() -> None:
    hook = MemoryContextHook(runtime=RecordingRuntime())

    with pytest.raises(ValueError, match="owner_agent_id"):
        hook.run(_context({"project_id": "project-1"}))


def test_memory_context_hook_skips_when_provider_is_unavailable() -> None:
    hook = MemoryContextHook(runtime=UnavailableRuntime())
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)
    context = _context({"owner_agent_id": "agent-a", "memory_namespace": "agent-a"})
    context.ui_bus = ui_bus

    result = hook.run(context)

    assert len(result.messages) == 3
    assert result.metadata["memory"]["status"] == "unavailable"
    assert "provider unavailable" in result.metadata["memory"]["warning"]
    assert seen == []


def test_memory_session_save_hook_delegates_capture_to_memory_runtime() -> None:
    runtime = CaptureRuntime()
    hook = MemorySessionSaveHook(
        runtime=runtime,
        default_agent_id="agent-default",
        default_namespace="default-ns",
    )

    hook.run(
        SessionSaveContext(
            hook_point=HookPoint.SESSION_SAVE,
            session_id="session-1",
            session_data={
                "session_id": "session-1",
                "messages": [{"role": "user", "content": "remember this"}],
                "memory_scope": {
                    "owner_agent_id": "agent-a",
                    "memory_namespace": "agent-a",
                    "project_id": "project-1",
                },
            },
            metadata={
                "memory_policy": {
                    "primary_provider": "project-index",
                    "capture": True,
                }
            },
        )
    )

    assert len(runtime.captures) == 1
    scope, event, policy = runtime.captures[0]
    assert scope.owner_agent_id == "agent-a"
    assert scope.memory_namespace == "agent-a"
    assert scope.project_id == "project-1"
    assert event.kind == "session_save"
    assert event.payload["session_id"] == "session-1"
    assert event.idempotency_key == "session_save:agent-a:agent-a:session-1"
    assert policy == {"primary_provider": "project-index", "capture": True}


def test_memory_tool_capture_hook_delegates_capture_to_memory_runtime() -> None:
    runtime = CaptureRuntime()
    hook = MemoryToolCaptureHook(
        runtime=runtime,
        default_agent_id="agent-default",
        default_namespace="default-ns",
    )

    hook.run(
        AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            session_id="session-1",
            tool_call=ToolCall(
                id="call-1",
                name="read_file",
                arguments={"path": "README.md"},
            ),
            result="README contents",
            round_index=3,
            metadata={
                "owner_agent_id": "agent-a",
                "memory_namespace": "agent-a",
                "memory_policy": {
                    "primary_provider": "project-index",
                    "capture": True,
                },
            },
        )
    )

    assert len(runtime.captures) == 1
    scope, event, policy = runtime.captures[0]
    assert scope.owner_agent_id == "agent-a"
    assert scope.memory_namespace == "agent-a"
    assert event.kind == "tool_result"
    assert event.payload["session_id"] == "session-1"
    assert event.payload["round_index"] == 3
    assert event.payload["tool_call"] == {
        "id": "call-1",
        "name": "read_file",
        "arguments": {"path": "README.md"},
    }
    assert event.payload["result"] == "README contents"
    assert event.idempotency_key == "tool_result:agent-a:agent-a:session-1:call-1"
    assert policy == {"primary_provider": "project-index", "capture": True}


def test_memory_hooks_are_builtin_core_hooks() -> None:
    specs = discover_hook_specs()

    names = {spec.hook_class.__name__ for spec in specs}
    assert "MemoryContextHook" in names
    assert "MemorySessionSaveHook" in names
    assert "MemoryToolCaptureHook" in names
    assert "ToolPolicyGuardHook" not in names
