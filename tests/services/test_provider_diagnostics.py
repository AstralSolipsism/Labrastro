from __future__ import annotations

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.services.providers.diagnostics import provider_error_envelope


def test_provider_error_envelope_redacts_sensitive_url_values() -> None:
    provider = ProviderConfig(
        id="Zenmux",
        type="anthropic_messages",
        api_key="sk-secret-should-not-leak",
        base_url=(
            "https://user:secret-token@zenmux.ai/api/v1"
            "?api_key=sk-secret-should-not-leak&safe=1#token=secret-token"
        ),
    )
    exc = RuntimeError(
        "invalid csrf token from https://proxy.test/v1?token=secret-token "
        "with api_key=sk-secret-should-not-leak"
    )

    payload = provider_error_envelope(
        provider,
        "anthropic/claude-opus-4.7",
        exc,
        code="provider_test_failed",
    )

    rendered = str(payload)
    assert "openai_chat" in payload["recommended_action"]
    assert "zenmux.ai/api/v1" in payload["base_url"]
    assert "safe=1" in payload["base_url"]
    assert "secret-token" not in rendered
    assert "sk-secret-should-not-leak" not in rendered
    assert "user:" not in payload["base_url"]
    assert "api_key=sk-secret" not in payload["base_url"]


def test_provider_error_envelope_hints_anthropic_messages_for_anthropic_endpoint() -> None:
    provider = ProviderConfig(
        id="anthropic-wrong-type",
        type="openai_chat",
        api_key="sk-secret-should-not-leak",
        base_url="https://api.anthropic.com/v1/messages?key=secret-token",
    )
    exc = RuntimeError("missing anthropic-version header")

    payload = provider_error_envelope(provider, "claude-opus-4.7", exc)

    rendered = str(payload)
    assert payload["suspected_reason"] == "provider_protocol_mismatch_suspected"
    assert "anthropic_messages" in payload["recommended_action"]
    assert "secret-token" not in rendered
    assert "sk-secret-should-not-leak" not in rendered


def test_provider_error_envelope_tolerates_invalid_base_url_port() -> None:
    provider = ProviderConfig(
        id="bad-port",
        type="openai_chat",
        api_key="sk-secret-should-not-leak",
        base_url="https://api.example.test:bad/v1?token=secret-token",
    )

    payload = provider_error_envelope(provider, "model", RuntimeError("failed"))

    rendered = str(payload)
    assert payload["base_url"].startswith("https://api.example.test/v1")
    assert "secret-token" not in rendered
    assert "sk-secret-should-not-leak" not in rendered
