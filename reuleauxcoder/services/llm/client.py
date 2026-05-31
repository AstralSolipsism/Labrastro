"""LLM facade backed by provider adapters."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from reuleauxcoder.domain.config.models import (
    PROVIDER_CONFIG_FIELDS,
    ProviderConfig,
    infer_provider_compat,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterLLMResponseContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import LLMResponse
from reuleauxcoder.domain.providers.models import (
    ProviderDiagnostic,
    ProviderRequest,
    ProviderResponse,
)
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
from reuleauxcoder.services.providers.manager import ProviderManager
from reuleauxcoder.services.providers.stream_supervisor import (
    ProviderStreamInterruptedError,
    StreamRecoveryPolicy,
)


MAX_DEBUG_CONTENT_CHARS = 400
MAX_DEBUG_STREAM_EVENTS = 200
STREAM_RECOVERY_VISIBLE_LIMIT = 4000

_PROVIDER_PAYLOAD_KEYS_BY_TYPE: dict[str, set[str]] = {
    "openai_chat": {"messages", "tools"},
    "anthropic_messages": {"system", "messages", "tools"},
    "openai_responses": {"input", "tools"},
}
_SERVER_ORIGIN_PROVIDER_TYPES = {"labrastro_server"}
_SERVER_ORIGIN_REQUIRED_ENV_BY_TYPE = {
    "labrastro_server": (
        "LABRASTRO_REMOTE_BASE_URL",
        "LABRASTRO_PEER_TOKEN",
        "LABRASTRO_AGENT_RUN_ID",
        "LABRASTRO_AGENT_RUN_REQUEST_ID",
        "LABRASTRO_AGENT_RUN_WORKER_ID",
    )
}


def provider_requires_api_key(provider_type: str | None) -> bool:
    """Return whether a provider must carry its own API key in this process."""
    return str(provider_type or "").strip() not in _SERVER_ORIGIN_PROVIDER_TYPES


def provider_runtime_unavailable_reason(provider_type: str | None) -> str:
    normalized = str(provider_type or "").strip()
    required = _SERVER_ORIGIN_REQUIRED_ENV_BY_TYPE.get(normalized, ())
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if not missing:
        return ""
    return (
        f"{', '.join(missing)} "
        f"{'is' if len(missing) == 1 else 'are'} required for {normalized} provider."
    )


def llm_is_configured(llm: Any) -> bool:
    """Validate the active model binding without assuming all providers hold keys."""
    model = str(getattr(llm, "model", "") or "").strip()
    if not model:
        return False
    provider_type = str(getattr(llm, "provider_type", "") or "").strip()
    provider_config = getattr(llm, "provider_config", None)
    if not provider_type:
        provider_type = str(getattr(provider_config, "type", "") or "").strip()
    api_key = str(getattr(llm, "api_key", "") or "").strip()
    if provider_requires_api_key(provider_type) and not api_key:
        return False
    if provider_runtime_unavailable_reason(provider_type):
        return False
    unavailable = str(getattr(llm, "_provider_unavailable_reason", "") or "").strip()
    return not unavailable


def llm_unavailable_reason(llm: Any) -> str:
    """Return a user-facing reason for a failed model binding check."""
    reason = str(getattr(llm, "_provider_unavailable_reason", "") or "").strip()
    if reason:
        return reason
    model = str(getattr(llm, "model", "") or "").strip()
    if not model:
        return "No chat model is selected. Choose a provider and model before starting chat."
    provider_type = str(getattr(llm, "provider_type", "") or "").strip()
    provider_config = getattr(llm, "provider_config", None)
    if not provider_type:
        provider_type = str(getattr(provider_config, "type", "") or "").strip()
    api_key = str(getattr(llm, "api_key", "") or "").strip()
    if provider_requires_api_key(provider_type) and not api_key:
        return (
            "No model provider API key is configured. Configure a provider "
            "and model profile before starting chat."
        )
    runtime_reason = provider_runtime_unavailable_reason(provider_type)
    if runtime_reason:
        return runtime_reason
    return (
        "No model provider/profile is configured. "
        "Configure providers.items and models.profiles."
    )


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


def _direct_provider_config(
    *,
    provider_id: str | None,
    api_key: str,
    base_url: str | None,
    timeout_sec: int = 120,
    max_retries: int = 3,
) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id or ("direct-openai-chat" if api_key or base_url else "unconfigured"),
        type="openai_chat",
        compat=infer_provider_compat(base_url),
        api_key=api_key,
        base_url=base_url,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
    )


class LLM:
    """LLM facade for the active provider/model binding."""

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        base_url: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 0,
        preserve_reasoning_content: bool = True,
        backfill_reasoning_content_for_tool_calls: bool = False,
        reasoning_effort: str | None = None,
        thinking_enabled: bool | None = None,
        reasoning_replay_mode: str | None = None,
        reasoning_replay_placeholder: str = DEFAULT_REASONING_REPLAY_PLACEHOLDER,
        debug_trace: bool = False,
        debug_raw_chunks: bool = False,
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
        self.debug_raw_chunks = debug_raw_chunks
        self.ui_bus = ui_bus
        self.provider_config = provider_config or _direct_provider_config(
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
        if not self.model:
            self._provider = None
            self.client = None
            self._provider_unavailable_reason = (
                "No chat model is selected. Choose a provider and model before starting chat."
            )
            return
        if not self.api_key and provider_requires_api_key(self.provider_type):
            self._provider = None
            self.client = None
            self._provider_unavailable_reason = (
                "No model provider API key is configured. Configure a provider "
                "and model profile before starting chat."
            )
            return
        runtime_reason = provider_runtime_unavailable_reason(self.provider_type)
        if runtime_reason:
            self._provider = None
            self.client = None
            self._provider_unavailable_reason = runtime_reason
            return
        try:
            self._provider = self._provider_manager.create(self.provider_config)
        except Exception as exc:
            self._provider = None
            self.client = None
            self._provider_unavailable_reason = str(exc) or (
                "Model provider could not be initialized."
            )
            return
        self.client = getattr(self._provider, "client", None)

    @property
    def configured(self) -> bool:
        return llm_is_configured(self)

    @property
    def unavailable_reason(self) -> str:
        return llm_unavailable_reason(self)

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
        debug_raw_chunks: bool | None = None,
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
        if debug_raw_chunks is not None:
            self.debug_raw_chunks = debug_raw_chunks
        self.provider_config = provider_config or _direct_provider_config(
            provider_id=provider,
            api_key=api_key,
            base_url=base_url,
        )
        self._rebuild_provider()

    def _emit_debug(
        self, message: str, *, ui_bus: UIEventBus | None = None, **data: Any
    ) -> None:
        bus = ui_bus or self.ui_bus
        if bus is not None:
            bus.debug(message, kind=UIEventKind.AGENT, **data)

    def _prepare_provider(self):
        if self._provider is None:
            self._rebuild_provider()
        if self._provider is None:
            raise RuntimeError(self._provider_unavailable_reason)
        return self._provider

    def _persist_stream_interruption_diagnostic(
        self,
        *,
        interrupted: ProviderStreamInterruptedError,
        params: dict[str, Any],
        raw_messages: list[dict],
        final_messages: list[dict],
        final_metadata: dict[str, Any],
        session_id: str | None,
        started_at: float,
    ) -> ProviderResponse:
        partial = interrupted.partial_response
        diagnostic_path = persist_llm_error_diagnostic(
            model=self.model,
            base_url=self.base_url,
            session_id=session_id,
            request_params=params,
            raw_messages=raw_messages,
            sanitized_messages=final_messages,
            error=interrupted.original_error,
            metadata=final_metadata,
            provider_id=self.provider_id,
            provider_type=self.provider_type,
            timeout_sec=getattr(self.provider_config, "timeout_sec", None),
            max_retries=getattr(self.provider_config, "max_retries", None),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        interruption = dict(partial.interruption or interrupted.interruption)
        interruption["diagnostic_path"] = str(diagnostic_path)
        partial.interruption = interruption
        partial.provider_extra = {
            **dict(partial.provider_extra or {}),
            "llm_diagnostic_path": str(diagnostic_path),
        }
        return partial

    def _recover_interrupted_response(
        self,
        *,
        provider: Any,
        request: ProviderRequest,
        partial: ProviderResponse,
        policy: StreamRecoveryPolicy,
    ) -> ProviderResponse:
        interruption = dict(partial.interruption or {})
        action = str(interruption.get("retry_action") or "retry")
        partial_kind = str(interruption.get("partial_kind") or "empty")
        if not policy.enabled:
            partial.recovery = {
                "attempted": False,
                "action": action,
                "attempt": 0,
                "max_attempts": 0,
                "reason": "disabled",
            }
            return partial
        if action == "continue" and policy.max_continue_attempts < 1:
            partial.recovery = {
                "attempted": False,
                "action": action,
                "attempt": 0,
                "max_attempts": 0,
                "reason": "continue_disabled",
            }
            return partial
        if action == "retry":
            if partial_kind == "empty" and not policy.retry_empty_once:
                return self._mark_recovery_skipped(partial, action, "empty_retry_disabled")
            if partial_kind == "tool_call_delta" and not policy.retry_tool_delta_once:
                return self._mark_recovery_skipped(partial, action, "tool_delta_retry_disabled")

        attempts = self._recovery_attempts(policy, action)
        last_error: str | None = None
        for attempt_index, attempt in enumerate(attempts, start=1):
            recovery_request = self._build_recovery_request(
                request,
                partial=partial,
                action=action,
                model=str(attempt.get("model") or request.model),
            )
            try:
                attempt_provider = provider
                fallback_provider_id = attempt.get("provider_id")
                if attempt.get("provider_config") is not None:
                    attempt_provider = self._provider_manager.create(
                        attempt["provider_config"],
                        allow_disabled=True,
                    )
                recovered = attempt_provider.chat(recovery_request)
            except ProviderStreamInterruptedError as exc:
                last_error = str(exc.original_error)
                continue
            except Exception as exc:
                last_error = str(exc)
                continue
            recovery = {
                "attempted": True,
                "action": action,
                "attempt": attempt_index,
                "max_attempts": len(attempts),
                **(
                    {"fallback_provider": fallback_provider_id}
                    if fallback_provider_id
                    else {}
                ),
                **(
                    {"fallback_model": attempt.get("model")}
                    if attempt.get("model") and attempt.get("model") != request.model
                    else {}
                ),
            }
            return self._merge_recovered_response(
                partial,
                recovered,
                action=action,
                recovery=recovery,
            )

        partial.recovery = {
            "attempted": True,
            "action": action,
            "attempt": len(attempts),
            "max_attempts": len(attempts),
            "failed": True,
            **({"error": last_error} if last_error else {}),
        }
        return partial

    def _mark_recovery_skipped(
        self, partial: ProviderResponse, action: str, reason: str
    ) -> ProviderResponse:
        partial.recovery = {
            "attempted": False,
            "action": action,
            "attempt": 0,
            "max_attempts": 0,
            "reason": reason,
        }
        return partial

    def _recovery_attempts(
        self, policy: StreamRecoveryPolicy, action: str
    ) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = [{"model": self.model}]
        if action == "continue":
            attempts = attempts[: policy.max_continue_attempts]
        for raw in policy.fallback_models:
            provider_config = self._fallback_provider_config(raw)
            attempts.append(
                {
                    "provider_id": raw.get("provider_id") or raw.get("provider"),
                    "model": raw.get("model") or self.model,
                    "provider_config": provider_config,
                }
            )
        return attempts

    def _fallback_provider_config(self, raw: dict[str, Any]) -> ProviderConfig | None:
        provider_data = raw.get("provider_config")
        if not isinstance(provider_data, dict):
            provider_data = {
                key: value
                for key, value in raw.items()
                if key in PROVIDER_CONFIG_FIELDS
            }
        if not provider_data:
            return None
        provider_id = str(raw.get("provider_id") or raw.get("provider") or "stream-fallback")
        return ProviderConfig.from_dict(provider_id, provider_data)

    def _build_recovery_request(
        self,
        request: ProviderRequest,
        *,
        partial: ProviderResponse,
        action: str,
        model: str,
    ) -> ProviderRequest:
        messages = list(request.messages)
        if action == "continue":
            messages.append(partial.to_llm_response().message)
            messages.append(
                {
                    "role": "user",
                    "content": self._continuation_prompt(partial),
                }
            )
        recovery_metadata = dict(request.metadata)
        recovery_metadata["stream_recovery"] = {
            "action": action,
            "partial_kind": (partial.interruption or {}).get("partial_kind"),
        }
        return ProviderRequest(
            model=model,
            messages=messages,
            tools=list(request.tools),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            reasoning_effort=request.reasoning_effort,
            thinking_enabled=request.thinking_enabled,
            tool_choice=request.tool_choice,
            on_token=request.on_token,
            on_reasoning_token=request.on_reasoning_token,
            on_tool_call_delta=request.on_tool_call_delta,
            metadata=recovery_metadata,
        )

    def _continuation_prompt(self, partial: ProviderResponse) -> str:
        visible = (partial.content or "").strip()
        if len(visible) > STREAM_RECOVERY_VISIBLE_LIMIT:
            visible = visible[-STREAM_RECOVERY_VISIBLE_LIMIT:]
        return (
            "<stream_recovery>\n"
            "The previous provider stream was interrupted after partial assistant output.\n"
            "Continue from the exact point after the already delivered assistant text. "
            "Do not repeat completed text and do not mention the stream failure unless it affects the answer.\n"
            "Already delivered assistant text:\n"
            f"{visible}\n"
            "</stream_recovery>"
        )

    def _merge_recovered_response(
        self,
        partial: ProviderResponse,
        recovered: ProviderResponse,
        *,
        action: str,
        recovery: dict[str, Any],
    ) -> ProviderResponse:
        if action == "continue":
            content = f"{partial.content or ''}{recovered.content or ''}"
            reasoning = "".join(
                item
                for item in [
                    partial.reasoning_content or "",
                    recovered.reasoning_content or "",
                ]
                if item
            ) or None
            tokens = [*partial.tokens, *recovered.tokens]
        else:
            content = recovered.content
            reasoning = recovered.reasoning_content
            tokens = list(recovered.tokens)
        provider_extra = {
            **dict(recovered.provider_extra or {}),
            "stream_interruption": dict(partial.interruption or {}),
            "stream_recovery": dict(recovery),
        }
        return ProviderResponse(
            content=content,
            reasoning_content=reasoning,
            reasoning_signature=recovered.reasoning_signature or partial.reasoning_signature,
            reasoning_details=[
                *list(partial.reasoning_details),
                *list(recovered.reasoning_details),
            ],
            tool_calls=list(recovered.tool_calls),
            prompt_tokens=partial.prompt_tokens + recovered.prompt_tokens,
            completion_tokens=partial.completion_tokens + recovered.completion_tokens,
            cache_read_tokens=self._sum_nullable(
                partial.cache_read_tokens, recovered.cache_read_tokens
            ),
            cache_write_tokens=self._sum_nullable(
                partial.cache_write_tokens, recovered.cache_write_tokens
            ),
            cost_usd=self._sum_nullable_float(partial.cost_usd, recovered.cost_usd),
            usage_extra={
                "partial": dict(partial.usage_extra),
                "recovered": dict(recovered.usage_extra),
            },
            tokens=tokens,
            provider_response_id=recovered.provider_response_id or partial.provider_response_id,
            provider_extra=provider_extra,
            diagnostics=[*list(partial.diagnostics), *list(recovered.diagnostics)],
            stream_status="completed",
            interruption=dict(partial.interruption or {}),
            recovery=recovery,
        )

    @staticmethod
    def _sum_nullable(a: int | None, b: int | None) -> int | None:
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    @staticmethod
    def _sum_nullable_float(a: float | None, b: float | None) -> float | None:
        if a is None and b is None:
            return None
        return (a or 0.0) + (b or 0.0)

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_reasoning_token: Optional[Callable[[str], None]] = None,
        on_tool_call_delta: Optional[Callable[[dict[str, Any]], None]] = None,
        hook_registry: HookRegistry | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ui_bus: UIEventBus | None = None,
    ) -> LLMResponse:
        """Send messages, stream back response, handle tool calls."""
        active_ui_bus = ui_bus or self.ui_bus
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
                    ui_bus=active_ui_bus,
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
            on_reasoning_token=on_reasoning_token,
            on_tool_call_delta=on_tool_call_delta,
            metadata=dict(metadata or {}),
        )
        params: dict[str, Any] = {}
        final_messages = list(sanitized_messages)
        final_metadata = dict(request.metadata)
        request_started_at = time.monotonic()

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
                ui_bus=active_ui_bus,
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
            if self.debug_trace:
                request.metadata["llm_debug_trace"] = True
                if self.debug_raw_chunks:
                    request.metadata["llm_debug_raw_chunks"] = True
            try:
                provider_response = provider.chat(request)
            except ProviderStreamInterruptedError as interrupted:
                provider_response = self._persist_stream_interruption_diagnostic(
                    interrupted=interrupted,
                    params=params,
                    raw_messages=raw_messages,
                    final_messages=final_messages,
                    final_metadata=final_metadata,
                    session_id=session_id,
                    started_at=request_started_at,
                )
                provider_response = self._recover_interrupted_response(
                    provider=provider,
                    request=request,
                    partial=provider_response,
                    policy=StreamRecoveryPolicy.from_config(self.provider_config),
                )
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
                    ui_bus=active_ui_bus,
                    model=self.model,
                    has_tool_calls=bool(response.tool_calls),
                    content_chars=len(response.content or ""),
                )

            if self.debug_trace:
                debug_events = list(
                    (response.provider_extra or {}).get("debug_stream_events") or []
                )[:MAX_DEBUG_STREAM_EVENTS]
                debug_raw_chunks = list(
                    (response.provider_extra or {}).get("debug_raw_stream_chunks")
                    or []
                )
                debug_http_chunks = list(
                    (response.provider_extra or {}).get("debug_http_chunks") or []
                )
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
                        "status": response.stream_status,
                        "interruption": response.interruption,
                        "recovery": response.recovery,
                        "event_count": len(debug_events),
                        "reasoning_chunks": reasoning_stream_chunks,
                        "events": debug_events,
                        "raw_chunk_count": len(debug_raw_chunks),
                        "raw_chunks": debug_raw_chunks,
                        "http_chunk_count": len(debug_http_chunks),
                        "http_chunks": debug_http_chunks,
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
                    ui_bus=active_ui_bus,
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
            error_params = getattr(e, "provider_request_params", None)
            if isinstance(error_params, dict):
                params = dict(error_params)
            diagnostic_path = persist_llm_error_diagnostic(
                model=self.model,
                base_url=self.base_url,
                session_id=session_id,
                request_params=params,
                raw_messages=raw_messages,
                sanitized_messages=final_messages,
                error=e,
                metadata=final_metadata,
                provider_id=self.provider_id,
                provider_type=self.provider_type,
                timeout_sec=getattr(self.provider_config, "timeout_sec", None),
                max_retries=getattr(self.provider_config, "max_retries", None),
                duration_ms=int((time.monotonic() - request_started_at) * 1000),
            )
            setattr(e, "llm_diagnostic_path", str(diagnostic_path))
            raise
