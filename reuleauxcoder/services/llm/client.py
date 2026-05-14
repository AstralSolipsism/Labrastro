"""LLM facade backed by provider adapters."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from reuleauxcoder.domain.config.models import ProviderConfig, infer_provider_compat
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterLLMResponseContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import LLMResponse
from reuleauxcoder.domain.providers.models import ProviderDiagnostic, ProviderRequest
from reuleauxcoder.infrastructure.fs.paths import get_diagnostics_dir
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.services.llm.diagnostics import (
    persist_llm_error_diagnostic,
    snapshot_messages,
)
from reuleauxcoder.services.llm.sanitizer import (
    DEFAULT_REASONING_REPLAY_PLACEHOLDER,
    sanitize_messages_for_llm,
)
from reuleauxcoder.services.providers.adapters.openai_chat import OpenAIChatProvider
from reuleauxcoder.services.providers.manager import ProviderManager


MAX_DEBUG_CONTENT_CHARS = 400
MAX_DEBUG_STREAM_EVENTS = 200

_PROVIDER_PAYLOAD_KEYS_BY_TYPE: dict[str, set[str]] = {
    "openai_chat": {"messages", "tools"},
    "anthropic_messages": {"system", "messages", "tools"},
    "openai_responses": {"input", "tools"},
}


def _provider_diagnostic_key(item: Any) -> tuple[str, str, str] | None:
    if isinstance(item, ProviderDiagnostic):
        return (item.code, item.message, item.level)
    if isinstance(item, dict):
        code = item.get("code")
        message = item.get("message")
        level = item.get("level", "warning")
        if code is None or message is None:
            return None
        return (str(code), str(message), str(level))
    return None


def _dedupe_provider_diagnostics(metadata: dict[str, Any]) -> None:
    diagnostics = metadata.get("provider_diagnostics")
    if not isinstance(diagnostics, list):
        return

    seen: set[tuple[str, str, str]] = set()
    deduped: list[Any] = []
    for item in diagnostics:
        key = _provider_diagnostic_key(item)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(item)
    metadata["provider_diagnostics"] = deduped


def _merge_hook_request_overrides(
    provider_type: str,
    rebuilt_params: dict[str, Any],
    hook_params: dict[str, Any],
    original_params: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(rebuilt_params)
    payload_keys = _PROVIDER_PAYLOAD_KEYS_BY_TYPE.get(provider_type, set())

    for key, value in hook_params.items():
        if key in payload_keys:
            continue
        if key not in original_params or value != original_params.get(key):
            merged[key] = value
    return merged


def _mask_api_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def _trim_text(value: Any, limit: int = MAX_DEBUG_CONTENT_CHARS) -> str:
    text = str(value)
    return text[:limit] + ("..." if len(text) > limit else "")


def _persist_debug_trace(
    payload: dict[str, Any], *, session_id: str | None, trace_id: str | None
) -> Path:
    diagnostics_dir = get_diagnostics_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    session_slug = session_id or "no_session"
    trace_slug = trace_id or "no_trace"
    path = diagnostics_dir / f"llm_trace_{timestamp}_{session_slug}_{trace_slug}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _legacy_provider_config(
    *,
    provider_id: str | None,
    api_key: str,
    base_url: str | None,
    timeout_sec: int = 120,
    max_retries: int = 3,
) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id or "legacy-openai-chat",
        type="openai_chat",
        compat=infer_provider_compat(base_url),
        api_key=api_key,
        base_url=base_url,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
    )


class LLM:
    """LLM facade that keeps the legacy public API stable."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        preserve_reasoning_content: bool = True,
        backfill_reasoning_content_for_tool_calls: bool = False,
        reasoning_effort: str | None = None,
        thinking_enabled: bool | None = None,
        reasoning_replay_mode: str | None = None,
        reasoning_replay_placeholder: str = DEFAULT_REASONING_REPLAY_PLACEHOLDER,
        debug_trace: bool = False,
        ui_bus: UIEventBus | None = None,
        provider: str | None = None,
        provider_config: ProviderConfig | None = None,
    ):
        self._provider_manager = ProviderManager()
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.preserve_reasoning_content = preserve_reasoning_content
        self.backfill_reasoning_content_for_tool_calls = (
            backfill_reasoning_content_for_tool_calls
        )
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        self.reasoning_replay_mode = reasoning_replay_mode
        self.reasoning_replay_placeholder = reasoning_replay_placeholder
        self.debug_trace = debug_trace
        self.ui_bus = ui_bus
        self.provider_config = provider_config or _legacy_provider_config(
            provider_id=provider,
            api_key=api_key,
            base_url=base_url,
        )
        self.provider_id = self.provider_config.id
        self.provider_type = self.provider_config.type
        self.api_key = self.provider_config.api_key or api_key
        self.base_url = self.provider_config.base_url if provider_config else base_url
        self.client: Any = None
        self._provider = None
        self._provider_unavailable_reason = ""
        self._rebuild_provider()

    def _rebuild_provider(self) -> None:
        self.provider_id = self.provider_config.id
        self.provider_type = self.provider_config.type
        self.api_key = self.provider_config.api_key
        self.base_url = self.provider_config.base_url
        self._provider_unavailable_reason = ""
        if not self.api_key:
            self._provider = None
            self.client = None
            self._provider_unavailable_reason = (
                "No model provider API key is configured. Configure a provider "
                "and model profile before starting chat."
            )
            return
        self._provider = self._provider_manager.create(self.provider_config)
        self.client = getattr(self._provider, "client", None)

    def reconfigure(
        self,
        *,
        model: str,
        api_key: str,
        base_url: Optional[str],
        temperature: float,
        max_tokens: int,
        preserve_reasoning_content: bool | None = None,
        backfill_reasoning_content_for_tool_calls: bool | None = None,
        reasoning_effort: str | None = None,
        thinking_enabled: bool | None = None,
        reasoning_replay_mode: str | None = None,
        reasoning_replay_placeholder: str | None = None,
        debug_trace: bool | None = None,
        provider: str | None = None,
        provider_config: ProviderConfig | None = None,
    ) -> None:
        """Hot-swap runtime model/client settings."""
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        if preserve_reasoning_content is not None:
            self.preserve_reasoning_content = preserve_reasoning_content
        if backfill_reasoning_content_for_tool_calls is not None:
            self.backfill_reasoning_content_for_tool_calls = (
                backfill_reasoning_content_for_tool_calls
            )
        if reasoning_effort is not None:
            self.reasoning_effort = reasoning_effort
        if thinking_enabled is not None:
            self.thinking_enabled = thinking_enabled
        if reasoning_replay_mode is not None:
            self.reasoning_replay_mode = reasoning_replay_mode
        if reasoning_replay_placeholder is not None:
            self.reasoning_replay_placeholder = reasoning_replay_placeholder
        if debug_trace is not None:
            self.debug_trace = debug_trace
        self.provider_config = provider_config or _legacy_provider_config(
            provider_id=provider,
            api_key=api_key,
            base_url=base_url,
        )
        self._rebuild_provider()

    def _emit_debug(self, message: str, **data: Any) -> None:
        if self.ui_bus is not None:
            self.ui_bus.debug(message, kind=UIEventKind.AGENT, **data)

    def _prepare_provider(self):
        if self._provider is None:
            self._rebuild_provider()
        if self._provider is None:
            raise RuntimeError(self._provider_unavailable_reason)
        if isinstance(self._provider, OpenAIChatProvider):
            self._provider.call_with_retry = self._call_with_retry
        return self._provider

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        hook_registry: HookRegistry | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send messages, stream back response, handle tool calls."""
        raw_messages = [dict(msg) for msg in messages]
        sanitized_messages = sanitize_messages_for_llm(
            messages,
            preserve_reasoning_content=self.preserve_reasoning_content,
            backfill_reasoning_content_for_tool_calls=self.backfill_reasoning_content_for_tool_calls,
            reasoning_replay_mode=self.reasoning_replay_mode,
            reasoning_replay_placeholder=self.reasoning_replay_placeholder,
            thinking_enabled=bool(self.thinking_enabled),
        )
        if self.thinking_enabled and self.preserve_reasoning_content:
            placeholder = self.reasoning_replay_placeholder
            backfilled_indices: list[int] = []
            for idx, msg in enumerate(sanitized_messages):
                if msg.get("role") != "assistant":
                    continue
                if not msg.get("tool_calls"):
                    continue
                if msg.get("reasoning_content") == placeholder:
                    backfilled_indices.append(idx)
            if backfilled_indices:
                self._emit_debug(
                    "[reasoning] sanitizer backfilled placeholder "
                    "reasoning_content for tool-call assistant messages",
                    placeholder=placeholder,
                    message_indices=backfilled_indices,
                    count=len(backfilled_indices),
                )
        provider = self._prepare_provider()
        request = ProviderRequest(
            model=self.model,
            messages=sanitized_messages,
            tools=list(tools) if tools else [],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            thinking_enabled=self.thinking_enabled,
            on_token=on_token,
            metadata=dict(metadata or {}),
        )
        params: dict[str, Any] = {}
        final_messages = list(sanitized_messages)
        final_metadata = dict(request.metadata)

        try:
            params = provider.build_request_params(request)
            original_params = dict(params)
            before_context = BeforeLLMRequestContext(
                hook_point=HookPoint.BEFORE_LLM_REQUEST,
                request_params=dict(params),
                messages=list(sanitized_messages),
                tools=list(tools) if tools else [],
                model=self.model,
                session_id=session_id,
                trace_id=trace_id,
                metadata=dict(request.metadata),
            )

            if hook_registry is not None:
                guard_decisions = hook_registry.run_guards(
                    HookPoint.BEFORE_LLM_REQUEST, before_context
                )
                denied = next((d for d in guard_decisions if not d.allowed), None)
                if denied is not None:
                    raise RuntimeError(
                        denied.reason or "LLM request blocked by guard hook"
                    )
                before_context = hook_registry.run_transforms(
                    HookPoint.BEFORE_LLM_REQUEST, before_context
                )
                hook_registry.run_observers(
                    HookPoint.BEFORE_LLM_REQUEST, before_context
                )

            request.metadata = dict(before_context.metadata)
            request.messages = list(before_context.messages)
            request.tools = list(before_context.tools)
            _dedupe_provider_diagnostics(request.metadata)
            rebuilt_params = provider.build_request_params(request)
            _dedupe_provider_diagnostics(request.metadata)
            request.request_params = _merge_hook_request_overrides(
                self.provider_type,
                rebuilt_params,
                before_context.request_params,
                original_params,
            )
            params = dict(request.request_params)
            before_context.metadata = dict(request.metadata)
            final_messages = list(request.messages)
            final_metadata = dict(before_context.metadata)
            provider_response = provider.chat(request)
            response = provider_response.to_llm_response()
            params = dict(response.provider_extra.get("request_params") or params)

            if (
                self.thinking_enabled
                and self.preserve_reasoning_content
                and not response.reasoning_content
            ):
                self._emit_debug(
                    "[reasoning] thinking enabled but no reasoning_content "
                    "received in stream",
                    model=self.model,
                    has_tool_calls=bool(response.tool_calls),
                    content_chars=len(response.content or ""),
                )

            if self.debug_trace:
                debug_events = list(
                    (response.provider_extra or {}).get("debug_stream_events") or []
                )[:MAX_DEBUG_STREAM_EVENTS]
                reasoning_received = bool(response.reasoning_content)
                reasoning_stream_chunks = sum(
                    1
                    for ev in debug_events
                    if ev.get("type") in {"reasoning", "reasoning_detail"}
                )
                trace_payload = {
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "provider": {
                        "id": self.provider_id,
                        "type": self.provider_type,
                    },
                    "model": self.model,
                    "base_url": self.base_url,
                    "api_key_hint": _mask_api_key(self.api_key),
                    "request": {
                        "temperature": params.get("temperature"),
                        "max_tokens": params.get("max_tokens")
                        or params.get("max_output_tokens"),
                        "stream": params.get("stream"),
                        "stream_options": params.get("stream_options"),
                        "stream_options_enabled": bool(
                            response.provider_extra.get("stream_options_enabled")
                        ),
                        "tool_count": len(params.get("tools") or []),
                        "reasoning_effort": params.get("reasoning_effort")
                        or (params.get("reasoning") or {}).get("effort"),
                        "reasoning_replay_mode": self.reasoning_replay_mode or "none",
                        "thinking_enabled": self.thinking_enabled,
                        "thinking_type": (
                            ((params.get("extra_body") or {}).get("thinking") or {}).get(
                                "type"
                            )
                            or (params.get("thinking") or {}).get("type")
                        ),
                        "preserve_reasoning_content": self.preserve_reasoning_content,
                    },
                    "messages": {
                        "raw_count": len(raw_messages),
                        "sanitized_count": len(final_messages),
                        "raw_tail": snapshot_messages(raw_messages),
                        "sanitized_tail": snapshot_messages(final_messages),
                    },
                    "stream": {
                        "event_count": len(debug_events),
                        "reasoning_chunks": reasoning_stream_chunks,
                        "events": debug_events,
                    },
                    "response": {
                        "content": _trim_text(response.content or "", 1000),
                        "reasoning_content": _trim_text(
                            response.reasoning_content or "", 1000
                        ),
                        "reasoning_received": reasoning_received,
                        "reasoning_chars": len(response.reasoning_content or ""),
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "name": tc.name,
                                "arguments": tc.arguments,
                                "argument_error": getattr(tc, "argument_error", None),
                                "argument_diagnostics": list(
                                    getattr(tc, "argument_diagnostics", None) or []
                                ),
                            }
                            for tc in response.tool_calls
                        ],
                        "tool_argument_diagnostics": list(
                            response.provider_extra.get("tool_argument_diagnostics")
                            or []
                        ),
                        "usage": {
                            "prompt_tokens": response.prompt_tokens,
                            "completion_tokens": response.completion_tokens,
                        },
                    },
                    "metadata": dict(before_context.metadata),
                }
                trace_path = _persist_debug_trace(
                    trace_payload, session_id=session_id, trace_id=trace_id
                )
                self._emit_debug(
                    f"LLM trace saved: {trace_path}",
                    trace_path=str(trace_path),
                    session_id=session_id,
                    trace_id=trace_id,
                )

            after_context = AfterLLMResponseContext(
                hook_point=HookPoint.AFTER_LLM_RESPONSE,
                request_params=dict(params),
                response=response,
                model=self.model,
                session_id=session_id,
                trace_id=trace_id,
                metadata=dict(before_context.metadata),
            )

            if hook_registry is not None:
                after_context = hook_registry.run_transforms(
                    HookPoint.AFTER_LLM_RESPONSE, after_context
                )
                hook_registry.run_observers(HookPoint.AFTER_LLM_RESPONSE, after_context)

            return after_context.response or response
        except Exception as e:
            diagnostic_path = persist_llm_error_diagnostic(
                model=self.model,
                base_url=self.base_url,
                session_id=session_id,
                request_params=params,
                raw_messages=raw_messages,
                sanitized_messages=final_messages,
                error=e,
                metadata=final_metadata,
            )
            setattr(e, "llm_diagnostic_path", str(diagnostic_path))
            raise

    def _call_with_retry(self, params: dict, max_retries: int | None = None):
        """Retry OpenAI-compatible Chat Completions transient errors."""
        retries = self.provider_config.max_retries if max_retries is None else max_retries
        if self.client is None:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        for attempt in range(retries + 1):
            try:
                return self.client.chat.completions.create(**params)
            except (RateLimitError, APITimeoutError, APIConnectionError):
                if attempt >= retries:
                    raise
                time.sleep(2**attempt)
