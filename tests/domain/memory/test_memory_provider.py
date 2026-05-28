from __future__ import annotations

import pytest

from reuleauxcoder.domain.memory import (
    MemoryBundle,
    MemoryBundleFragment,
    MemoryCaptureEvent,
    MemoryCaptureReceipt,
    MemoryForgetSelector,
    MemoryMutationResult,
    MemoryProviderCapabilities,
    MemoryProviderConfigurationError,
    MemoryProviderRegistry,
    MemoryProviderStatus,
    MemoryProvideRequest,
    MemoryRememberItem,
    MemoryRuntime,
    MemoryScope,
    MemoryToolSurfacePolicy,
)


@pytest.fixture(autouse=True)
def reset_memory_adapters() -> None:
    MemoryProviderRegistry.clear_registered_adapters()
    yield
    MemoryProviderRegistry.clear_registered_adapters()


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        *,
        capabilities: MemoryProviderCapabilities | None = None,
        fragments: list[MemoryBundleFragment] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.capabilities = capabilities or MemoryProviderCapabilities(
            provide=True,
            capture=True,
            remember=True,
            forget=True,
        )
        self.fragments = list(fragments or [])
        self.captured: list[MemoryCaptureEvent] = []
        self.remembered: list[MemoryRememberItem] = []
        self.forgotten: list[MemoryForgetSelector] = []

    def health(self, scope: MemoryScope) -> MemoryProviderStatus:
        return MemoryProviderStatus(
            provider_id=self.provider_id,
            available=True,
            capabilities=self.capabilities,
        )

    def provide(
        self, scope: MemoryScope, request: MemoryProvideRequest
    ) -> MemoryBundle:
        return MemoryBundle(scope=scope, fragments=list(self.fragments))

    def capture(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt:
        self.captured.append(event)
        return MemoryCaptureReceipt(
            provider_id=self.provider_id,
            accepted=True,
            status="accepted",
        )

    def remember(
        self, scope: MemoryScope, item: MemoryRememberItem
    ) -> MemoryMutationResult:
        self.remembered.append(item)
        return MemoryMutationResult(
            provider_id=self.provider_id,
            accepted=True,
            status="accepted",
            item_ids=["remembered"],
        )

    def forget(
        self, scope: MemoryScope, selector: MemoryForgetSelector
    ) -> MemoryMutationResult:
        self.forgotten.append(selector)
        return MemoryMutationResult(
            provider_id=self.provider_id,
            accepted=True,
            status="accepted",
            item_ids=["forgotten"],
        )


class FailingProvider(FakeProvider):
    def __init__(self, provider_id: str, *, fail_on: str) -> None:
        super().__init__(provider_id)
        self.fail_on = fail_on

    def provide(
        self, scope: MemoryScope, request: MemoryProvideRequest
    ) -> MemoryBundle:
        if self.fail_on == "provide":
            raise RuntimeError("provide failed")
        return super().provide(scope, request)

    def capture(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt:
        if self.fail_on == "capture":
            raise RuntimeError("capture failed")
        return super().capture(scope, event)

    def remember(
        self, scope: MemoryScope, item: MemoryRememberItem
    ) -> MemoryMutationResult:
        if self.fail_on == "remember":
            raise RuntimeError("remember failed")
        return super().remember(scope, item)

    def forget(
        self, scope: MemoryScope, selector: MemoryForgetSelector
    ) -> MemoryMutationResult:
        if self.fail_on == "forget":
            raise RuntimeError("forget failed")
        return super().forget(scope, selector)


def test_registry_has_no_core_default_provider_or_sqlite_fallback() -> None:
    registry = MemoryProviderRegistry({"main": {"adapter": "missing"}})

    with pytest.raises(MemoryProviderConfigurationError, match="not registered"):
        registry.provider("main")

    assert MemoryProviderRegistry.registered_adapters() == set()


def test_runtime_requires_registered_default_provider_when_enabled() -> None:
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "missing"}}),
        default_provider="main",
    )

    with pytest.raises(MemoryProviderConfigurationError, match="not registered"):
        runtime.validate_ready()


def test_runtime_rejects_unknown_default_provider_without_fallback() -> None:
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({}),
        default_provider="missing",
    )

    with pytest.raises(MemoryProviderConfigurationError, match="not configured"):
        runtime.validate_ready()


def test_runtime_validate_ready_checks_default_provider_capabilities() -> None:
    provider = FakeProvider(
        "main",
        capabilities=MemoryProviderCapabilities(provide=False, capture=True),
    )
    MemoryProviderRegistry.register_adapter("fake", lambda provider_id, config: provider)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "fake"}}),
        default_provider="main",
        inject_default=True,
    )

    with pytest.raises(MemoryProviderConfigurationError, match="does not support provide"):
        runtime.validate_ready()


def test_runtime_merges_registered_provider_fragments_and_applies_budget() -> None:
    def factory(provider_id: str, config: dict) -> FakeProvider:
        return FakeProvider(
            provider_id,
            fragments=[
                MemoryBundleFragment(
                    id=f"{provider_id}-low",
                    text="low score",
                    source_provider=provider_id,
                    source_kind="note",
                    trust_tier="user",
                    score=0.1,
                    token_estimate=5,
                ),
                MemoryBundleFragment(
                    id=f"{provider_id}-high",
                    text="high score",
                    source_provider=provider_id,
                    source_kind="note",
                    trust_tier="user",
                    score=0.9,
                    token_estimate=5,
                ),
            ],
        )

    MemoryProviderRegistry.register_adapter("fake", factory)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry(
            {
                "alpha": {"adapter": "fake"},
                "beta": {"adapter": "fake"},
            }
        ),
        default_provider="alpha",
        token_budget_default=10,
    )
    scope = MemoryScope(owner_agent_id="agent-a")

    bundle = runtime.provide_for_llm_request(
        scope,
        MemoryProvideRequest(query="x", token_budget=10),
        policy={"read_providers": ["alpha", "beta"], "primary_provider": "alpha"},
    )

    assert [fragment.id for fragment in bundle.fragments] == [
        "alpha-high",
        "beta-high",
    ]
    assert bundle.token_estimate == 10


def test_runtime_capture_checks_provider_capability() -> None:
    provider = FakeProvider(
        "main",
        capabilities=MemoryProviderCapabilities(provide=True, capture=False),
    )
    MemoryProviderRegistry.register_adapter("fake", lambda provider_id, config: provider)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "fake"}}),
        default_provider="main",
    )

    receipt = runtime.capture_event(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryCaptureEvent(kind="session_save", payload={}),
    )

    assert receipt is not None
    assert receipt.accepted is False
    assert receipt.status == "capability_missing"
    assert provider.captured == []


def test_runtime_fail_open_reports_provider_provide_error() -> None:
    provider = FailingProvider("main", fail_on="provide")
    MemoryProviderRegistry.register_adapter("fake", lambda provider_id, config: provider)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "fake"}}),
        default_provider="main",
        fail_mode="open",
    )

    bundle = runtime.provide_for_llm_request(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryProvideRequest(query="x"),
    )

    assert bundle.fragments == []
    assert bundle.diagnostics[0].code == "provider_unavailable"
    assert bundle.diagnostics[0].metadata["error_type"] == "RuntimeError"
    assert bundle.warnings == ["provide failed"]


def test_runtime_fail_open_reports_provider_factory_error() -> None:
    def factory(provider_id: str, config: dict) -> FakeProvider:
        raise RuntimeError("factory failed")

    MemoryProviderRegistry.register_adapter("broken", factory)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "broken"}}),
        default_provider="main",
        fail_mode="open",
    )

    bundle = runtime.provide_for_llm_request(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryProvideRequest(query="x"),
    )

    assert bundle.fragments == []
    assert bundle.diagnostics[0].code == "provider_unavailable"
    assert bundle.warnings == ["factory failed"]


def test_runtime_fail_closed_reraises_provider_provide_error() -> None:
    provider = FailingProvider("main", fail_on="provide")
    MemoryProviderRegistry.register_adapter("fake", lambda provider_id, config: provider)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "fake"}}),
        default_provider="main",
        fail_mode="closed",
    )

    with pytest.raises(RuntimeError, match="provide failed"):
        runtime.provide_for_llm_request(
            MemoryScope(owner_agent_id="agent-a"),
            MemoryProvideRequest(query="x"),
        )


def test_runtime_capture_fail_open_reports_provider_error() -> None:
    provider = FailingProvider("main", fail_on="capture")
    MemoryProviderRegistry.register_adapter("fake", lambda provider_id, config: provider)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "fake"}}),
        default_provider="main",
        fail_mode="open",
    )

    receipt = runtime.capture_event(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryCaptureEvent(kind="session_save", payload={}),
    )

    assert receipt is not None
    assert receipt.accepted is False
    assert receipt.status == "provider_unavailable"
    assert receipt.diagnostics[0].code == "provider_unavailable"
    assert receipt.diagnostics[0].message == "capture failed"


def test_runtime_capture_fail_open_reports_provider_factory_error() -> None:
    def factory(provider_id: str, config: dict) -> FakeProvider:
        raise RuntimeError("factory failed")

    MemoryProviderRegistry.register_adapter("broken", factory)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "broken"}}),
        default_provider="main",
        fail_mode="open",
    )

    receipt = runtime.capture_event(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryCaptureEvent(kind="session_save", payload={}),
    )

    assert receipt is not None
    assert receipt.accepted is False
    assert receipt.status == "provider_unavailable"
    assert receipt.diagnostics[0].message == "factory failed"


def test_runtime_capture_fail_closed_reraises_provider_error() -> None:
    provider = FailingProvider("main", fail_on="capture")
    MemoryProviderRegistry.register_adapter("fake", lambda provider_id, config: provider)
    runtime = MemoryRuntime(
        enabled=True,
        provider_registry=MemoryProviderRegistry({"main": {"adapter": "fake"}}),
        default_provider="main",
        fail_mode="closed",
    )

    with pytest.raises(RuntimeError, match="capture failed"):
        runtime.capture_event(
            MemoryScope(owner_agent_id="agent-a"),
            MemoryCaptureEvent(kind="session_save", payload={}),
        )


def _tool_runtime(
    *,
    enabled: bool = True,
    tool_surface_policy: MemoryToolSurfacePolicy | None = None,
    tool_provider: FakeProvider | None = None,
    default_provider: FakeProvider | None = None,
    fail_mode: str = "open",
) -> tuple[MemoryRuntime, FakeProvider, FakeProvider]:
    default_provider = default_provider or FakeProvider("default")
    tool_provider = tool_provider or FakeProvider("tools")
    providers = {
        "default": default_provider,
        "tools": tool_provider,
    }

    def factory(provider_id: str, config: dict) -> FakeProvider:
        return providers[provider_id]

    MemoryProviderRegistry.register_adapter("fake", factory)
    runtime = MemoryRuntime(
        enabled=enabled,
        provider_registry=MemoryProviderRegistry(
            {
                "default": {"adapter": "fake"},
                "tools": {"adapter": "fake"},
            }
        ),
        default_provider="default",
        fail_mode=fail_mode,
        tool_surface_policy=tool_surface_policy
        or MemoryToolSurfacePolicy(
            enabled=True,
            provider="tools",
            allowed_agents=["agent-a"],
            remember=True,
            forget=True,
        ),
    )
    return runtime, default_provider, tool_provider


@pytest.mark.parametrize(
    ("runtime_enabled", "policy", "tool_policy", "expected_status"),
    [
        (False, {"expose_tools": True}, None, "tool_surface_disabled"),
        (True, {"enabled": False, "expose_tools": True}, None, "tool_surface_disabled"),
        (True, {"expose_tools": False}, None, "tool_surface_disabled"),
        (
            True,
            {"expose_tools": True},
            MemoryToolSurfacePolicy(
                enabled=False,
                provider="tools",
                remember=True,
                forget=True,
            ),
            "tool_surface_disabled",
        ),
        (
            True,
            {"expose_tools": True},
            MemoryToolSurfacePolicy(
                enabled=True,
                provider="tools",
                allowed_agents=["agent-b"],
                remember=True,
                forget=True,
            ),
            "tool_not_allowed",
        ),
        (
            True,
            {"expose_tools": True},
            MemoryToolSurfacePolicy(
                enabled=True,
                provider="tools",
                allowed_agents=["agent-a"],
                remember=False,
                forget=True,
            ),
            "operation_disabled",
        ),
    ],
)
def test_runtime_remember_requires_tool_surface_authorization(
    runtime_enabled: bool,
    policy: dict,
    tool_policy: MemoryToolSurfacePolicy | None,
    expected_status: str,
) -> None:
    runtime, default_provider, tool_provider = _tool_runtime(
        enabled=runtime_enabled,
        tool_surface_policy=tool_policy,
    )

    result = runtime.remember(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryRememberItem(text="remember this"),
        policy=policy,
    )

    assert result.accepted is False
    assert result.status == expected_status
    assert default_provider.remembered == []
    assert tool_provider.remembered == []


def test_runtime_forget_requires_operation_authorization() -> None:
    runtime, default_provider, tool_provider = _tool_runtime(
        tool_surface_policy=MemoryToolSurfacePolicy(
            enabled=True,
            provider="tools",
            allowed_agents=["agent-a"],
            remember=True,
            forget=False,
        )
    )

    result = runtime.forget(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryForgetSelector(query="drop this"),
        policy={"expose_tools": True},
    )

    assert result.accepted is False
    assert result.status == "operation_disabled"
    assert default_provider.forgotten == []
    assert tool_provider.forgotten == []


def test_runtime_memory_tools_use_configured_tool_provider() -> None:
    runtime, default_provider, tool_provider = _tool_runtime()

    remember_result = runtime.remember(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryRememberItem(text="remember this"),
        policy={"expose_tools": True},
    )
    forget_result = runtime.forget(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryForgetSelector(query="drop this"),
        policy={"expose_tools": True},
    )

    assert remember_result.accepted is True
    assert forget_result.accepted is True
    assert default_provider.remembered == []
    assert default_provider.forgotten == []
    assert [item.text for item in tool_provider.remembered] == ["remember this"]
    assert [selector.query for selector in tool_provider.forgotten] == ["drop this"]


def test_runtime_memory_tools_require_configured_provider() -> None:
    runtime, _default_provider, _tool_provider = _tool_runtime(
        tool_surface_policy=MemoryToolSurfacePolicy(
            enabled=True,
            remember=True,
            forget=True,
        )
    )

    with pytest.raises(MemoryProviderConfigurationError, match="memory.tools.provider"):
        runtime.remember(
            MemoryScope(owner_agent_id="agent-a"),
            MemoryRememberItem(text="remember this"),
            policy={"expose_tools": True},
        )


def test_runtime_remember_reports_capability_missing() -> None:
    tool_provider = FakeProvider(
        "tools",
        capabilities=MemoryProviderCapabilities(
            provide=True,
            capture=True,
            remember=False,
            forget=True,
        ),
    )
    runtime, _default_provider, _tool_provider = _tool_runtime(tool_provider=tool_provider)

    result = runtime.remember(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryRememberItem(text="remember this"),
        policy={"expose_tools": True},
    )

    assert result.accepted is False
    assert result.status == "capability_missing"
    assert tool_provider.remembered == []


def test_runtime_memory_tool_fail_open_reports_provider_error() -> None:
    runtime, _default_provider, tool_provider = _tool_runtime(
        tool_provider=FailingProvider("tools", fail_on="remember"),
        fail_mode="open",
    )

    result = runtime.remember(
        MemoryScope(owner_agent_id="agent-a"),
        MemoryRememberItem(text="remember this"),
        policy={"expose_tools": True},
    )

    assert result.accepted is False
    assert result.status == "provider_unavailable"
    assert result.provider_id == "tools"
    assert result.diagnostics[0].message == "remember failed"
    assert tool_provider.remembered == []


def test_runtime_memory_tool_fail_closed_reraises_provider_error() -> None:
    runtime, _default_provider, _tool_provider = _tool_runtime(
        tool_provider=FailingProvider("tools", fail_on="forget"),
        fail_mode="closed",
    )

    with pytest.raises(RuntimeError, match="forget failed"):
        runtime.forget(
            MemoryScope(owner_agent_id="agent-a"),
            MemoryForgetSelector(query="drop this"),
            policy={"expose_tools": True},
        )
