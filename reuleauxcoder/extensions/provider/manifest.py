"""Provider manifest CLI helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reuleauxcoder.domain.config.models import (
    ProviderApiFeatures,
    ProviderConfig,
    infer_provider_compat,
)
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigEnvironmentError, ConfigLoader
from reuleauxcoder.services.providers.manager import ProviderManager


@dataclass(slots=True)
class ProviderRecordResult:
    provider_id: str
    path: Path
    created: bool


class ProviderManifestManager:
    """Record and read server-side LLM provider entries."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or ConfigLoader.GLOBAL_CONFIG_PATH

    def record_provider(self, provider: ProviderConfig) -> ProviderRecordResult:
        if not provider.id.strip():
            raise ValueError("provider id is required")
        data = self._load_data()
        providers_data = data.setdefault("providers", {})
        if not isinstance(providers_data, dict):
            providers_data = {}
            data["providers"] = providers_data
        items = providers_data.setdefault("items", {})
        if not isinstance(items, dict):
            items = {}
            providers_data["items"] = items
        created = provider.id not in items
        items[provider.id] = provider.to_dict()
        save_yaml_config(self.config_path, data)
        return ProviderRecordResult(provider.id, self.config_path, created)

    def list_providers(self) -> dict[str, ProviderConfig]:
        data = self._load_data()
        data = ConfigLoader()._expand_env_refs(data)
        raw_items = ((data.get("providers") or {}).get("items") or {})
        if not isinstance(raw_items, dict):
            return {}
        providers: dict[str, ProviderConfig] = {}
        for provider_id, provider_data in raw_items.items():
            if isinstance(provider_data, dict):
                providers[str(provider_id)] = ProviderConfig.from_dict(
                    str(provider_id), provider_data
                )
        return providers

    def raw_provider(self, provider_id: str) -> ProviderConfig | None:
        providers = self.list_providers()
        return providers.get(provider_id)

    def _load_data(self) -> dict:
        try:
            data = load_yaml_config(self.config_path)
        except FileNotFoundError:
            data = {}
        return data if isinstance(data, dict) else {}


def run_provider_record_cli(args) -> int:
    try:
        if args.api_key and args.api_key_env:
            raise ValueError("use either --api-key or --api-key-env, not both")
        if args.base_url and args.base_url_env:
            raise ValueError("use either --base-url or --base-url-env, not both")
        api_key = args.api_key or (f"${{{args.api_key_env}}}" if args.api_key_env else "")
        base_url = args.base_url or (
            f"${{{args.base_url_env}}}" if args.base_url_env else None
        )
        provider = ProviderConfig(
            id=str(args.provider_id),
            type=args.provider_type,
            compat=args.compat or infer_provider_compat(base_url),
            api_key=str(api_key or ""),
            base_url=base_url,
            headers=_parse_key_value_entries(list(args.header or []), "--header"),
            timeout_sec=int(args.timeout_sec),
            max_retries=int(args.max_retries),
            api_features=ProviderApiFeatures.from_dict(
                _parse_api_feature_entries(list(args.api_feature or [])),
                provider_type=args.provider_type,
            ),
            extra=_parse_extra_entries(list(args.extra or [])),
        )
        config_path = resolve_cli_config_path(
            args, require=True, purpose="provider record"
        )
        result = ProviderManifestManager(config_path).record_provider(provider)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    verb = "Created" if result.created else "Updated"
    print(f"{verb} provider entry '{result.provider_id}' in {result.path}")
    return 0


def run_provider_list_cli(args) -> int:
    try:
        providers = ProviderManifestManager(
            resolve_cli_config_path(args, require=False, purpose="provider list")
        ).list_providers()
    except (ConfigEnvironmentError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    if not providers:
        print("No providers configured.")
        return 0
    for provider_id in sorted(providers):
        provider = providers[provider_id]
        caps = [
            key
            for key, enabled in provider.api_features.to_dict().items()
            if enabled
        ]
        print(
            f"{provider.id}\t{provider.type}\tcompat={provider.compat}\tapi_key={_mask(provider.api_key)}\t"
            f"base_url={provider.base_url or '-'}\tapi_features={','.join(sorted(caps))}"
        )
    return 0


def run_provider_test_cli(args) -> int:
    try:
        provider = ProviderManifestManager(
            resolve_cli_config_path(args, require=False, purpose="provider test")
        ).raw_provider(str(args.provider_id))
        if provider is None:
            print(f"Error: provider '{args.provider_id}' is not configured")
            return 1
        response = ProviderManager().create(provider).test(
            model=str(args.model), prompt=str(args.prompt or "ping")
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    preview = response.content.strip().replace("\n", " ")
    if len(preview) > 200:
        preview = preview[:197] + "..."
    print(
        f"Provider '{provider.id}' ok: model={args.model}, "
        f"tokens={response.prompt_tokens + response.completion_tokens}, response={preview!r}"
    )
    return 0


def _parse_key_value_entries(entries: list[str], option_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"invalid {option_name} entry, expected KEY=VALUE: {entry}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid {option_name} entry, empty key: {entry}")
        result[key] = value
    return result


def _parse_api_feature_entries(entries: list[str]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(
                f"invalid --api-feature entry, expected NAME=true|false: {entry}"
            )
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid --api-feature entry, empty name: {entry}")
        result[key] = value.strip().lower() in {"1", "true", "yes", "on"}
    return result


def _parse_extra_entries(entries: list[str]) -> dict[str, Any]:
    return _parse_key_value_entries(entries, "--extra")


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


__all__ = [
    "ProviderManifestManager",
    "ProviderRecordResult",
    "run_provider_list_cli",
    "run_provider_record_cli",
    "run_provider_test_cli",
]
