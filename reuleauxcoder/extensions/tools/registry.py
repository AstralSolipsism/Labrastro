"""Decorator-based tool registry and builders."""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules
from typing import Optional

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.catalog import ToolCatalog, ToolExposurePlan
from reuleauxcoder.extensions.tools.spec import ToolExposure, ToolSpec

_BUILTIN_TOOL_PACKAGE = "reuleauxcoder.extensions.tools.builtin"
_TOOL_CLASSES: list[type[Tool]] = []


def register_tool(cls: type[Tool]) -> type[Tool]:
    """Register a tool class for builder-based instantiation."""
    if cls not in _TOOL_CLASSES:
        _TOOL_CLASSES.append(cls)
    return cls


def _import_builtin_tool_modules() -> None:
    """Import builtin tool modules so decorator registrations run."""
    package = import_module(_BUILTIN_TOOL_PACKAGE)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return

    for module_info in iter_modules(package_paths):
        if module_info.name.startswith("_"):
            continue
        import_module(f"{_BUILTIN_TOOL_PACKAGE}.{module_info.name}")


def iter_tool_classes() -> tuple[type[Tool], ...]:
    """Return registered tool classes."""
    _import_builtin_tool_modules()
    return tuple(sorted(_TOOL_CLASSES, key=lambda tool_cls: tool_cls.name))


def build_tools(backend: ToolBackend | None = None) -> list[Tool]:
    """Instantiate all registered tool classes with the provided backend."""
    effective_backend = backend or LocalToolBackend()
    return [tool_cls(backend=effective_backend) for tool_cls in iter_tool_classes()]


def build_tool_catalog(backend: ToolBackend | None = None) -> ToolCatalog:
    """Build the canonical catalog for registered builtin tools."""
    return ToolCatalog.from_tools(build_tools(backend=backend))


def build_tool_exposure_plan(
    backend: ToolBackend | None = None,
) -> ToolExposurePlan:
    """Build the exposure plan for registered builtin tools."""
    return build_tool_catalog(backend=backend).exposure_plan()


def build_tool_specs(
    backend: ToolBackend | None = None,
    *,
    exposure: ToolExposure | None = None,
) -> tuple[ToolSpec, ...]:
    """Build the stable canonical catalog for registered tools."""
    specs = tuple(entry.spec for entry in build_tool_catalog(backend=backend).entries)
    if exposure is not None:
        specs = tuple(spec for spec in specs if spec.exposure == exposure)
    return tuple(sorted(specs, key=lambda spec: (spec.namespace, spec.name)))


def get_tool(name: str, backend: ToolBackend | None = None) -> Optional[Tool]:
    """Instantiate a tool by name."""
    for tool_cls in iter_tool_classes():
        if tool_cls.name == name:
            return tool_cls(backend=backend or LocalToolBackend())
    return None


# ALL_TOOLS removed — use build_tools(backend) for explicit instantiation.
# Previously this was a module-level singleton built eagerly at import time.
