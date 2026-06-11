"""Normalize LLM advisory capability package candidates into backend manifests."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.agent_runtime.models import CAPABILITY_COMPONENT_KINDS
from reuleauxcoder.domain.capability_packages import CapabilityManifest
from reuleauxcoder.domain.environment_requirements import ENVIRONMENT_REQUIREMENT_KINDS


def normalize_capability_manifest_candidate(value: dict[str, Any]) -> CapabilityManifest:
    """Convert advisory LLM output into a backend-owned manifest skeleton."""

    components: list[dict[str, Any]] = []
    environment_requirements: list[dict[str, Any]] = []
    unmapped: dict[str, list[dict[str, Any]]] = {
        "unclassified_requirements": [],
        "unsupported_component_candidates": [],
    }
    for item in _dict_items(value.get("components")):
        normalized = _normalize_component_candidate(item)
        if normalized is None:
            finding = _unmapped_finding(item)
            _finding_bucket(unmapped, finding).append(finding)
            continue
        components.append(normalized)
        if normalized.get("kind") == "environment_requirement":
            environment_requirements.append(dict(normalized))

    return CapabilityManifest.from_dict(
        {
            "package": dict(value.get("package") or {}),
            "components": components,
            "environment_requirements": environment_requirements,
            "dependency_edges": _dict_items(value.get("dependency_edges")),
            "credential_requirements": _dict_items(value.get("credential_requirements")),
            "install_plans": _dict_items(value.get("install_plans")),
            "activation_rules": dict(value.get("activation_rules") or {}),
            "update_metadata": dict(value.get("update_metadata") or {}),
            "unmapped_findings": {
                key: items for key, items in unmapped.items() if items
            },
            "exposed_file_closures": _dict_items(value.get("exposed_file_closures")),
        }
    )


def _normalize_component_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(item.get("kind") or item.get("type") or "").strip().lower()
    component_id = str(item.get("id") or "").strip()
    if kind in ENVIRONMENT_REQUIREMENT_KINDS:
        kind = "environment_requirement"
    if kind not in CAPABILITY_COMPONENT_KINDS:
        return None
    if not _component_id_prefix_matches_kind(component_id, kind):
        return None
    if component_id.startswith("envreq:") and _envreq_id_kind(component_id) not in (
        "" if kind != "environment_requirement" else "environment_requirement",
        *ENVIRONMENT_REQUIREMENT_KINDS,
    ):
        return None
    if kind == "environment_requirement" and component_id.startswith("envreq:"):
        env_kind = _envreq_id_kind(component_id)
        if env_kind not in ENVIRONMENT_REQUIREMENT_KINDS:
            return None

    result = dict(item)
    result["kind"] = kind
    return result


def _component_id_prefix_matches_kind(component_id: str, kind: str) -> bool:
    if not component_id or ":" not in component_id:
        return True
    if component_id.startswith("envreq:"):
        return kind == "environment_requirement"
    if component_id.startswith("skill:"):
        return kind == "skill"
    if component_id.startswith("mcp_server:") or component_id.startswith("mcp:"):
        return kind in {"mcp", "mcp_server"}
    if component_id.startswith("credreq:"):
        return kind in {"credential", "credential_requirement"}
    return True


def _envreq_id_kind(component_id: str) -> str:
    if not component_id.startswith("envreq:"):
        return ""
    parts = component_id.split(":", 2)
    return parts[1] if len(parts) > 1 else ""


def _unmapped_finding(item: dict[str, Any]) -> dict[str, Any]:
    finding: dict[str, Any] = {}
    component_id = str(item.get("id") or "").strip()
    if component_id:
        finding["id"] = component_id
    observed = _observed_requirement(item)
    if observed:
        finding["observed"] = observed
    suggested_kind = str(item.get("kind") or item.get("type") or "").strip()
    if suggested_kind:
        finding["suggested_kind"] = suggested_kind
    for key in ("source_path", "source_document_id", "evidence"):
        if item.get(key):
            finding[key] = item[key]
    finding["mapping_state"] = "mapping_required"
    return finding


def _finding_bucket(
    unmapped: dict[str, list[dict[str, Any]]],
    finding: dict[str, Any],
) -> list[dict[str, Any]]:
    if finding.get("observed") or str(finding.get("id") or "").startswith("envreq:"):
        return unmapped["unclassified_requirements"]
    return unmapped["unsupported_component_candidates"]


def _observed_requirement(item: dict[str, Any]) -> str:
    for key in ("install", "check", "configure", "command"):
        text = str(item.get(key) or "").strip()
        if text:
            return text
    return ""


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
