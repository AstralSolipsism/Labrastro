"""Canonical capability install candidate snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.agent_runtime.models import CapabilityComponentConfig


CAPABILITY_INSTALL_CANDIDATE_STORE_KEY = "capability_install_candidates"
CAPABILITY_INSTALL_CANDIDATE_OPERATIONS = {"install", "update", "rollback"}
CAPABILITY_INSTALL_CANDIDATE_STATUSES = {
    "drafting",
    "ready",
    "superseded",
    "approved",
    "applied",
    "rejected",
    "blocked",
    "failed",
    "needs_attention",
}
EXECUTABLE_CANDIDATE_STATUSES = {"approved"}
APPROVABLE_CANDIDATE_STATUSES = {"ready"}


@dataclass(slots=True)
class CapabilityInstallCandidate:
    operation: str
    package_id: str
    components: list[dict[str, Any]]
    candidate_id: str = ""
    candidate_hash: str = ""
    status: str = "ready"
    display_name: str = ""
    description: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    install_plan: list[str] = field(default_factory=list)
    usage: list[str] = field(default_factory=list)
    effective_capabilities: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    credential_requirements: list[dict[str, Any]] = field(default_factory=list)
    credential_bindings: list[dict[str, Any]] = field(default_factory=list)
    risk_level: str = ""
    hooks: list[dict[str, Any]] = field(default_factory=list)
    runtime_footprint: dict[str, Any] = field(default_factory=dict)
    post_install_checks: list[dict[str, Any]] = field(default_factory=list)
    package_patch: dict[str, Any] = field(default_factory=dict)
    evidence_map: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    source_bundle: dict[str, Any] = field(default_factory=dict)
    agent_run_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CapabilityInstallCandidate":
        raw = data if isinstance(data, dict) else {}
        return cls(
            operation=_candidate_operation(raw.get("operation")),
            package_id=str(raw.get("package_id") or raw.get("id") or "").strip(),
            components=_dict_list(raw.get("components")),
            candidate_id=str(raw.get("candidate_id") or "").strip(),
            candidate_hash=str(raw.get("candidate_hash") or "").strip(),
            status=_candidate_status(raw.get("status")),
            display_name=str(raw.get("display_name") or raw.get("name") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            source=_dict_value(raw.get("source")),
            review=_dict_value(raw.get("review")),
            install_plan=_string_list(raw.get("install_plan")),
            usage=_string_list(raw.get("usage")),
            effective_capabilities=_string_list(raw.get("effective_capabilities")),
            evidence=_dict_list(raw.get("evidence")),
            credentials=_string_list(raw.get("credentials")),
            credential_requirements=_dict_list(raw.get("credential_requirements")),
            credential_bindings=_dict_list(raw.get("credential_bindings")),
            risk_level=str(raw.get("risk_level") or "").strip().lower(),
            hooks=_dict_list(raw.get("hooks")),
            runtime_footprint=_dict_value(raw.get("runtime_footprint")),
            post_install_checks=_dict_list(raw.get("post_install_checks")),
            package_patch=_dict_value(raw.get("package_patch")),
            evidence_map=_dict_value(raw.get("evidence_map")),
            diagnostics=_dict_value(raw.get("diagnostics")),
            source_bundle=_dict_value(raw.get("source_bundle")),
            agent_run_id=str(raw.get("agent_run_id") or "").strip(),
        )

    def to_dict(
        self,
        *,
        include_hash: bool = True,
        include_status: bool = True,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "operation": _candidate_operation(self.operation),
            "package_id": self.package_id,
            "candidate_id": self.candidate_id,
            "display_name": self.display_name,
            "description": self.description,
            "source": dict(self.source),
            "components": [dict(item) for item in self.components],
            "review": dict(self.review),
            "install_plan": list(self.install_plan),
            "usage": list(self.usage),
            "effective_capabilities": list(self.effective_capabilities),
            "evidence": [dict(item) for item in self.evidence],
            "credentials": list(self.credentials),
            "credential_requirements": [
                dict(item) for item in self.credential_requirements
            ],
            "credential_bindings": [dict(item) for item in self.credential_bindings],
            "risk_level": self.risk_level,
            "hooks": [dict(item) for item in self.hooks],
            "runtime_footprint": dict(self.runtime_footprint),
            "post_install_checks": [dict(item) for item in self.post_install_checks],
            "package_patch": dict(self.package_patch),
            "evidence_map": dict(self.evidence_map),
            "diagnostics": dict(self.diagnostics),
            "source_bundle": dict(self.source_bundle),
            "agent_run_id": self.agent_run_id,
        }
        if include_status:
            result["status"] = _candidate_status(self.status)
        if include_hash:
            result["candidate_hash"] = self.candidate_hash
        return _drop_empty(result)


@dataclass(frozen=True, slots=True)
class CapabilityInstallCandidateValidationResult:
    ok: bool
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "messages": list(self.messages)}


@dataclass(frozen=True, slots=True)
class CapabilityInstallCandidateBuildResult:
    status: str
    candidate: CapabilityInstallCandidate | None = None
    messages: list[str] = field(default_factory=list)
    reason: str = ""
    user_choice: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "messages": list(self.messages),
        }
        if self.reason:
            result["reason"] = self.reason
        if self.candidate is not None:
            result["candidate"] = self.candidate.to_dict()
        if self.user_choice:
            result["user_choice"] = dict(self.user_choice)
        return result


class CapabilityInstallCandidateValidator:
    def validate(
        self,
        candidate: CapabilityInstallCandidate,
        *,
        existing_mcp_servers: set[str] | None = None,
    ) -> CapabilityInstallCandidateValidationResult:
        messages: list[str] = []
        if candidate.operation not in CAPABILITY_INSTALL_CANDIDATE_OPERATIONS:
            messages.append("candidate.operation is invalid")
        if candidate.status not in CAPABILITY_INSTALL_CANDIDATE_STATUSES:
            messages.append("candidate.status is invalid")
        if not candidate.package_id:
            messages.append("candidate.package_id is required")
        if not candidate.components:
            messages.append("candidate.components must contain at least one component")
        existing_servers = set(existing_mcp_servers or set())
        candidate_servers = {
            str(item.get("name") or "").strip()
            for item in candidate.components
            if str(item.get("kind") or item.get("type") or "").strip() in {"mcp", "mcp_server"}
        }
        known_servers = {item for item in [*existing_servers, *candidate_servers] if item}
        component_ids: set[str] = set()
        for item in candidate.components:
            component_id = str(item.get("id") or "").strip()
            kind = str(item.get("kind") or item.get("type") or "").strip()
            name = str(item.get("name") or "").strip()
            if not component_id:
                messages.append("candidate component id is required")
            elif component_id in component_ids:
                messages.append(f"candidate component id is duplicated: {component_id}")
            component_ids.add(component_id)
            if not kind:
                messages.append(f"candidate component kind is required: {component_id or name}")
            if not name:
                messages.append(f"candidate component name is required: {component_id or kind}")
            if kind == "mcp_tool":
                registry_path = str(item.get("registry_path") or "").strip()
                server, tool = _parse_mcp_tool_registry_path(registry_path)
                if not server or not tool:
                    messages.append(
                        f"mcp_tool component must use registry_path mcp:<server>:<tool>: {component_id or name}"
                    )
                elif server not in known_servers:
                    messages.append(
                        f"mcp_tool component references unknown mcp server '{server}': {component_id or name}"
                    )
                if registry_path and component_id and component_id != registry_path:
                    messages.append(
                        f"mcp_tool component id must match registry_path: {component_id}"
                    )
        return CapabilityInstallCandidateValidationResult(
            ok=not messages,
            messages=_unique_strings(messages),
        )


def build_install_candidate(
    *,
    operation: str,
    package_id: str,
    display_name: str,
    description: str,
    source: dict[str, Any],
    components: list[CapabilityComponentConfig],
    install_plan: list[str] | None = None,
    usage: list[str] | None = None,
    effective_capabilities: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    credentials: list[str] | None = None,
    credential_requirements: list[dict[str, Any]] | None = None,
    credential_bindings: list[dict[str, Any]] | None = None,
    risk_level: str = "",
    hooks: list[dict[str, Any]] | None = None,
    runtime_footprint: dict[str, Any] | None = None,
    post_install_checks: list[dict[str, Any]] | None = None,
    package_patch: dict[str, Any] | None = None,
    evidence_map: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    source_bundle: dict[str, Any] | None = None,
    agent_run_id: str = "",
    existing_mcp_servers: set[str] | None = None,
) -> CapabilityInstallCandidateBuildResult:
    component_payloads = [_component_to_candidate_dict(component) for component in components]
    review = capability_install_candidate_review(
        package_id=package_id,
        display_name=display_name,
        description=description,
        components=component_payloads,
        runtime_footprint=runtime_footprint or {},
        install_plan=install_plan or [],
        usage=usage or [],
        evidence=evidence or [],
        credentials=credentials or [],
        risk_level=risk_level,
        post_install_checks=post_install_checks or [],
    )
    candidate = CapabilityInstallCandidate(
        operation=_candidate_operation(operation),
        package_id=str(package_id).strip(),
        display_name=str(display_name or package_id).strip(),
        description=str(description or "").strip(),
        source=dict(source or {}),
        components=component_payloads,
        review=review,
        install_plan=_string_list(install_plan),
        usage=_string_list(usage),
        effective_capabilities=_string_list(effective_capabilities),
        evidence=[dict(item) for item in evidence or [] if isinstance(item, dict)],
        credentials=_string_list(credentials),
        credential_requirements=_dict_list(credential_requirements),
        credential_bindings=_dict_list(credential_bindings),
        risk_level=str(risk_level or "").strip().lower(),
        hooks=_dict_list(hooks),
        runtime_footprint=dict(runtime_footprint or {}),
        post_install_checks=_dict_list(post_install_checks),
        package_patch=dict(package_patch or {}),
        evidence_map=dict(evidence_map or {}),
        diagnostics=dict(diagnostics or {}),
        source_bundle=dict(source_bundle or {}),
        agent_run_id=str(agent_run_id or "").strip(),
    )
    candidate = with_candidate_identity(candidate)
    validation = CapabilityInstallCandidateValidator().validate(
        candidate,
        existing_mcp_servers=existing_mcp_servers,
    )
    if not validation.ok:
        return CapabilityInstallCandidateBuildResult(
            status="blocked",
            candidate=None,
            messages=validation.messages,
            reason="candidate_validation_failed",
        )
    return CapabilityInstallCandidateBuildResult(status="ready", candidate=candidate)


def with_candidate_identity(
    candidate: CapabilityInstallCandidate,
) -> CapabilityInstallCandidate:
    payload = candidate.to_dict(include_hash=False, include_status=False)
    payload["candidate_id"] = ""
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    candidate.candidate_hash = digest
    if not candidate.candidate_id:
        candidate.candidate_id = (
            f"capability-candidate:{candidate.operation}:{candidate.package_id}:{digest[:16]}"
        )
    return candidate


def candidate_items(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.setdefault(CAPABILITY_INSTALL_CANDIDATE_STORE_KEY, {})
    if not isinstance(raw, dict):
        raw = {}
        data[CAPABILITY_INSTALL_CANDIDATE_STORE_KEY] = raw
    return raw


def save_candidate_snapshot(
    data: dict[str, Any],
    candidate: CapabilityInstallCandidate,
    *,
    status: str = "ready",
) -> CapabilityInstallCandidate:
    candidate.status = _candidate_status(status)
    candidate = with_candidate_identity(candidate)
    items = candidate_items(data)
    items[candidate.candidate_id] = candidate.to_dict()
    return candidate


def load_candidate_snapshot(
    data: dict[str, Any],
    candidate_id: str,
) -> CapabilityInstallCandidate | None:
    raw = candidate_items(data).get(str(candidate_id or "").strip())
    if not isinstance(raw, dict):
        return None
    return CapabilityInstallCandidate.from_dict(raw)


def mark_candidate_status(
    data: dict[str, Any],
    candidate_id: str,
    status: str,
    *,
    details: dict[str, Any] | None = None,
) -> CapabilityInstallCandidate | None:
    items = candidate_items(data)
    raw = items.get(str(candidate_id or "").strip())
    if not isinstance(raw, dict):
        return None
    candidate = CapabilityInstallCandidate.from_dict(raw)
    candidate.status = _candidate_status(status)
    payload = candidate.to_dict()
    if details:
        payload["status_details"] = dict(details)
    items[candidate.candidate_id] = payload
    return candidate


def verify_candidate_hash(candidate: CapabilityInstallCandidate, expected_hash: str) -> bool:
    actual = with_candidate_identity(CapabilityInstallCandidate.from_dict(candidate.to_dict())).candidate_hash
    return bool(expected_hash) and actual == str(expected_hash or "").strip()


def existing_mcp_server_names(data: dict[str, Any]) -> set[str]:
    mcp = data.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else {}
    return {
        str(name).strip()
        for name in (servers.keys() if isinstance(servers, dict) else [])
        if str(name).strip()
    }


def capability_install_candidate_review(
    *,
    package_id: str,
    display_name: str,
    description: str,
    components: list[dict[str, Any]],
    runtime_footprint: dict[str, Any],
    install_plan: list[str],
    usage: list[str],
    evidence: list[dict[str, Any]],
    credentials: list[str],
    risk_level: str,
    post_install_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "package_id": package_id,
        "name": display_name or package_id,
        "description": description,
        "components": [dict(item) for item in components],
        "capabilities": [
            _review_component(item)
            for item in components
            if str(item.get("kind") or "").strip() in {"skill", "mcp_tool", "builtin_tool", "prompt_fragment"}
        ],
        "dependencies": [
            _review_component(item)
            for item in components
            if str(item.get("kind") or "").strip()
            in {"mcp", "mcp_server", "environment_requirement", "credential"}
        ],
        "runtime_footprint": dict(runtime_footprint),
        "install_plan": list(install_plan),
        "usage": list(usage),
        "evidence": [dict(item) for item in evidence],
        "credentials": list(credentials),
        "risks": [item for item in [f"risk_level: {risk_level}" if risk_level else ""] if item],
        "post_install_checks": [dict(item) for item in post_install_checks],
    }


def _component_to_candidate_dict(component: CapabilityComponentConfig) -> dict[str, Any]:
    payload = component.to_dict()
    payload["id"] = component.id
    if component.kind == "mcp_tool":
        payload["registry_path"] = _normalize_mcp_tool_registry_path(component)
    return payload


def _normalize_mcp_tool_registry_path(component: CapabilityComponentConfig) -> str:
    registry_path = str(component.registry_path or "").strip()
    server, tool = _parse_mcp_tool_registry_path(registry_path)
    if server and tool:
        return registry_path
    config = dict(component.config or {})
    server = str(
        config.get("server_name")
        or config.get("mcp_server")
        or config.get("server")
        or ""
    ).strip()
    tool = str(config.get("tool_name") or component.name or "").strip()
    if server and tool:
        return f"mcp:{server}:{tool}"
    return registry_path


def _parse_mcp_tool_registry_path(value: str) -> tuple[str, str]:
    parts = str(value or "").strip().split(":", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return "", ""
    return parts[1].strip(), parts[2].strip()


def _review_component(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or "").strip(),
        "kind": str(item.get("kind") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "display_name": str(item.get("display_name") or item.get("name") or "").strip(),
        "summary": str(item.get("summary") or item.get("description") or "").strip(),
    }


def _candidate_operation(value: Any) -> str:
    return str(value or "").strip().lower()


def _candidate_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if item in ("", [], {}, None):
            continue
        result[key] = item
    return result


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
