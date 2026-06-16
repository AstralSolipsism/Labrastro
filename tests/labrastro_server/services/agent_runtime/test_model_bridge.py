from __future__ import annotations

import logging

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.providers.models import ProviderResponse

from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from labrastro_server.services.agent_runtime.model_bridge import (
    AgentRunModelBridge,
    AgentRunModelBridgeError,
)


class _AdminManager:
    def _expanded_provider(self, provider_id: str):
        if provider_id == "deepseek":
            return ProviderConfig(id="deepseek", type="openai_chat", api_key="sk-test")
        return None


class _Provider:
    def __init__(self) -> None:
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        if request.on_token:
            request.on_token("hel")
            request.on_token("lo")
        if request.on_reasoning_token:
            request.on_reasoning_token("plan")
        return ProviderResponse(content="hello", reasoning_content="plan")


class _FailingProvider:
    def chat(self, _request):
        raise RuntimeError("Error code: 500 - {'message': 'invalid csrf token'}")


def _control_plane(*, locale: str = "") -> AgentRunControlPlane:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "packager": {
                    "executor": "reuleauxcoder",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "model_request_origin": "server",
                }
            },
            "agents": {
                "capability_packager": {
                    "visibility": "system",
                    "system_flow_only": ["capability_ingest"],
                    "runtime_profile": "packager",
                    "model": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "parameters": {"max_tokens": 384000, "temperature": 0.2},
                    },
                }
            },
        }
    )
    control.submit_agent_run(
        AgentRunRequest(
            agent_id="capability_packager",
            prompt="package",
            source="capability_ingest",
            metadata={"locale": locale} if locale else {},
        ),
        task_id="run-1",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["reuleauxcoder"],
        peer_id="peer-1",
        peer_features=["worker_kind:sandbox_worker"],
    )
    assert claim is not None
    return control


def _active_claim(control: AgentRunControlPlane):
    return next(iter(control._claims.values()))


def test_model_bridge_validates_claim_and_uses_run_model_binding(monkeypatch, caplog) -> None:
    control = _control_plane()
    claim = _active_claim(control)
    provider = _Provider()
    monkeypatch.setattr(
        "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
        lambda _self, _config: provider,
    )
    bridge = AgentRunModelBridge(
        runtime_control_plane=control,
        admin_manager=_AdminManager(),
    )
    tokens: list[str] = []
    reasoning: list[str] = []

    caplog.set_level(logging.INFO, logger="labrastro_server.services.agent_runtime.model_bridge")
    prepared = bridge.prepare(
        {
            "agent_run_id": "run-1",
            "request_id": claim.request_id,
            "activation_id": claim.activation_id,
            "worker_id": "worker-1",
            "messages": [{"role": "user", "content": "hi"}],
            "parameters": {"max_tokens": 1},
        },
        peer_id="peer-1",
    )
    response = bridge.execute(
        prepared,
        on_token=tokens.append,
        on_reasoning_token=reasoning.append,
    )

    assert response.content == "hello"
    assert tokens == ["hel", "lo"]
    assert reasoning == ["plan"]
    request = provider.requests[0]
    assert request.model == "deepseek-v4-pro"
    assert request.messages == [{"role": "user", "content": "hi"}]
    assert request.max_tokens == 384000
    assert request.temperature == 0.2
    log_text = caplog.text
    assert "agent_run_model_request server_origin_bridge" in log_text
    assert "agent_run_id=run-1" in log_text
    assert "request_id=" in log_text
    assert "worker_id=worker-1" in log_text
    assert "provider=deepseek" in log_text
    assert "model=deepseek-v4-pro" in log_text
    assert "sk-test" not in log_text


def test_model_bridge_provider_failure_keeps_protocol_diagnostics(monkeypatch) -> None:
    class _ZenmuxAnthropicAdminManager:
        def _expanded_provider(self, provider_id: str):
            if provider_id == "deepseek":
                return ProviderConfig(
                    id="deepseek",
                    type="anthropic_messages",
                    api_key="sk-secret-should-not-leak",
                    base_url="https://zenmux.ai/api/v1",
                )
            return None

    control = _control_plane()
    claim = _active_claim(control)
    monkeypatch.setattr(
        "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
        lambda _self, _config: _FailingProvider(),
    )
    bridge = AgentRunModelBridge(
        runtime_control_plane=control,
        admin_manager=_ZenmuxAnthropicAdminManager(),
    )
    prepared = bridge.prepare(
        {
            "agent_run_id": "run-1",
            "request_id": claim.request_id,
            "activation_id": claim.activation_id,
            "worker_id": "worker-1",
            "messages": [{"role": "user", "content": "hi"}],
        },
        peer_id="peer-1",
    )

    try:
        bridge.execute(prepared)
    except AgentRunModelBridgeError as exc:
        assert exc.code == "provider_request_failed"
        assert exc.details is not None
        assert exc.details["suspected_reason"] == "provider_protocol_mismatch_suspected"
        assert "openai_chat" in exc.details["recommended_action"]
        assert "invalid csrf token" in exc.details["upstream_message"]
        assert "sk-secret" not in str(exc.details)
    else:  # pragma: no cover - defensive assertion branch
        raise AssertionError("expected provider diagnostics error")


def test_model_bridge_injects_locale_instruction_from_agent_run_metadata(monkeypatch) -> None:
    control = _control_plane(locale="zh-CN")
    claim = _active_claim(control)
    provider = _Provider()
    monkeypatch.setattr(
        "labrastro_server.services.agent_runtime.model_bridge.ProviderManager.create",
        lambda _self, _config: provider,
    )
    bridge = AgentRunModelBridge(
        runtime_control_plane=control,
        admin_manager=_AdminManager(),
    )

    prepared = bridge.prepare(
        {
            "agent_run_id": "run-1",
            "request_id": claim.request_id,
            "activation_id": claim.activation_id,
            "worker_id": "worker-1",
            "messages": [{"role": "user", "content": "hi"}],
        },
        peer_id="peer-1",
    )
    bridge.execute(prepared)

    request = provider.requests[0]
    assert request.messages[0]["role"] == "system"
    assert "所有用户可见的生成内容都必须使用简体中文" in request.messages[0]["content"]
    assert "生成草案中的自然语言字段" in request.messages[0]["content"]
    assert request.messages[1:] == [{"role": "user", "content": "hi"}]
    assert request.metadata["locale"] == "zh-CN"


def test_model_bridge_rejects_claim_mismatch() -> None:
    control = _control_plane()
    claim = _active_claim(control)
    bridge = AgentRunModelBridge(
        runtime_control_plane=control,
        admin_manager=_AdminManager(),
    )

    try:
        bridge.prepare(
            {
                "agent_run_id": "run-1",
                "request_id": "claim-missing",
                "activation_id": claim.activation_id,
                "worker_id": "worker-1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            peer_id="peer-1",
        )
    except AgentRunModelBridgeError as exc:
        assert exc.code == "claim_not_found"
    else:
        raise AssertionError("expected claim mismatch")
