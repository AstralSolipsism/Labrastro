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


def _control_plane() -> AgentRunControlPlane:
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
            issue_id="pkg",
            agent_id="capability_packager",
            prompt="package",
            source="capability_ingest",
        ),
        task_id="run-1",
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["reuleauxcoder"],
        peer_id="peer-1",
        peer_features=["worker_kind:sandbox_worker"],
    )
    assert claim is not None
    return control


def test_model_bridge_validates_claim_and_uses_run_model_binding(monkeypatch, caplog) -> None:
    control = _control_plane()
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
            "request_id": next(iter(control._claims)),
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


def test_model_bridge_rejects_claim_mismatch() -> None:
    control = _control_plane()
    bridge = AgentRunModelBridge(
        runtime_control_plane=control,
        admin_manager=_AdminManager(),
    )

    try:
        bridge.prepare(
            {
                "agent_run_id": "run-1",
                "request_id": "claim-missing",
                "worker_id": "worker-1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            peer_id="peer-1",
        )
    except AgentRunModelBridgeError as exc:
        assert exc.code == "claim_not_found"
    else:
        raise AssertionError("expected claim mismatch")
