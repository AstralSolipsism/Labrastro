"""Provider error diagnostics safe for user-facing configuration feedback."""

from __future__ import annotations

from typing import Any
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from reuleauxcoder.domain.config.models import ProviderConfig


_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|authorization|bearer|token|access[_-]?token|refresh[_-]?token|password|secret|client[_-]?secret)\s*[:=]\s*['\"]?[^,'\"\s}&]+"
    ),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
)
_SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "access_token",
    "refresh_token",
    "bearer",
    "client_secret",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}


def provider_error_envelope(
    provider: ProviderConfig | Any | None,
    model: str,
    exc: Exception,
    *,
    code: str = "provider_request_failed",
) -> dict[str, Any]:
    """Build a redacted provider error payload with protocol-mismatch hints."""

    provider_id = _provider_value(provider, "id") or _exception_value(exc, "provider_id")
    provider_type = _provider_value(provider, "type") or _exception_value(exc, "provider_type")
    base_url = _provider_value(provider, "base_url") or _exception_value(exc, "provider_base_url")
    safe_base_url = _redact_url(base_url)
    upstream_status = _upstream_status(exc)
    upstream_message = _redact(_upstream_message(exc))
    suspected_reason, recommended_action = _protocol_hint(
        provider_type=provider_type,
        base_url=base_url,
        upstream_message=upstream_message,
    )
    message = _primary_message(
        upstream_message=upstream_message,
        recommended_action=recommended_action,
    )
    envelope: dict[str, Any] = {
        "error": code,
        "code": code,
        "message": message,
        "provider_id": provider_id,
        "provider_type": provider_type,
        "base_url": safe_base_url,
        "model": str(model or ""),
        "upstream_status": upstream_status,
        "upstream_message": upstream_message,
        "diagnostic_error_type": type(exc).__name__,
        "diagnostic_id": _diagnostic_id(provider_id, provider_type, model),
    }
    request_params = getattr(exc, "provider_request_params", None)
    if isinstance(request_params, dict):
        envelope["request_param_keys"] = sorted(str(key) for key in request_params)
    phase = _exception_value(exc, "provider_error_phase")
    if phase:
        envelope["provider_error_phase"] = phase
    if suspected_reason:
        envelope["suspected_reason"] = suspected_reason
    if recommended_action:
        envelope["recommended_action"] = recommended_action
    return {key: value for key, value in envelope.items() if value not in (None, "")}


def provider_error_message(
    provider: ProviderConfig | Any | None,
    model: str,
    exc: Exception,
) -> str:
    return str(provider_error_envelope(provider, model, exc).get("message") or str(exc))


def _provider_value(provider: ProviderConfig | Any | None, name: str) -> str:
    if provider is None:
        return ""
    return str(getattr(provider, name, "") or "").strip()


def _exception_value(exc: Exception, name: str) -> str:
    return str(getattr(exc, name, "") or "").strip()


def _upstream_status(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _upstream_message(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    message = _message_from_body(body)
    if message:
        return message
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text = response.text
        except Exception:
            text = ""
        if text:
            return str(text)
    return str(exc)


def _message_from_body(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _message_from_body(value)
                if nested:
                    return nested
    return ""


def _redact(value: str, *, limit: int = 800) -> str:
    text = str(value or "").strip()
    text = _SECRET_PATTERNS[0].sub(lambda match: f"{match.group(1)}: [redacted]", text)
    text = _SECRET_PATTERNS[1].sub("[redacted]", text)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _redact_url(value: str, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return _redact(text, limit=limit)
    if not parts.scheme and not parts.netloc:
        return _redact(text, limit=limit)
    hostname = parts.hostname or ""
    if not hostname:
        netloc_tail = parts.netloc.rsplit("@", 1)[-1]
        hostname = netloc_tail.split(":", 1)[0]
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    query = _redact_query(parts.query)
    fragment = _redact(parts.fragment) if parts.fragment else ""
    redacted = urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
    return redacted if len(redacted) <= limit else redacted[: limit - 3] + "..."


def _redact_query(value: str) -> str:
    if not value:
        return ""
    try:
        pairs = parse_qsl(value, keep_blank_values=True)
    except ValueError:
        return _redact(value)
    redacted_pairs = []
    for key, item_value in pairs:
        normalized_key = key.strip().lower()
        if normalized_key in _SENSITIVE_QUERY_KEYS or any(
            token in normalized_key for token in ("token", "secret", "password", "api_key")
        ):
            redacted_pairs.append((key, "[redacted]"))
        else:
            redacted_pairs.append((key, _redact(item_value)))
    return urlencode(redacted_pairs, doseq=True)


def _protocol_hint(
    *,
    provider_type: str,
    base_url: str,
    upstream_message: str,
) -> tuple[str, str]:
    normalized_type = provider_type.strip().lower()
    normalized_url = base_url.strip().lower()
    normalized_message = upstream_message.strip().lower()
    openai_like_url = (
        normalized_url.endswith("/v1")
        or normalized_url.endswith("/api/v1")
        or "zenmux.ai" in normalized_url
        or "openrouter.ai" in normalized_url
    )
    non_anthropic_error = (
        "invalid csrf token" in normalized_message
        or "<html" in normalized_message
        or "not found" in normalized_message
        or "openai" in normalized_message
    )
    if normalized_type == "anthropic_messages" and (openai_like_url or non_anthropic_error):
        return (
            "provider_protocol_mismatch_suspected",
            "This provider is configured as Anthropic Messages, but the endpoint/error looks OpenAI-compatible. Try provider type openai_chat.",
        )
    anthropic_like_url = "api.anthropic.com" in normalized_url
    anthropic_error = "anthropic-version" in normalized_message or "messages api" in normalized_message
    if normalized_type == "openai_chat" and (anthropic_like_url or anthropic_error):
        return (
            "provider_protocol_mismatch_suspected",
            "This provider is configured as OpenAI Chat, but the endpoint/error looks Anthropic Messages. Try provider type anthropic_messages.",
        )
    return "", ""


def _primary_message(*, upstream_message: str, recommended_action: str) -> str:
    if recommended_action:
        return f"{recommended_action} Upstream error: {upstream_message or 'unknown provider error'}"
    return f"Provider request failed. Upstream error: {upstream_message or 'unknown provider error'}"


def _diagnostic_id(provider_id: str, provider_type: str, model: str) -> str:
    parts = [
        "provider",
        provider_id or "unknown",
        provider_type or "unknown",
        str(model or "unknown"),
    ]
    return ":".join(part.replace(":", "_") for part in parts)
