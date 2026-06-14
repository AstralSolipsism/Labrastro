"""Capability package upstream transition helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from reuleauxcoder.domain.capability_packages import (
    CapabilityManifest,
    CapabilitySourceSnapshot,
    capability_package_is_active,
)


_MANIFEST_DIFF_SECTIONS = (
    "package",
    "components",
    "dependency_edges",
    "environment_requirements",
    "credential_requirements",
    "install_plans",
    "activation_rules",
    "exposed_file_closures",
    "update_metadata",
)

_VOLATILE_MANIFEST_FIELDS = {
    "created_at",
    "last_checked_at",
    "updated_at",
}

_LIST_MANIFEST_DIFF_SECTIONS = {
    "components",
    "dependency_edges",
    "environment_requirements",
    "credential_requirements",
    "install_plans",
    "exposed_file_closures",
}


@dataclass(frozen=True)
class CapabilityPackageTransitionPayload:
    next_source_snapshot: dict[str, Any]
    next_manifest: dict[str, Any]
    transition_id: str = ""
    impact_summary: str = ""

    @property
    def has_transition(self) -> bool:
        return bool(self.next_source_snapshot and self.next_manifest)


def detect_upstream_version(value: dict[str, Any]) -> str:
    """Return the user-visible upstream version without minting a Labrastro version."""

    for key in ("upstream_version", "release", "tag", "version"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    commit_sha = str(
        value.get("commit_sha") or value.get("commit") or value.get("sha") or ""
    ).strip()
    source_ref = str(
        value.get("source_ref") or value.get("ref") or value.get("branch") or ""
    ).strip()
    if source_ref and commit_sha:
        return f"{source_ref}@{commit_sha[:7]}"
    if commit_sha:
        return commit_sha[:7]
    if source_ref:
        return source_ref
    return "unversioned"


def normalize_source_snapshot(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    return CapabilitySourceSnapshot.from_dict(value).to_dict()


def normalize_update_transition_payload(
    payload: dict[str, Any] | None,
) -> CapabilityPackageTransitionPayload:
    value = payload if isinstance(payload, dict) else {}
    raw_snapshot = _first_dict(
        value,
        "next_source_snapshot",
        "source_snapshot",
        "snapshot",
    )
    raw_manifest = _first_dict(
        value,
        "next_manifest",
        "manifest",
        "capability_manifest",
    )
    return CapabilityPackageTransitionPayload(
        next_source_snapshot=normalize_source_snapshot(raw_snapshot),
        next_manifest=CapabilityManifest.from_dict(raw_manifest).to_dict()
        if raw_manifest
        else {},
        transition_id=str(value.get("transition_id") or "").strip(),
        impact_summary=str(
            value.get("impact_summary") or value.get("change_summary") or ""
        ).strip(),
    )


def manifest_diff(
    current_manifest: dict[str, Any] | None,
    next_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute a deterministic backend-owned diff between normalized manifests."""

    current = deepcopy(current_manifest) if isinstance(current_manifest, dict) else {}
    next_value = deepcopy(next_manifest) if isinstance(next_manifest, dict) else {}
    current_components = _items_by_id(current.get("components"))
    next_components = _items_by_id(next_value.get("components"))
    added = sorted(set(next_components) - set(current_components))
    removed = sorted(set(current_components) - set(next_components))
    changed = sorted(
        component_id
        for component_id in set(current_components).intersection(next_components)
        if _stable_item(current_components[component_id])
        != _stable_item(next_components[component_id])
    )
    return {
        "added_components": added,
        "removed_components": removed,
        "changed_components": changed,
        "changed_sections": _changed_manifest_sections(current, next_value),
        "component_count_before": len(current_components),
        "component_count_after": len(next_components),
        "dependency_edge_delta": _list_delta_count(
            current.get("dependency_edges"),
            next_value.get("dependency_edges"),
        ),
        "environment_requirement_delta": _list_delta_count(
            current.get("environment_requirements"),
            next_value.get("environment_requirements"),
        ),
        "credential_requirement_delta": _list_delta_count(
            current.get("credential_requirements"),
            next_value.get("credential_requirements"),
        ),
    }


def manifest_diff_has_changes(value: dict[str, Any] | None) -> bool:
    diff = _dict(value)
    for key in ("added_components", "removed_components", "changed_components"):
        items = diff.get(key)
        if isinstance(items, list) and items:
            return True
    changed_sections = diff.get("changed_sections")
    if isinstance(changed_sections, list) and changed_sections:
        return True
    for key in (
        "dependency_edge_delta",
        "environment_requirement_delta",
        "credential_requirement_delta",
    ):
        if int(diff.get(key) or 0) != 0:
            return True
    return False


def build_update_transition_patch(
    *,
    package_id: str,
    current_package: dict[str, Any] | None,
    next_source_snapshot: dict[str, Any],
    next_manifest: dict[str, Any],
    transition_id: str = "",
    impact_summary: str = "",
) -> dict[str, Any]:
    """Build a backend-owned update transition patch from fetched upstream data."""

    current = deepcopy(current_package) if isinstance(current_package, dict) else {}
    snapshot = normalize_source_snapshot(next_source_snapshot)
    manifest = (
        CapabilityManifest.from_dict(next_manifest).to_dict()
        if isinstance(next_manifest, dict)
        else {}
    )
    current_snapshot = normalize_source_snapshot(_dict(current.get("source_snapshot")))
    current_manifest = _dict(current.get("manifest"))
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    patch_id = (
        str(transition_id or "").strip()
        or f"{package_id}:{snapshot_id or detect_upstream_version(snapshot)}"
    )
    return {
        "transition_id": patch_id,
        "package_id": str(package_id),
        "upstream_version": detect_upstream_version(snapshot),
        "source_snapshot": snapshot,
        "manifest": manifest,
        "manifest_diff": manifest_diff(current_manifest, manifest),
        "previous_snapshot_id": str(current_snapshot.get("snapshot_id") or "").strip(),
        "previous_source_snapshot": current_snapshot,
        "previous_manifest": current_manifest,
        "impact_summary": str(impact_summary or "").strip(),
        "activation_required": True,
        "state": {"update_state": "candidate_ready"},
    }


def apply_update_transition_patch(
    current_package: dict[str, Any],
    patch: dict[str, Any],
    *,
    activation_approved: bool = False,
) -> dict[str, Any]:
    """Apply a prepared transition patch without implicitly activating it."""

    current = deepcopy(current_package) if isinstance(current_package, dict) else {}
    applied = deepcopy(current)
    next_snapshot = normalize_source_snapshot(
        _dict(patch.get("source_snapshot"))
    )
    next_manifest = _dict(patch.get("manifest"))
    if next_snapshot:
        applied["source_snapshot"] = next_snapshot
    if next_manifest:
        applied["manifest"] = next_manifest
        component_ids = _manifest_component_ids(next_manifest)
        if component_ids:
            applied["components"] = component_ids
    applied["upstream_version"] = str(
        patch.get("upstream_version") or detect_upstream_version(next_snapshot)
    )
    applied["status"] = "installed"
    was_active = capability_package_is_active(current)
    applied["enabled"] = bool(was_active and activation_approved)
    rollback_snapshot = normalize_source_snapshot(
        _dict(patch.get("previous_source_snapshot"))
    ) or normalize_source_snapshot(
        _dict(current.get("source_snapshot"))
    )
    previous_manifest = _dict(patch.get("previous_manifest")) or _dict(
        current.get("manifest")
    )
    rollback: dict[str, Any] = {
        "snapshot_id": str(
            patch.get("previous_snapshot_id")
            or rollback_snapshot.get("snapshot_id")
            or ""
        ).strip(),
        "source_snapshot": rollback_snapshot,
        "manifest": previous_manifest,
        "transition_id": str(patch.get("transition_id") or "").strip(),
    }
    applied["rollback"] = {key: value for key, value in rollback.items() if value}
    applied["last_update"] = {
        "transition_id": str(patch.get("transition_id") or "").strip(),
        "upstream_version": applied["upstream_version"],
        "manifest_diff": _dict(patch.get("manifest_diff")),
    }
    state = _dict(current.get("state"))
    state.update(
        {
            "install_state": "installed",
            "activation_state": "active" if applied["enabled"] else "inactive",
            "update_state": "rollback_available",
        }
    )
    applied["state"] = state
    return applied


def apply_rollback_transition_patch(
    current_package: dict[str, Any],
    *,
    activation_approved: bool = False,
) -> dict[str, Any]:
    """Restore the rollback snapshot while keeping activation explicitly gated."""

    current = deepcopy(current_package) if isinstance(current_package, dict) else {}
    rollback = _dict(current.get("rollback"))
    restored = deepcopy(current)
    rollback_snapshot = normalize_source_snapshot(
        _dict(rollback.get("source_snapshot"))
    )
    restored_manifest = _dict(rollback.get("manifest"))
    if rollback_snapshot:
        restored["source_snapshot"] = rollback_snapshot
        restored["upstream_version"] = detect_upstream_version(rollback_snapshot)
    if restored_manifest:
        restored["manifest"] = restored_manifest
        component_ids = _manifest_component_ids(restored_manifest)
        if component_ids:
            restored["components"] = component_ids
    restored["enabled"] = bool(capability_package_is_active(current) and activation_approved)
    restored["rollback"] = {}
    state = _dict(current.get("state"))
    state.update(
        {
            "install_state": "installed",
            "activation_state": "active" if restored["enabled"] else "inactive",
            "update_state": "current",
        }
    )
    restored["state"] = state
    return restored


def rollback_update_available(current_package: dict[str, Any]) -> bool:
    """Return whether the current package has a rollback transition to consume."""

    current = current_package if isinstance(current_package, dict) else {}
    state = _dict(current.get("state"))
    if str(state.get("update_state") or "").strip() != "rollback_available":
        return False
    rollback = _dict(current.get("rollback"))
    if not rollback:
        return False
    rollback_snapshot = normalize_source_snapshot(
        _dict(rollback.get("source_snapshot"))
    )
    restored_manifest = _dict(rollback.get("manifest"))
    return bool(rollback_snapshot or restored_manifest)


def _items_by_id(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("component_id") or "").strip()
        if item_id:
            result[item_id] = dict(item)
    return result


def _stable_item(value: dict[str, Any]) -> dict[str, Any]:
    stable = _stable_manifest_value(value)
    return stable if isinstance(stable, dict) else {}


def _changed_manifest_sections(
    current: dict[str, Any],
    next_value: dict[str, Any],
) -> list[str]:
    return [
        section
        for section in _MANIFEST_DIFF_SECTIONS
        if _stable_manifest_section(section, current.get(section))
        != _stable_manifest_section(section, next_value.get(section))
    ]


def _stable_manifest_section(section: str, value: Any) -> Any:
    if value is None:
        value = [] if section in _LIST_MANIFEST_DIFF_SECTIONS else {}
    return _stable_manifest_value(value)


def _stable_manifest_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_manifest_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_MANIFEST_FIELDS
        }
    if isinstance(value, list):
        stable_items = [_stable_manifest_value(item) for item in value]
        return sorted(stable_items, key=_stable_json)
    return value


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _list_delta_count(before: Any, after: Any) -> int:
    before_count = len(before) if isinstance(before, list) else 0
    after_count = len(after) if isinstance(after, list) else 0
    return after_count - before_count


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_dict(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        item = value.get(key)
        if isinstance(item, dict):
            return dict(item)
    return {}


def _manifest_component_ids(manifest: dict[str, Any]) -> list[str]:
    return list(_items_by_id(manifest.get("components")).keys())
