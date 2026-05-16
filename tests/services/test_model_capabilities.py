import json
from typing import Any

from reuleauxcoder.services.providers.model_capabilities import (
    LITELLM_MODEL_PRICES_URL,
    OPENROUTER_MODELS_URL,
    ModelCapabilityCatalogService,
)


class FakeResponse:
    def __init__(self, payload: Any):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_litellm_source_parses_unified_capability(tmp_path) -> None:
    service = ModelCapabilityCatalogService(
        tmp_path,
        urlopen=lambda _url, timeout=20: FakeResponse(
            {
                "openai/gpt-4.1": {
                    "litellm_provider": "openai",
                    "max_input_tokens": 1_047_576,
                    "max_output_tokens": 32_768,
                    "supports_function_calling": True,
                    "supports_response_schema": True,
                    "supports_vision": True,
                    "input_cost_per_token": 0.000000002,
                    "output_cost_per_token": 0.000000008,
                }
            }
        ),
    )

    capability = service._fetch_litellm_capabilities()[0]

    assert capability.provider == "openai"
    assert capability.model_id == "gpt-4.1"
    assert capability.max_context_tokens == 1047576
    assert capability.max_output_tokens == 32768
    assert capability.supports_tools is True
    assert capability.supports_structured_outputs is True
    assert capability.supports_vision is True


def test_openrouter_source_parses_output_limit_and_supported_parameters(tmp_path) -> None:
    service = ModelCapabilityCatalogService(
        tmp_path,
        urlopen=lambda _url, timeout=20: FakeResponse(
            {
                "data": [
                    {
                        "id": "deepseek/deepseek-v4-pro",
                        "context_length": 1_000_000,
                        "top_provider": {"max_completion_tokens": 384_000},
                        "supported_parameters": [
                            "tools",
                            "response_format",
                            "reasoning",
                        ],
                    }
                ]
            }
        ),
    )

    capability = service._fetch_openrouter_capabilities()[0]

    assert capability.provider == "deepseek"
    assert capability.model_id == "deepseek-v4-pro"
    assert capability.max_context_tokens == 1_000_000
    assert capability.max_output_tokens == 384_000
    assert capability.supports_tools is True
    assert capability.supports_json_output is True
    assert capability.supports_reasoning is True


def test_refresh_merges_sources_with_deepseek_v4_override_priority(tmp_path) -> None:
    def fake_urlopen(url: str, timeout=20):
        if url == LITELLM_MODEL_PRICES_URL:
            return FakeResponse(
                {
                    "deepseek/deepseek-v4-pro": {
                        "litellm_provider": "deepseek",
                        "max_input_tokens": 128_000,
                        "max_output_tokens": 4_096,
                    }
                }
            )
        if url == OPENROUTER_MODELS_URL:
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": "deepseek/deepseek-v4-pro",
                            "context_length": 1_000_000,
                            "top_provider": {"max_completion_tokens": 384_000},
                            "supported_parameters": ["tools", "reasoning"],
                        }
                    ]
                }
            )
        raise AssertionError(url)

    service = ModelCapabilityCatalogService(tmp_path, urlopen=fake_urlopen)

    result = service.refresh()
    capability = service.lookup("deepseek", "deepseek-v4-pro")

    assert result["ok"] is True
    assert capability is not None
    assert capability.max_context_tokens == 1_000_000
    assert capability.max_output_tokens == 384_000
    assert capability.confidence == "high"
    assert service.cache_path.exists()


def test_refresh_failure_keeps_previous_cache(tmp_path, monkeypatch) -> None:
    service = ModelCapabilityCatalogService(
        tmp_path,
        urlopen=lambda _url, timeout=20: FakeResponse({}),
    )
    assert service.refresh()["ok"] is True
    previous_updated_at = service.load_catalog()["updated_at"]

    service.urlopen = lambda _url, timeout=20: (_ for _ in ()).throw(RuntimeError("offline"))
    monkeypatch.setattr(service, "_builtin_capabilities", lambda: (_ for _ in ()).throw(RuntimeError("builtin disabled")))

    result = service.refresh()

    assert result["ok"] is False
    assert service.load_catalog()["updated_at"] == previous_updated_at
