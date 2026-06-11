"""Dependency graph helpers for capability package manifests."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.capability_packages import (
    CapabilityDependencyGraph,
    SharedCapabilityRegistryEntry,
)

_SHARED_EXECUTABLES = (
    "git",
    "gh",
    "bash",
    "sh",
    "python3",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "docker",
    "jq",
    "curl",
    "wget",
    "rg",
)


def default_shared_capability_registry() -> dict[str, dict[str, Any]]:
    return {
        f"shared:executable:{name}": _shared_executable_entry(name).to_dict()
        for name in _SHARED_EXECUTABLES
    }


def build_dependency_graph(
    *,
    components: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    registry: dict[str, dict[str, Any]] | None = None,
) -> CapabilityDependencyGraph:
    registry_by_requirement = _registry_by_requirement_id(registry or {})
    requirement_items = _requirements_by_id(requirements, registry_by_requirement)
    edges: list[dict[str, Any]] = []
    blocked: list[str] = []

    for component in _dict_items(components):
        component_id = str(component.get("id") or "").strip()
        if not component_id:
            continue
        for requirement_id in _component_requirement_refs(component):
            if requirement_id not in requirement_items:
                if requirement_id in registry_by_requirement:
                    requirement_items[requirement_id] = _requirement_from_registry(
                        registry_by_requirement[requirement_id]
                    )
                else:
                    edges.append(
                        {
                            "from_component_id": component_id,
                            "to_requirement_id": requirement_id,
                            "status": "invalid",
                            "reason": "missing_requirement",
                        }
                    )
                    blocked.append(component_id)
                    continue
            edge = {
                "from_component_id": component_id,
                "to_requirement_id": requirement_id,
                "status": "valid",
            }
            shared_id = str(requirement_items[requirement_id].get("shared_registry_id") or "")
            if shared_id:
                edge["shared_registry_id"] = shared_id
            edges.append(edge)

    return CapabilityDependencyGraph(
        requirements=dict(sorted(requirement_items.items())),
        edges=edges,
        blocked_component_ids=_unique_strings(blocked),
    )


def _shared_executable_entry(name: str) -> SharedCapabilityRegistryEntry:
    requirement_id = f"envreq:executable:{name}"
    return SharedCapabilityRegistryEntry(
        id=f"shared:executable:{name}",
        requirement_id=requirement_id,
        kind="executable",
        name=name,
        version_check_action={
            "type": "run_executable",
            "requirement_id": requirement_id,
            "executable": name,
            "args": _version_args(name),
        },
        install_action_policy={
            "policy": "target_managed",
            "allowed_targets": ["server", "local_peer"],
            "requires_typed_action": True,
        },
        platforms=["linux", "macos", "windows"],
        credential_interaction=(
            "user_account_optional" if name in {"gh", "docker"} else "not_required"
        ),
        conflict_policy="shared_singleton",
        evidence_required=[
            "source_requirement",
            "version_check_result",
            "target_owner_result",
        ],
    )


def _version_args(name: str) -> list[str]:
    if name in {"sh", "bash"}:
        return ["--version"]
    return ["--version"]


def _registry_by_requirement_id(
    registry: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for shared_id, raw_entry in registry.items():
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        entry.setdefault("id", str(shared_id))
        requirement_id = str(entry.get("requirement_id") or "").strip()
        if requirement_id:
            result[requirement_id] = entry
    return result


def _requirements_by_id(
    requirements: list[dict[str, Any]],
    registry_by_requirement: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in _dict_items(requirements):
        requirement_id = str(item.get("id") or "").strip()
        if not requirement_id:
            continue
        requirement = dict(item)
        registry_entry = registry_by_requirement.get(requirement_id)
        if registry_entry:
            requirement["shared_registry_id"] = str(registry_entry.get("id") or "")
            requirement["shared_registry_entry"] = dict(registry_entry)
        result[requirement_id] = requirement
    return result


def _requirement_from_registry(registry_entry: dict[str, Any]) -> dict[str, Any]:
    requirement = {
        "id": str(registry_entry.get("requirement_id") or ""),
        "kind": str(registry_entry.get("kind") or ""),
        "name": str(registry_entry.get("name") or ""),
        "shared_registry_id": str(registry_entry.get("id") or ""),
        "shared_registry_entry": dict(registry_entry),
    }
    return {key: value for key, value in requirement.items() if value}


def _component_requirement_refs(component: dict[str, Any]) -> list[str]:
    raw_refs = component.get("environment_requirement_refs")
    if not isinstance(raw_refs, list):
        config = component.get("config")
        raw_refs = (
            config.get("environment_requirement_refs")
            if isinstance(config, dict)
            else []
        )
    return _unique_strings([str(item) for item in raw_refs if str(item).strip()])


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


__all__ = ["build_dependency_graph", "default_shared_capability_registry"]
