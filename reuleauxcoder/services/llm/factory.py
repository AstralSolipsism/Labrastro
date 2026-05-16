"""Helpers for constructing/reconfiguring LLM clients from config/profile settings."""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ProviderConfig, ProvidersConfig
from reuleauxcoder.services.llm.client import LLM


_LLM_RUNTIME_FIELDS = (
    "model",
    "api_key",
    "base_url",
    "temperature",
    "max_tokens",
    "preserve_reasoning_content",
    "backfill_reasoning_content_for_tool_calls",
    "reasoning_effort",
    "thinking_enabled",
    "reasoning_replay_mode",
    "reasoning_replay_placeholder",
    "provider",
)


def resolve_provider_config(
    settings: Any, providers: ProvidersConfig | None = None
) -> ProviderConfig | None:
    provider_name = getattr(settings, "provider", None)
    if not provider_name or providers is None:
        return None
    return providers.items.get(provider_name)


def llm_runtime_kwargs(
    settings: Any,
    *,
    debug_trace: bool = False,
    debug_raw_chunks: bool = False,
    providers: ProvidersConfig | None = None,
) -> dict[str, Any]:
    """Extract LLM constructor/reconfigure kwargs from a config/profile-like object."""
    kwargs = {field: getattr(settings, field, None) for field in _LLM_RUNTIME_FIELDS}
    kwargs["debug_trace"] = debug_trace
    kwargs["debug_raw_chunks"] = debug_raw_chunks
    provider_config = resolve_provider_config(settings, providers)
    if provider_config is not None:
        kwargs["provider_config"] = provider_config
        kwargs["api_key"] = getattr(settings, "api_key", "") or provider_config.api_key
        kwargs["base_url"] = (
            getattr(settings, "base_url", None) or provider_config.base_url
        )
    return kwargs


def build_llm_from_settings(
    settings: Any,
    *,
    debug_trace: bool = False,
    debug_raw_chunks: bool = False,
    providers: ProvidersConfig | None = None,
) -> LLM:
    """Create an LLM from a config/profile-like object."""
    if providers is None:
        providers = getattr(settings, "providers", None)
    return LLM(
        **llm_runtime_kwargs(
            settings,
            debug_trace=debug_trace,
            debug_raw_chunks=debug_raw_chunks,
            providers=providers,
        )
    )


def reconfigure_llm_from_settings(
    llm: LLM,
    settings: Any,
    *,
    debug_trace: bool | None = None,
    debug_raw_chunks: bool | None = None,
    providers: ProvidersConfig | None = None,
) -> None:
    """Reconfigure an existing LLM from a config/profile-like object."""
    if providers is None:
        providers = getattr(settings, "providers", None)
    kwargs = llm_runtime_kwargs(
        settings,
        debug_trace=llm.debug_trace if debug_trace is None else debug_trace,
        debug_raw_chunks=(
            llm.debug_raw_chunks if debug_raw_chunks is None else debug_raw_chunks
        ),
        providers=providers,
    )
    llm.reconfigure(**kwargs)


def model_binding_settings(
    *,
    provider: str,
    model: str,
    parameters: dict[str, Any] | None = None,
    fallback: Any | None = None,
) -> Any:
    """Build a config/profile-like settings object from an Agent model binding."""
    params = dict(parameters or {})
    return SimpleNamespace(
        model=model,
        provider=provider,
        api_key=params.get("api_key") or getattr(fallback, "api_key", ""),
        base_url=params.get("base_url") or getattr(fallback, "base_url", None),
        temperature=params.get("temperature", getattr(fallback, "temperature", 0.0)),
        max_tokens=params.get("max_tokens", getattr(fallback, "max_tokens", 4096)),
        max_context_tokens=params.get(
            "max_context_tokens", getattr(fallback, "max_context_tokens", 128000)
        ),
        preserve_reasoning_content=params.get(
            "preserve_reasoning_content",
            getattr(fallback, "preserve_reasoning_content", True),
        ),
        backfill_reasoning_content_for_tool_calls=params.get(
            "backfill_reasoning_content_for_tool_calls",
            getattr(fallback, "backfill_reasoning_content_for_tool_calls", False),
        ),
        reasoning_effort=params.get(
            "reasoning_effort", getattr(fallback, "reasoning_effort", None)
        ),
        thinking_enabled=params.get(
            "thinking_enabled", getattr(fallback, "thinking_enabled", None)
        ),
        reasoning_replay_mode=params.get(
            "reasoning_replay_mode", getattr(fallback, "reasoning_replay_mode", None)
        ),
        reasoning_replay_placeholder=params.get(
            "reasoning_replay_placeholder",
            getattr(fallback, "reasoning_replay_placeholder", None),
        ),
    )
