"""Tool catalog and exposure planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reuleauxcoder.extensions.tools.spec import ProviderSurface, ToolExposure, ToolSpec


@dataclass(frozen=True)
class ToolCatalogEntry:
    """A concrete executable tool bound to its canonical specification."""

    tool: Any
    spec: ToolSpec

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def tool_id(self) -> str:
        explicit = str(self.spec.metadata.get("tool_id") or "").strip()
        return explicit or f"{self.spec.namespace}:{self.spec.name}"


@dataclass(frozen=True)
class ToolExposurePlan:
    """Stable model exposure and executor route plan for a tool catalog."""

    entries: tuple[ToolCatalogEntry, ...]
    direct: tuple[ToolCatalogEntry, ...]
    deferred: tuple[ToolCatalogEntry, ...]
    hidden: tuple[ToolCatalogEntry, ...]
    hosted: tuple[ToolCatalogEntry, ...]
    executor_routes: dict[str, ToolCatalogEntry]
    executor_routes_by_id: dict[str, ToolCatalogEntry]

    def direct_provider_schemas(self) -> list[dict[str, Any]]:
        """Return stable provider schemas for directly exposed function tools."""
        return [
            entry.spec.to_openai_chat_tool()
            for entry in self.direct
            if entry.spec.provider_surface == ProviderSurface.FUNCTION
        ]

    def get_model_callable_tool(self, name: str) -> Any | None:
        """Return a tool that can be called directly by the model."""
        entry = self.executor_routes.get(str(name or ""))
        if entry is None or entry.spec.exposure != ToolExposure.DIRECT:
            return None
        return entry.tool

    def get_executor(self, name: str) -> Any | None:
        """Return the registered executor for a cataloged tool name."""
        entry = self.executor_routes.get(str(name or ""))
        return entry.tool if entry is not None else None

    def get_executor_by_id(self, tool_id: str) -> Any | None:
        """Return the registered executor for a cataloged tool id."""
        entry = self.executor_routes_by_id.get(str(tool_id or ""))
        return entry.tool if entry is not None else None


@dataclass(frozen=True)
class ToolCatalog:
    """Canonical catalog of registered tool instances."""

    entries: tuple[ToolCatalogEntry, ...]

    @classmethod
    def from_tools(cls, tools: list[Any] | tuple[Any, ...]) -> "ToolCatalog":
        entries = tuple(
            sorted(
                (
                    ToolCatalogEntry(tool=tool, spec=tool.tool_spec())
                    for tool in list(tools or [])
                ),
                key=lambda entry: (entry.spec.namespace, entry.spec.name),
            )
        )
        return cls(entries=entries)

    def exposure_plan(self) -> ToolExposurePlan:
        routes_by_name: dict[str, ToolCatalogEntry] = {}
        routes_by_id: dict[str, ToolCatalogEntry] = {}
        for entry in self.entries:
            existing_id = routes_by_id.get(entry.tool_id)
            if existing_id is not None:
                raise ValueError(
                    "duplicate tool id in tool catalog: "
                    f"{entry.tool_id} ({existing_id.name}, {entry.name})"
                )
            routes_by_id[entry.tool_id] = entry

            existing_name = routes_by_name.get(entry.name)
            if existing_name is None:
                routes_by_name[entry.name] = entry
                continue
            if (
                existing_name.spec.exposure == ToolExposure.DIRECT
                and entry.spec.exposure == ToolExposure.DIRECT
            ):
                raise ValueError(
                    "duplicate direct tool name in tool catalog: "
                    f"{entry.name} ({existing_name.tool_id}, {entry.tool_id})"
                )
            if entry.spec.exposure == ToolExposure.DIRECT:
                routes_by_name[entry.name] = entry
                continue
        return ToolExposurePlan(
            entries=self.entries,
            direct=self._entries_for_exposure(ToolExposure.DIRECT),
            deferred=self._entries_for_exposure(ToolExposure.DEFERRED),
            hidden=self._entries_for_exposure(ToolExposure.HIDDEN),
            hosted=self._entries_for_exposure(ToolExposure.HOSTED),
            executor_routes=routes_by_name,
            executor_routes_by_id=routes_by_id,
        )

    def _entries_for_exposure(
        self,
        exposure: ToolExposure,
    ) -> tuple[ToolCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.spec.exposure == exposure)
