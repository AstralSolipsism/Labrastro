"""Model capability catalog aggregation and cache support."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib import request

from reuleauxcoder.domain.config.models import ProviderConfig


LITELLM_MODEL_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
CATALOG_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ModelCapability:
    provider: str
    model_id: str
    canonical_id: str
    aliases: list[str] = field(default_factory=list)
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_structured_outputs: bool | None = None
    supports_json_output: bool | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None
    supports_parallel_tool_calls: bool | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "medium"
    last_verified_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelCapability":
        return cls(
            provider=str(data.get("provider") or ""),
            model_id=str(data.get("model_id") or ""),
            canonical_id=str(data.get("canonical_id") or ""),
            aliases=[str(item) for item in data.get("aliases", []) if str(item).strip()]
            if isinstance(data.get("aliases"), list)
            else [],
            max_context_tokens=_int_value(data.get("max_context_tokens")),
            max_output_tokens=_int_value(data.get("max_output_tokens")),
            supports_tools=_optional_bool(data.get("supports_tools")),
            supports_structured_outputs=_optional_bool(
                data.get("supports_structured_outputs")
            ),
            supports_json_output=_optional_bool(data.get("supports_json_output")),
            supports_reasoning=_optional_bool(data.get("supports_reasoning")),
            supports_vision=_optional_bool(data.get("supports_vision")),
            supports_parallel_tool_calls=_optional_bool(
                data.get("supports_parallel_tool_calls")
            ),
            input_cost_per_token=_float_value(data.get("input_cost_per_token")),
            output_cost_per_token=_float_value(data.get("output_cost_per_token")),
            sources=[
                dict(item) for item in data.get("sources", []) if isinstance(item, dict)
            ]
            if isinstance(data.get("sources"), list)
            else [],
            confidence=str(data.get("confidence") or "medium"),
            last_verified_at=str(data.get("last_verified_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "canonical_id": self.canonical_id,
            "aliases": list(self.aliases),
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_structured_outputs": self.supports_structured_outputs,
            "supports_json_output": self.supports_json_output,
            "supports_reasoning": self.supports_reasoning,
            "supports_vision": self.supports_vision,
            "supports_parallel_tool_calls": self.supports_parallel_tool_calls,
            "input_cost_per_token": self.input_cost_per_token,
            "output_cost_per_token": self.output_cost_per_token,
            "sources": list(self.sources),
            "confidence": self.confidence,
            "last_verified_at": self.last_verified_at,
        }


class ModelCapabilityCatalogService:
    """Aggregate model capability metadata from remote and built-in sources."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        urlopen: Callable[..., Any] | None = None,
        timeout_sec: int = 20,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_path = cache_dir / "catalog.json"
        self.urlopen = urlopen or request.urlopen
        self.timeout_sec = max(1, int(timeout_sec or 20))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_refresh_error = ""

    def status(self, *, enabled: bool = True, interval_sec: int = 86400) -> dict[str, Any]:
        catalog = self.load_catalog()
        return {
            "enabled": enabled,
            "interval_sec": interval_sec,
            "cache_path": str(self.cache_path),
            "exists": self.cache_path.exists(),
            "updated_at": str(catalog.get("updated_at") or ""),
            "model_count": len(catalog.get("models", []))
            if isinstance(catalog.get("models"), list)
            else 0,
            "sources": catalog.get("sources", [])
            if isinstance(catalog.get("sources"), list)
            else [],
            "last_refresh_error": self._last_refresh_error,
        }

    def start_periodic(self, *, enabled: bool, interval_sec: int) -> None:
        if not enabled or self._thread is not None:
            return
        interval = max(60, int(interval_sec or 86400))
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.refresh()
                except Exception as exc:  # pragma: no cover - defensive background guard
                    self._last_refresh_error = str(exc)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_periodic(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def load_catalog(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return self._catalog_from_capabilities(self._builtin_capabilities(), [])
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return self._catalog_from_capabilities(self._builtin_capabilities(), [])
        return data if isinstance(data, dict) else {}

    def list_capabilities(
        self, *, provider: str = "", model: str = ""
    ) -> list[dict[str, Any]]:
        catalog = self.load_catalog()
        raw_models = catalog.get("models", [])
        models = raw_models if isinstance(raw_models, list) else []
        provider_filter = _normalize_text(provider)
        model_filter = _normalize_text(model)
        filtered: list[dict[str, Any]] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            capability = ModelCapability.from_dict(item)
            if provider_filter and _normalize_text(capability.provider) != provider_filter:
                continue
            haystack = " ".join(
                [capability.model_id, capability.canonical_id, *capability.aliases]
            ).lower()
            if model_filter and model_filter not in haystack:
                continue
            filtered.append(capability.to_dict())
        return filtered

    def lookup(
        self,
        provider: ProviderConfig | str | None,
        model_id: str,
    ) -> ModelCapability | None:
        provider_id = provider.id if isinstance(provider, ProviderConfig) else str(provider or "")
        candidates = _lookup_candidates(provider_id, model_id)
        if isinstance(provider, ProviderConfig):
            if provider.compat:
                candidates.extend(_lookup_candidates(provider.compat, model_id))
            base = str(provider.base_url or "").lower()
            if "deepseek" in base:
                candidates.extend(_lookup_candidates("deepseek", model_id))

        for item in self.list_capabilities():
            capability = ModelCapability.from_dict(item)
            aliases = {
                _normalize_model_key(capability.canonical_id),
                _normalize_model_key(capability.model_id),
                *[_normalize_model_key(alias) for alias in capability.aliases],
            }
            if any(candidate in aliases for candidate in candidates):
                return capability
        return None

    def enrich_model(
        self, provider: ProviderConfig, model: dict[str, Any]
    ) -> dict[str, Any]:
        model_id = str(model.get("id") or model.get("model_id") or model.get("model") or "")
        capability = self.lookup(provider, model_id)
        if capability is None:
            return model
        enriched = dict(model)
        if capability.max_output_tokens:
            enriched["max_tokens"] = capability.max_output_tokens
        if capability.max_context_tokens:
            enriched["max_context_tokens"] = capability.max_context_tokens
        enriched["capability_source"] = _capability_source_label(capability)
        enriched["capability"] = capability.to_dict()
        for field_name in (
            "supports_tools",
            "supports_structured_outputs",
            "supports_json_output",
            "supports_reasoning",
            "supports_vision",
            "supports_parallel_tool_calls",
        ):
            value = getattr(capability, field_name)
            if value is not None:
                enriched[field_name] = value
        return enriched

    def refresh(self) -> dict[str, Any]:
        source_results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        capabilities: list[ModelCapability] = []

        for source_name, loader in (
            ("litellm", self._fetch_litellm_capabilities),
            ("openrouter", self._fetch_openrouter_capabilities),
            ("builtin", self._builtin_capabilities),
        ):
            try:
                items = loader()
                capabilities.extend(items)
                source_results.append(
                    {
                        "source": source_name,
                        "model_count": len(items),
                        "fetched_at": utc_now_iso(),
                    }
                )
            except Exception as exc:
                errors.append({"source": source_name, "message": str(exc)})

        if not capabilities:
            self._last_refresh_error = "; ".join(
                f"{item['source']}: {item['message']}" for item in errors
            )
            return {
                "ok": False,
                "error": "model_capabilities_refresh_failed",
                "errors": errors,
                **self.status(),
            }

        catalog = self._catalog_from_capabilities(capabilities, source_results)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cache_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.cache_path)
        self._last_refresh_error = ""
        return {
            "ok": True,
            "errors": errors,
            "model_capabilities": self.status(),
        }

    def _catalog_from_capabilities(
        self,
        capabilities: list[ModelCapability],
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        merged = _merge_capabilities(capabilities)
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "sources": sources,
            "models": [item.to_dict() for item in merged],
        }

    def _fetch_json(self, url: str) -> Any:
        with self.urlopen(url, timeout=self.timeout_sec) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def _fetch_litellm_capabilities(self) -> list[ModelCapability]:
        data = self._fetch_json(LITELLM_MODEL_PRICES_URL)
        if not isinstance(data, dict):
            return []
        fetched_at = utc_now_iso()
        capabilities: list[ModelCapability] = []
        for raw_model_id, raw_entry in data.items():
            if raw_model_id == "sample_spec" or not isinstance(raw_entry, dict):
                continue
            provider = str(raw_entry.get("litellm_provider") or "").strip()
            model_id = _strip_provider_prefix(str(raw_model_id), provider)
            if not model_id:
                continue
            aliases = _unique_aliases([str(raw_model_id), model_id])
            capabilities.append(
                ModelCapability(
                    provider=provider,
                    model_id=model_id,
                    canonical_id=_canonical_id(provider, model_id),
                    aliases=aliases,
                    max_context_tokens=_int_value(raw_entry.get("max_input_tokens")),
                    max_output_tokens=_int_value(raw_entry.get("max_output_tokens"))
                    or _int_value(raw_entry.get("max_tokens")),
                    supports_tools=_optional_bool(
                        raw_entry.get("supports_function_calling")
                    ),
                    supports_structured_outputs=_optional_bool(
                        raw_entry.get("supports_response_schema")
                        or raw_entry.get("supports_structured_outputs")
                    ),
                    supports_json_output=_optional_bool(
                        raw_entry.get("supports_response_schema")
                        or raw_entry.get("supports_json_mode")
                    ),
                    supports_reasoning=_optional_bool(
                        raw_entry.get("supports_reasoning")
                    ),
                    supports_vision=_optional_bool(raw_entry.get("supports_vision")),
                    supports_parallel_tool_calls=_optional_bool(
                        raw_entry.get("supports_parallel_function_calling")
                    ),
                    input_cost_per_token=_float_value(
                        raw_entry.get("input_cost_per_token")
                    ),
                    output_cost_per_token=_float_value(
                        raw_entry.get("output_cost_per_token")
                    ),
                    sources=[
                        {
                            "source": "litellm",
                            "source_url": LITELLM_MODEL_PRICES_URL,
                            "fetched_at": fetched_at,
                        }
                    ],
                    confidence="medium",
                    last_verified_at=fetched_at,
                )
            )
        return capabilities

    def _fetch_openrouter_capabilities(self) -> list[ModelCapability]:
        data = self._fetch_json(OPENROUTER_MODELS_URL)
        items = data.get("data", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []
        fetched_at = utc_now_iso()
        capabilities: list[ModelCapability] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            raw_id = str(raw_item.get("id") or "").strip()
            if not raw_id:
                continue
            provider, model_id = _split_openrouter_id(raw_id)
            top_provider = (
                raw_item.get("top_provider")
                if isinstance(raw_item.get("top_provider"), dict)
                else {}
            )
            supported = raw_item.get("supported_parameters", [])
            supported_params = (
                {str(item) for item in supported} if isinstance(supported, list) else set()
            )
            architecture = (
                raw_item.get("architecture")
                if isinstance(raw_item.get("architecture"), dict)
                else {}
            )
            modalities = " ".join(
                str(value)
                for value in architecture.values()
                if isinstance(value, str)
            ).lower()
            capabilities.append(
                ModelCapability(
                    provider=provider,
                    model_id=model_id,
                    canonical_id=_canonical_id(provider, model_id),
                    aliases=_unique_aliases([raw_id, model_id, model_id.split(":")[0]]),
                    max_context_tokens=_int_value(raw_item.get("context_length"))
                    or _int_value(top_provider.get("context_length")),
                    max_output_tokens=_int_value(
                        top_provider.get("max_completion_tokens")
                    ),
                    supports_tools="tools" in supported_params,
                    supports_structured_outputs="structured_outputs" in supported_params
                    or "response_format" in supported_params,
                    supports_json_output="response_format" in supported_params,
                    supports_reasoning="reasoning" in supported_params
                    or "include_reasoning" in supported_params,
                    supports_vision="image" in modalities or "vision" in modalities,
                    supports_parallel_tool_calls=None,
                    sources=[
                        {
                            "source": "openrouter",
                            "source_url": OPENROUTER_MODELS_URL,
                            "fetched_at": fetched_at,
                        }
                    ],
                    confidence="medium",
                    last_verified_at=fetched_at,
                )
            )
        return capabilities

    def _builtin_capabilities(self) -> list[ModelCapability]:
        fetched_at = utc_now_iso()
        base = {
            "provider": "deepseek",
            "max_context_tokens": 1_000_000,
            "max_output_tokens": 384_000,
            "supports_tools": True,
            "supports_structured_outputs": True,
            "supports_json_output": True,
            "supports_reasoning": True,
            "supports_vision": False,
            "supports_parallel_tool_calls": None,
            "sources": [
                {
                    "source": "builtin_deepseek_v4",
                    "source_url": DEEPSEEK_PRICING_URL,
                    "fetched_at": fetched_at,
                }
            ],
            "confidence": "high",
            "last_verified_at": fetched_at,
        }
        return [
            ModelCapability(
                model_id="deepseek-v4-flash",
                canonical_id="deepseek/deepseek-v4-flash",
                aliases=["deepseek-v4-flash", "deepseek/deepseek-v4-flash"],
                **base,
            ),
            ModelCapability(
                model_id="deepseek-v4-pro",
                canonical_id="deepseek/deepseek-v4-pro",
                aliases=["deepseek-v4-pro", "deepseek/deepseek-v4-pro"],
                **base,
            ),
        ]


def capability_recommendation(
    capability: ModelCapability | None,
    *,
    current_max_tokens: int | None,
    current_max_context_tokens: int | None,
) -> dict[str, Any] | None:
    if capability is None:
        return None
    recommended_max_tokens = capability.max_output_tokens
    recommended_max_context = capability.max_context_tokens
    if not recommended_max_tokens and not recommended_max_context:
        return None
    differs = (
        bool(recommended_max_tokens and recommended_max_tokens != current_max_tokens)
        or bool(
            recommended_max_context
            and recommended_max_context != current_max_context_tokens
        )
    )
    if not differs:
        return None
    return {
        "max_tokens": recommended_max_tokens,
        "max_context_tokens": recommended_max_context,
        "current_max_tokens": current_max_tokens,
        "current_max_context_tokens": current_max_context_tokens,
        "source": _capability_source_label(capability),
        "capability": capability.to_dict(),
    }


def capability_source_label(capability: ModelCapability) -> str:
    return _capability_source_label(capability)


def _merge_capabilities(capabilities: list[ModelCapability]) -> list[ModelCapability]:
    catalog: dict[str, ModelCapability] = {}
    alias_to_key: dict[str, str] = {}
    for capability in capabilities:
        aliases = _capability_aliases(capability)
        key = next((alias_to_key[alias] for alias in aliases if alias in alias_to_key), None)
        if key is None:
            key = _normalize_model_key(capability.canonical_id or capability.model_id)
            catalog[key] = capability
        else:
            catalog[key] = _merge_one(catalog[key], capability)
        for alias in aliases:
            alias_to_key[alias] = key
    return sorted(catalog.values(), key=lambda item: item.canonical_id or item.model_id)


def _merge_one(base: ModelCapability, incoming: ModelCapability) -> ModelCapability:
    data = base.to_dict()
    incoming_data = incoming.to_dict()
    for key, value in incoming_data.items():
        if key == "aliases":
            data["aliases"] = _unique_aliases([*data.get("aliases", []), *value])
        elif key == "sources":
            data["sources"] = [*data.get("sources", []), *value]
        elif value not in (None, "", []):
            data[key] = value
    return ModelCapability.from_dict(data)


def _capability_aliases(capability: ModelCapability) -> set[str]:
    return {
        alias
        for alias in (
            _normalize_model_key(capability.canonical_id),
            _normalize_model_key(capability.model_id),
            *[_normalize_model_key(item) for item in capability.aliases],
        )
        if alias
    }


def _lookup_candidates(provider: str, model_id: str) -> list[str]:
    provider_text = _normalize_text(provider)
    model_text = _normalize_model_key(model_id)
    raw_text = _normalize_model_key(f"{provider_text}/{model_text}") if provider_text else ""
    candidates = [model_text]
    if raw_text:
        candidates.append(raw_text)
    if ":" in model_text:
        candidates.append(model_text.split(":", 1)[0])
        if provider_text:
            candidates.append(f"{provider_text}/{model_text.split(':', 1)[0]}")
    return [item for item in dict.fromkeys(candidates) if item]


def _canonical_id(provider: str, model_id: str) -> str:
    provider = _normalize_text(provider)
    model_id = str(model_id or "").strip()
    return f"{provider}/{model_id}" if provider else model_id


def _split_openrouter_id(raw_id: str) -> tuple[str, str]:
    if "/" not in raw_id:
        return "", raw_id
    provider, model_id = raw_id.split("/", 1)
    return provider.strip(), model_id.strip()


def _strip_provider_prefix(raw_model_id: str, provider: str) -> str:
    text = str(raw_model_id or "").strip()
    provider = str(provider or "").strip()
    if provider and text.lower().startswith(f"{provider.lower()}/"):
        return text.split("/", 1)[1].strip()
    return text


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_model_key(value: Any) -> str:
    return _normalize_text(value).replace(" ", "")


def _unique_aliases(values: list[str]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _normalize_model_key(text)
        if text and key not in seen:
            seen.add(key)
            aliases.append(text)
    return aliases


def _capability_source_label(capability: ModelCapability) -> str:
    if capability.sources:
        source = capability.sources[-1]
        source_name = str(source.get("source") or "")
        if source_name == "builtin_deepseek_v4":
            return "DeepSeek API Docs / Models & Pricing"
        return source_name or str(source.get("source_url") or "catalog")
    return "catalog"


def _int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None
