"""Resolve model/profile config into LLM runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reuleauxcoder.domain.config.models import (
    ModelProfileConfig,
    ProviderConfig,
    ProvidersConfig,
)
from reuleauxcoder.services.llm.client import LLM


@dataclass(frozen=True, slots=True)
class ResolvedModelRuntime:
    """Explicit LLM runtime derived from provider + model profile config."""

    model: str = ""
    provider_id: str | None = None
    provider_config: ProviderConfig | None = None
    profile_name: str | None = None
    temperature: float = 0.0
    max_tokens: int = 0
    max_context_tokens: int = 0
    preserve_reasoning_content: bool = True
    backfill_reasoning_content_for_tool_calls: bool = False
    reasoning_effort: str | None = None
    thinking_enabled: bool | None = None
    reasoning_replay_mode: str | None = None
    reasoning_replay_placeholder: str | None = None

    @property
    def api_key(self) -> str:
        return self.provider_config.api_key if self.provider_config is not None else ""

    @property
    def base_url(self) -> str | None:
        return self.provider_config.base_url if self.provider_config is not None else None

    @property
    def configured(self) -> bool:
        return bool(self.model and self.provider_config is not None and self.api_key)


def _profile_to_runtime(
    profile: ModelProfileConfig | Any,
    *,
    providers: ProvidersConfig | None = None,
    profile_name: str | None = None,
) -> ResolvedModelRuntime:
    provider_id = getattr(profile, "provider", None)
    provider_config = (
        providers.items.get(provider_id)
        if provider_id and providers is not None
        else None
    )
    return ResolvedModelRuntime(
        model=str(getattr(profile, "model", "") or ""),
        provider_id=str(provider_id) if provider_id else None,
        provider_config=provider_config,
        profile_name=profile_name or getattr(profile, "name", None),
        temperature=float(getattr(profile, "temperature", 0.0) or 0.0),
        max_tokens=int(getattr(profile, "max_tokens", 0) or 0),
        max_context_tokens=int(getattr(profile, "max_context_tokens", 0) or 0),
        preserve_reasoning_content=bool(
            getattr(profile, "preserve_reasoning_content", True)
        ),
        backfill_reasoning_content_for_tool_calls=bool(
            getattr(profile, "backfill_reasoning_content_for_tool_calls", False)
        ),
        reasoning_effort=getattr(profile, "reasoning_effort", None),
        thinking_enabled=getattr(profile, "thinking_enabled", None),
        reasoning_replay_mode=getattr(profile, "reasoning_replay_mode", None),
        reasoning_replay_placeholder=getattr(
            profile, "reasoning_replay_placeholder", None
        ),
    )


def resolve_provider_config(
    settings: Any, providers: ProvidersConfig | None = None
) -> ProviderConfig | None:
    if isinstance(settings, ResolvedModelRuntime):
        return settings.provider_config
    provider_name = getattr(settings, "provider", None) or getattr(
        settings, "provider_id", None
    )
    if not provider_name or providers is None:
        return None
    return providers.items.get(provider_name)


def resolve_model_runtime(
    settings: Any,
    *,
    profile_name: str | None = None,
    providers: ProvidersConfig | None = None,
) -> ResolvedModelRuntime:
    """Resolve runtime settings from Config, ModelProfileConfig, or runtime."""
    if isinstance(settings, ResolvedModelRuntime):
        return settings

    if providers is None:
        providers = getattr(settings, "providers", None)

    profiles = getattr(settings, "model_profiles", None)
    if isinstance(profiles, dict):
        selected = profile_name or getattr(settings, "active_main_model_profile", None)
        profile = profiles.get(selected) if selected else None
        if profile is None and not selected and profiles:
            selected, profile = next(iter(profiles.items()))
        if profile is not None:
            return _profile_to_runtime(
                profile, providers=providers, profile_name=str(selected)
            )
        return ResolvedModelRuntime()

    return _profile_to_runtime(settings, providers=providers, profile_name=profile_name)


def llm_trace_enabled(config: Any) -> bool:
    diagnostics = getattr(config, "diagnostics", None)
    llm_trace = getattr(diagnostics, "llm_trace", None)
    return bool(getattr(llm_trace, "enabled", False))


def llm_raw_chunks_enabled(config: Any) -> bool:
    diagnostics = getattr(config, "diagnostics", None)
    llm_trace = getattr(diagnostics, "llm_trace", None)
    return bool(getattr(llm_trace, "raw_chunks", False))


def llm_runtime_kwargs(
    settings: Any,
    *,
    debug_trace: bool = False,
    debug_raw_chunks: bool = False,
    providers: ProvidersConfig | None = None,
) -> dict[str, Any]:
    """Build LLM constructor/reconfigure kwargs from a resolved runtime."""
    runtime = resolve_model_runtime(settings, providers=providers)
    return {
        "model": runtime.model,
        "api_key": runtime.api_key,
        "base_url": runtime.base_url,
        "temperature": runtime.temperature,
        "max_tokens": runtime.max_tokens,
        "preserve_reasoning_content": runtime.preserve_reasoning_content,
        "backfill_reasoning_content_for_tool_calls": (
            runtime.backfill_reasoning_content_for_tool_calls
        ),
        "reasoning_effort": runtime.reasoning_effort,
        "thinking_enabled": runtime.thinking_enabled,
        "reasoning_replay_mode": runtime.reasoning_replay_mode,
        "reasoning_replay_placeholder": runtime.reasoning_replay_placeholder,
        "debug_trace": debug_trace,
        "debug_raw_chunks": debug_raw_chunks,
        "provider": runtime.provider_id,
        "provider_config": runtime.provider_config,
    }


def build_llm_from_settings(
    settings: Any,
    *,
    debug_trace: bool = False,
    debug_raw_chunks: bool = False,
    providers: ProvidersConfig | None = None,
) -> LLM:
    """Create an LLM from a resolved provider/profile runtime."""
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
    """Reconfigure an existing LLM from a resolved provider/profile runtime."""
    kwargs = llm_runtime_kwargs(
        settings,
        debug_trace=llm.debug_trace if debug_trace is None else debug_trace,
        debug_raw_chunks=(
            llm.debug_raw_chunks if debug_raw_chunks is None else debug_raw_chunks
        ),
        providers=providers,
    )
    llm.reconfigure(**kwargs)


def _param_value(params: dict[str, Any], key: str, fallback: Any) -> Any:
    value = params.get(key)
    return fallback if value is None else value


def model_binding_settings(
    *,
    provider: str,
    model: str,
    parameters: dict[str, Any] | None = None,
    fallback: Any | None = None,
    providers: ProvidersConfig | None = None,
) -> ResolvedModelRuntime:
    """Build runtime settings from an Agent model binding."""
    params = dict(parameters or {})
    fallback_runtime = (
        resolve_model_runtime(fallback, providers=providers)
        if fallback is not None
        else ResolvedModelRuntime()
    )
    if providers is None:
        providers = getattr(fallback, "providers", None)
    provider_config = providers.items.get(provider) if providers is not None else None
    return ResolvedModelRuntime(
        model=model,
        provider_id=provider,
        provider_config=provider_config,
        temperature=float(_param_value(params, "temperature", fallback_runtime.temperature) or 0.0),
        max_tokens=int(_param_value(params, "max_tokens", fallback_runtime.max_tokens) or 0),
        max_context_tokens=int(
            _param_value(
                params, "max_context_tokens", fallback_runtime.max_context_tokens
            )
            or 0
        ),
        preserve_reasoning_content=bool(
            _param_value(
                params,
                "preserve_reasoning_content",
                fallback_runtime.preserve_reasoning_content,
            )
        ),
        backfill_reasoning_content_for_tool_calls=bool(
            _param_value(
                params,
                "backfill_reasoning_content_for_tool_calls",
                fallback_runtime.backfill_reasoning_content_for_tool_calls,
            )
        ),
        reasoning_effort=_param_value(
            params, "reasoning_effort", fallback_runtime.reasoning_effort
        ),
        thinking_enabled=_param_value(
            params, "thinking_enabled", fallback_runtime.thinking_enabled
        ),
        reasoning_replay_mode=_param_value(
            params, "reasoning_replay_mode", fallback_runtime.reasoning_replay_mode
        ),
        reasoning_replay_placeholder=_param_value(
            params,
            "reasoning_replay_placeholder",
            fallback_runtime.reasoning_replay_placeholder,
        ),
    )
