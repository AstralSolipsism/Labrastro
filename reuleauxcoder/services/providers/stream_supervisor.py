"""Shared provider stream supervision and interruption modeling."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.providers.models import ProviderResponse


@dataclass(frozen=True)
class StreamRecoveryPolicy:
    """Provider stream recovery behavior resolved from provider config."""

    enabled: bool = True
    max_continue_attempts: int = 1
    retry_empty_once: bool = True
    retry_tool_delta_once: bool = True
    fallback_models: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Any) -> "StreamRecoveryPolicy":
        first_class = getattr(config, "stream_recovery", None)
        if first_class is not None:
            if hasattr(first_class, "to_dict"):
                raw_first = first_class.to_dict()
                return cls.from_dict(raw_first)
            if isinstance(first_class, dict):
                return cls.from_dict(first_class)
        extra = getattr(config, "extra", {}) or {}
        raw = extra.get("stream_recovery") if isinstance(extra, dict) else None
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> "StreamRecoveryPolicy":
        if not isinstance(raw, dict):
            return cls()
        fallbacks = raw.get("fallback_models")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            max_continue_attempts=max(0, int(raw.get("max_continue_attempts", 1) or 0)),
            retry_empty_once=bool(raw.get("retry_empty_once", True)),
            retry_tool_delta_once=bool(raw.get("retry_tool_delta_once", True)),
            fallback_models=[
                dict(item) for item in fallbacks if isinstance(item, dict)
            ]
            if isinstance(fallbacks, list)
            else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_continue_attempts": self.max_continue_attempts,
            "retry_empty_once": self.retry_empty_once,
            "retry_tool_delta_once": self.retry_tool_delta_once,
            "fallback_models": [dict(item) for item in self.fallback_models],
        }


class ProviderStreamInterruptedError(RuntimeError):
    """Raised when a provider stream stops after a recoverable partial result."""

    def __init__(
        self,
        message: str,
        *,
        original_error: Exception,
        partial_response: ProviderResponse,
        interruption: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.original_error = original_error
        self.partial_response = partial_response
        self.interruption = interruption
        self.__cause__ = original_error


def attach_stream_exception_diagnostics(
    exc: Exception,
    *,
    provider_id: str,
    provider_type: str,
    params: dict[str, Any],
    phase: str,
    interruption: dict[str, Any],
    attempts: list[dict[str, Any]] | None = None,
    stream_options_enabled: bool | None = None,
    debug_http_chunks: list[dict[str, Any]] | None = None,
) -> None:
    values: dict[str, Any] = {
        "provider_id": provider_id,
        "provider_type": provider_type,
        "provider_error_phase": phase,
        "provider_request_params": dict(params),
        "provider_stream_status": "interrupted",
        "provider_stream_interruption": dict(interruption),
        "provider_stream_classification": interruption.get("classification"),
        "provider_stream_chunk_count": interruption.get("chunk_count"),
    }
    if attempts is not None:
        values["provider_retry_attempts"] = list(attempts)
    if stream_options_enabled is not None:
        values["provider_stream_options_enabled"] = stream_options_enabled
    if debug_http_chunks:
        values["provider_debug_http_chunks"] = list(debug_http_chunks)
    for name, value in values.items():
        try:
            setattr(exc, name, value)
        except Exception:
            pass


class StreamSupervisor:
    """Wrap provider stream iteration and standardize partial interruptions."""

    def __init__(
        self,
        *,
        provider_id: str,
        provider_type: str,
        params: dict[str, Any],
        partial_response_factory: Callable[[], ProviderResponse],
        attempts: list[dict[str, Any]] | None = None,
        stream_options_enabled: bool | None = None,
        debug_http_chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.params = params
        self.partial_response_factory = partial_response_factory
        self.attempts = attempts
        self.stream_options_enabled = stream_options_enabled
        self.debug_http_chunks = debug_http_chunks
        self.phase = "stream_start"
        self.chunk_count = 0
        self.started_at = time.time()
        self.last_event_at: float | None = None

    def consume(
        self,
        stream: Iterable[Any],
        decoder: Callable[[int, Any], None],
    ) -> None:
        try:
            self.phase = "stream_iterate"
            for chunk_index, chunk in enumerate(stream):
                self.chunk_count = chunk_index + 1
                self.last_event_at = time.time()
                decoder(chunk_index, chunk)
            self.phase = "stream_complete"
        except ProviderStreamInterruptedError:
            raise
        except Exception as exc:
            partial = self.partial_response_factory()
            interruption = self._interruption_payload(exc, partial)
            partial.stream_status = "interrupted"
            partial.interruption = interruption
            partial.recovery = {
                "attempted": False,
                "action": interruption["retry_action"],
                "attempt": 0,
                "max_attempts": 1,
            }
            attach_stream_exception_diagnostics(
                exc,
                provider_id=self.provider_id,
                provider_type=self.provider_type,
                params=self.params,
                phase=self.phase,
                interruption=interruption,
                attempts=self.attempts,
                stream_options_enabled=self.stream_options_enabled,
                debug_http_chunks=self.debug_http_chunks,
            )
            raise ProviderStreamInterruptedError(
                str(exc) or type(exc).__name__,
                original_error=exc,
                partial_response=partial,
                interruption=interruption,
            ) from exc

    def _interruption_payload(
        self, exc: Exception, partial: ProviderResponse
    ) -> dict[str, Any]:
        partial_kind = _partial_kind(partial)
        retry_action = "continue" if partial_kind in {"text", "reasoning"} else "retry"
        classification = {
            "empty": "empty_interrupted",
            "text": "text_interrupted",
            "reasoning": "reasoning_interrupted",
            "tool_call_delta": "tool_call_delta_interrupted",
            "usage": "usage_after_output_interrupted",
        }[partial_kind]
        return {
            "phase": self.phase,
            "classification": classification,
            "recoverable": True,
            "chunk_count": self.chunk_count,
            "partial_kind": partial_kind,
            "retry_action": retry_action,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "last_event_at": self.last_event_at,
            "duration_ms": int((time.time() - self.started_at) * 1000),
        }


def _partial_kind(partial: ProviderResponse) -> str:
    stream_partial = partial.provider_extra.get("stream_partial")
    has_tool_delta = (
        isinstance(stream_partial, dict)
        and bool(stream_partial.get("has_tool_delta"))
    )
    if partial.content:
        return "text"
    if partial.reasoning_content or partial.reasoning_details:
        return "reasoning"
    if has_tool_delta:
        return "tool_call_delta"
    if partial.prompt_tokens or partial.completion_tokens or partial.usage_extra:
        return "usage"
    return "empty"
