"""Registries for memory provider and source adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from reuleauxcoder.domain.memory.models import MemoryProviderConfigurationError
from reuleauxcoder.domain.memory.provider import MemoryProvider

MemoryProviderAdapterFactory = Callable[[str, dict[str, Any]], MemoryProvider]
MemorySourceAdapterFactory = Callable[[str, dict[str, Any]], Any]


class MemoryProviderRegistry:
    """Resolve configured provider ids through registered adapter factories."""

    _adapter_factories: dict[str, MemoryProviderAdapterFactory] = {}

    def __init__(self, providers: dict[str, dict[str, Any]] | None = None) -> None:
        self._provider_configs = {
            str(provider_id): dict(provider_config)
            for provider_id, provider_config in (providers or {}).items()
            if isinstance(provider_config, dict)
        }
        self._providers: dict[str, MemoryProvider] = {}

    @classmethod
    def register_adapter(
        cls, adapter: str, factory: MemoryProviderAdapterFactory
    ) -> None:
        adapter_id = str(adapter or "").strip()
        if not adapter_id:
            raise ValueError("memory provider adapter id is required")
        cls._adapter_factories[adapter_id] = factory

    @classmethod
    def clear_registered_adapters(cls) -> None:
        cls._adapter_factories.clear()

    @classmethod
    def is_adapter_registered(cls, adapter: str) -> bool:
        return str(adapter or "").strip() in cls._adapter_factories

    @classmethod
    def registered_adapters(cls) -> set[str]:
        return set(cls._adapter_factories)

    def configured_provider_ids(self) -> set[str]:
        return set(self._provider_configs)

    def validate_provider_config(self, provider_id: str) -> None:
        config = self._provider_configs.get(provider_id)
        if config is None:
            raise MemoryProviderConfigurationError(
                f"memory provider '{provider_id}' is not configured"
            )
        if config.get("enabled") is False:
            raise MemoryProviderConfigurationError(
                f"memory provider '{provider_id}' is disabled"
            )
        adapter = str(config.get("adapter") or "").strip()
        if not adapter:
            raise MemoryProviderConfigurationError(
                f"memory.providers.{provider_id}.adapter is required"
            )
        if adapter not in self._adapter_factories:
            raise MemoryProviderConfigurationError(
                f"memory provider adapter '{adapter}' is not registered"
            )

    def provider(self, provider_id: str) -> MemoryProvider:
        key = str(provider_id or "").strip()
        if not key:
            raise MemoryProviderConfigurationError("memory provider id is required")
        existing = self._providers.get(key)
        if existing is not None:
            return existing
        self.validate_provider_config(key)
        config = dict(self._provider_configs[key])
        adapter = str(config.get("adapter") or "").strip()
        factory = self._adapter_factories[adapter]
        provider = factory(key, config)
        self._providers[key] = provider
        return provider


class MemorySourceRegistry:
    """Resolve memory source connector adapters separately from providers."""

    _adapter_factories: dict[str, MemorySourceAdapterFactory] = {}

    def __init__(self, sources: dict[str, dict[str, Any]] | None = None) -> None:
        self._source_configs = {
            str(source_id): dict(source_config)
            for source_id, source_config in (sources or {}).items()
            if isinstance(source_config, dict)
        }
        self._sources: dict[str, Any] = {}

    @classmethod
    def register_adapter(cls, adapter: str, factory: MemorySourceAdapterFactory) -> None:
        adapter_id = str(adapter or "").strip()
        if not adapter_id:
            raise ValueError("memory source adapter id is required")
        cls._adapter_factories[adapter_id] = factory

    @classmethod
    def clear_registered_adapters(cls) -> None:
        cls._adapter_factories.clear()

    @classmethod
    def is_adapter_registered(cls, adapter: str) -> bool:
        return str(adapter or "").strip() in cls._adapter_factories

    def source(self, source_id: str) -> Any:
        key = str(source_id or "").strip()
        if not key:
            raise MemoryProviderConfigurationError("memory source id is required")
        existing = self._sources.get(key)
        if existing is not None:
            return existing
        config = self._source_configs.get(key)
        if config is None:
            raise MemoryProviderConfigurationError(
                f"memory source '{source_id}' is not configured"
            )
        adapter = str(config.get("adapter") or "").strip()
        if not adapter:
            raise MemoryProviderConfigurationError(
                f"memory.sources.{source_id}.adapter is required"
            )
        factory = self._adapter_factories.get(adapter)
        if factory is None:
            raise MemoryProviderConfigurationError(
                f"memory source adapter '{adapter}' is not registered"
            )
        source = factory(key, dict(config))
        self._sources[key] = source
        return source
