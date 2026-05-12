from __future__ import annotations

import pytest

from reuleauxcoder.domain.hooks.builtin.memory_context import MemoryContextHook
from reuleauxcoder.domain.hooks.discovery import discover_hook_specs
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.domain.memory import (
    MemoryBackendUnavailable,
    MemoryBundle,
    MemoryItem,
    MemoryProvideRequest,
    MemoryScope,
)


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


class RecordingProvider:
    def __init__(self) -> None:
        self.scopes: list[MemoryScope] = []
        self.requests: list[MemoryProvideRequest] = []

    def provide(
        self, scope: MemoryScope, request: MemoryProvideRequest
    ) -> MemoryBundle:
        self.scopes.append(scope)
        self.requests.append(request)
        return MemoryBundle(
            scope=scope,
            items=[
                MemoryItem.create(
                    scope=scope,
                    type="project",
                    abstract="Testing preference",
                    content="This agent prefers pytest for verification.",
                )
            ],
            token_estimate=12,
            provenance={"scope_version": 7},
        )


class UnavailableProvider:
    def provide(
        self, scope: MemoryScope, request: MemoryProvideRequest
    ) -> MemoryBundle:
        raise MemoryBackendUnavailable("database unavailable")


def test_memory_context_hook_injects_after_existing_system_messages() -> None:
    provider = RecordingProvider()
    hook = MemoryContextHook(provider=provider)
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
    assert provider.scopes[0].owner_agent_id == "agent-a"
    assert provider.requests[0].query == "What should I remember?"
    assert result.metadata["memory"]["provided_items"] == 1
    assert result.metadata["memory"]["scope_version"] == 7


def test_memory_context_hook_fail_closed_without_owner_agent_id() -> None:
    hook = MemoryContextHook(provider=RecordingProvider())

    with pytest.raises(ValueError, match="owner_agent_id"):
        hook.run(_context({"project_id": "project-1"}))


def test_memory_context_hook_skips_when_backend_is_unavailable() -> None:
    hook = MemoryContextHook(provider=UnavailableProvider())
    context = _context({"owner_agent_id": "agent-a", "memory_namespace": "agent-a"})

    result = hook.run(context)

    assert len(result.messages) == 3
    assert result.metadata["memory"]["status"] == "unavailable"
    assert "database unavailable" in result.metadata["memory"]["warning"]


def test_memory_hooks_are_builtin_core_hooks() -> None:
    specs = discover_hook_specs()

    names = {spec.hook_class.__name__ for spec in specs}
    assert "MemoryContextHook" in names
    assert "MemorySessionSaveHook" in names
    assert "MemoryToolCaptureHook" in names
