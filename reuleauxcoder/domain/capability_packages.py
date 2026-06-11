"""Capability package domain state and projection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INSTALL_STATES = {
    "not_installed",
    "registered",
    "materialized",
    "installed",
    "blocked",
    "failed",
}

ACTIVATION_STATES = {"inactive", "active", "degraded", "blocked"}

RUNTIME_STATES = {
    "not_applicable",
    "stopped",
    "starting",
    "running",
    "connected",
    "failed",
}

CHECK_STATES = {"unknown", "pending", "passed", "missing", "failed", "stale"}

CREDENTIAL_STATES = {"not_required", "missing", "bound", "verified", "failed"}

UPDATE_STATES = {
    "not_checked",
    "current",
    "update_available",
    "candidate_ready",
    "updating",
    "rollback_available",
    "failed",
}

MAPPING_STATES = {"mapped", "unmapped", "mapping_required", "invalid"}

CREDENTIAL_BINDING_SCOPES = {"user", "workspace", "server_global"}
CREDENTIAL_ACTORS = {"user_delegated", "service_account"}
CREDENTIAL_SECRET_FIELD_NAMES = {
    "api_key",
    "password",
    "private_key",
    "private_key_pem",
    "secret",
    "secret_value",
    "token",
    "value",
}


def capability_package_state_projection(raw: dict[str, Any]) -> dict[str, str]:
    """Project legacy package config fields into the new multi-axis state view."""

    status = str(raw.get("status") or "").strip().lower()
    install_state = _legacy_install_state(status)
    activation_state = _legacy_activation_state(
        enabled=raw.get("enabled") is not False,
        install_state=install_state,
    )
    projection = {
        "install_state": install_state,
        "activation_state": activation_state,
        "runtime_state": _legacy_runtime_state(install_state),
        "check_state": _legacy_check_state(install_state),
        "credential_state": "not_required",
        "update_state": "not_checked",
        "mapping_state": "mapped",
    }
    explicit_state = raw.get("state")
    if isinstance(explicit_state, dict):
        _merge_state_axis(projection, explicit_state, "install_state", INSTALL_STATES)
        _merge_state_axis(projection, explicit_state, "activation_state", ACTIVATION_STATES)
        _merge_state_axis(projection, explicit_state, "runtime_state", RUNTIME_STATES)
        _merge_state_axis(projection, explicit_state, "check_state", CHECK_STATES)
        _merge_state_axis(projection, explicit_state, "credential_state", CREDENTIAL_STATES)
        _merge_state_axis(projection, explicit_state, "update_state", UPDATE_STATES)
        _merge_state_axis(projection, explicit_state, "mapping_state", MAPPING_STATES)
    return projection


def capability_package_is_active(raw: dict[str, Any] | None) -> bool:
    """Return whether a package should contribute active child resources."""

    if not isinstance(raw, dict):
        return False
    return capability_package_state_projection(raw).get("activation_state") == "active"


def package_managed_component_enabled(
    *,
    package_ids: list[str],
    packages: dict[str, Any],
    default: bool = False,
) -> bool:
    """Project child-resource availability from active package owners."""

    owner_ids = [str(item).strip() for item in package_ids if str(item).strip()]
    if not owner_ids:
        return bool(default)
    for package_id in owner_ids:
        raw_package = packages.get(package_id) if isinstance(packages, dict) else None
        if isinstance(raw_package, dict) and capability_package_is_active(raw_package):
            return True
    return False


def _merge_state_axis(
    projection: dict[str, str],
    explicit_state: dict[str, Any],
    key: str,
    allowed_values: set[str],
) -> None:
    value = str(explicit_state.get(key) or "").strip()
    if value in allowed_values:
        projection[key] = value


def _legacy_install_state(status: str) -> str:
    if status in INSTALL_STATES:
        return status
    if status in {"available", "ready"}:
        return "installed"
    if status in {"installing", "pending", "pending_install"}:
        return "registered"
    if status in {"error"}:
        return "failed"
    if status in {"removed", "deleted"}:
        return "not_installed"
    return "registered" if not status else "installed"


def _legacy_activation_state(*, enabled: bool, install_state: str) -> str:
    if install_state in {"blocked", "failed"}:
        return "blocked"
    if install_state != "installed":
        return "inactive"
    return "active" if enabled else "inactive"


def _legacy_runtime_state(install_state: str) -> str:
    if install_state == "failed":
        return "failed"
    return "not_applicable"


def _legacy_check_state(install_state: str) -> str:
    if install_state == "installed":
        return "unknown"
    if install_state == "failed":
        return "failed"
    if install_state == "blocked":
        return "failed"
    return "unknown"


@dataclass(frozen=True)
class CapabilitySourceSnapshot:
    """Immutable pointer to a complete upstream source snapshot."""

    package_id: str
    source_type: str
    source_url: str
    source_ref: str
    commit_sha: str
    upstream_version: str
    snapshot_id: str
    snapshot_path: str
    created_at: str = ""
    content_hash: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def display_version(self) -> str:
        if self.upstream_version:
            return self.upstream_version
        if self.source_ref and self.commit_sha:
            return f"{self.source_ref}@{self.commit_sha[:7]}"
        if self.commit_sha:
            return self.commit_sha[:7]
        return "unversioned"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilitySourceSnapshot":
        return cls(
            package_id=str(value.get("package_id") or value.get("package") or ""),
            source_type=str(value.get("source_type") or value.get("type") or ""),
            source_url=str(
                value.get("source_url")
                or value.get("url")
                or value.get("repo_url")
                or value.get("repository_url")
                or ""
            ),
            source_ref=str(
                value.get("source_ref") or value.get("ref") or value.get("branch") or ""
            ),
            commit_sha=str(
                value.get("commit_sha") or value.get("commit") or value.get("sha") or ""
            ),
            upstream_version=str(
                value.get("upstream_version")
                or value.get("release")
                or value.get("tag")
                or value.get("version")
                or ""
            ),
            snapshot_id=str(
                value.get("snapshot_id")
                or value.get("source_snapshot_id")
                or value.get("id")
                or ""
            ),
            snapshot_path=str(value.get("snapshot_path") or value.get("path") or ""),
            created_at=str(value.get("created_at") or ""),
            content_hash=str(value.get("content_hash") or value.get("hash") or ""),
            provenance=dict(value.get("provenance") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "package_id": self.package_id,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "source_ref": self.source_ref,
            "commit_sha": self.commit_sha,
            "upstream_version": self.upstream_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_path": self.snapshot_path,
            "created_at": self.created_at,
            "display_version": self.display_version,
        }
        if self.content_hash:
            result["content_hash"] = self.content_hash
        if self.provenance:
            result["provenance"] = dict(self.provenance)
        return result


@dataclass(frozen=True)
class CapabilityManifest:
    """Backend-owned normalized manifest for a capability package snapshot."""

    package: dict[str, Any] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)
    dependency_edges: list[dict[str, Any]] = field(default_factory=list)
    environment_requirements: list[dict[str, Any]] = field(default_factory=list)
    credential_requirements: list[dict[str, Any]] = field(default_factory=list)
    install_plans: list[dict[str, Any]] = field(default_factory=list)
    activation_rules: dict[str, Any] = field(default_factory=dict)
    update_metadata: dict[str, Any] = field(default_factory=dict)
    unmapped_findings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    exposed_file_closures: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityManifest":
        return cls(
            package=dict(value.get("package") or {}),
            components=_dict_items(value.get("components")),
            dependency_edges=_dict_items(value.get("dependency_edges")),
            environment_requirements=_dict_items(
                value.get("environment_requirements")
            ),
            credential_requirements=_dict_items(value.get("credential_requirements")),
            install_plans=_dict_items(value.get("install_plans")),
            activation_rules=dict(value.get("activation_rules") or {}),
            update_metadata=dict(value.get("update_metadata") or {}),
            unmapped_findings=_finding_items(value.get("unmapped_findings")),
            exposed_file_closures=_dict_items(value.get("exposed_file_closures")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": dict(self.package),
            "components": [dict(item) for item in self.components],
            "dependency_edges": [dict(item) for item in self.dependency_edges],
            "environment_requirements": [
                dict(item) for item in self.environment_requirements
            ],
            "credential_requirements": [
                dict(item) for item in self.credential_requirements
            ],
            "install_plans": [dict(item) for item in self.install_plans],
            "activation_rules": dict(self.activation_rules),
            "update_metadata": dict(self.update_metadata),
            "unmapped_findings": {
                key: [dict(item) for item in items]
                for key, items in self.unmapped_findings.items()
            },
            "exposed_file_closures": [
                dict(item) for item in self.exposed_file_closures
            ],
        }


@dataclass(frozen=True)
class SharedCapabilityRegistryEntry:
    """Backend-owned policy for a shared system capability."""

    id: str
    requirement_id: str
    kind: str
    name: str
    version_check_action: dict[str, Any] = field(default_factory=dict)
    install_action_policy: dict[str, Any] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=list)
    credential_interaction: str = "not_required"
    conflict_policy: str = "shared_singleton"
    evidence_required: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SharedCapabilityRegistryEntry":
        return cls(
            id=str(value.get("id") or ""),
            requirement_id=str(value.get("requirement_id") or ""),
            kind=str(value.get("kind") or ""),
            name=str(value.get("name") or ""),
            version_check_action=dict(value.get("version_check_action") or {}),
            install_action_policy=dict(value.get("install_action_policy") or {}),
            platforms=_string_items(value.get("platforms")),
            credential_interaction=str(
                value.get("credential_interaction") or "not_required"
            ),
            conflict_policy=str(value.get("conflict_policy") or "shared_singleton"),
            evidence_required=_string_items(value.get("evidence_required")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "name": self.name,
            "version_check_action": dict(self.version_check_action),
            "install_action_policy": dict(self.install_action_policy),
            "platforms": list(self.platforms),
            "credential_interaction": self.credential_interaction,
            "conflict_policy": self.conflict_policy,
            "evidence_required": list(self.evidence_required),
        }


@dataclass(frozen=True)
class CapabilityCredentialRequirement:
    """Credential requirement declared by a package, without credential values."""

    id: str
    provider: str
    kind: str
    placement: str = "server"
    allowed_scopes: list[str] = field(default_factory=list)
    required_by: list[str] = field(default_factory=list)
    credential_actor: str = "user_delegated"
    display_name: str = ""
    description: str = ""
    allow_global: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityCredentialRequirement":
        _reject_plaintext_secret_fields(value)
        actor = str(value.get("credential_actor") or "user_delegated").strip()
        if actor not in CREDENTIAL_ACTORS:
            actor = "user_delegated"
        return cls(
            id=str(value.get("id") or value.get("requirement_id") or "").strip(),
            provider=str(value.get("provider") or "").strip(),
            kind=str(value.get("kind") or "").strip(),
            placement=_credential_placement(value.get("placement", "server")),
            allowed_scopes=_string_items(value.get("allowed_scopes")),
            required_by=_string_items(value.get("required_by")),
            credential_actor=actor,
            display_name=str(value.get("display_name") or value.get("displayName") or "").strip(),
            description=str(value.get("description") or "").strip(),
            allow_global=value.get("allow_global") is not False,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "provider": self.provider,
            "kind": self.kind,
            "placement": self.placement,
            "allowed_scopes": list(self.allowed_scopes),
            "required_by": list(self.required_by),
            "credential_actor": self.credential_actor,
            "allow_global": self.allow_global,
        }
        if self.display_name:
            result["display_name"] = self.display_name
        if self.description:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class CapabilityCredentialBinding:
    """Reference to an externally stored credential secret."""

    requirement_id: str
    scope: str
    secret_ref_id: str
    user_id: str = ""
    workspace_id: str = ""
    credential_actor: str = "user_delegated"
    admin_authorized: bool = False
    authorized_by: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityCredentialBinding":
        _reject_plaintext_secret_fields(value)
        scope = str(value.get("scope") or "").strip()
        if scope not in CREDENTIAL_BINDING_SCOPES:
            raise ValueError(f"unknown credential binding scope: {scope or '<missing>'}")
        actor = str(value.get("credential_actor") or "user_delegated").strip()
        if actor not in CREDENTIAL_ACTORS:
            actor = "user_delegated"
        return cls(
            requirement_id=str(
                value.get("requirement_id") or value.get("id") or ""
            ).strip(),
            scope=scope,
            secret_ref_id=str(
                value.get("secret_ref_id")
                or value.get("secret_ref")
                or value.get("credential_ref_id")
                or ""
            ).strip(),
            user_id=str(value.get("user_id") or "").strip(),
            workspace_id=str(value.get("workspace_id") or "").strip(),
            credential_actor=actor,
            admin_authorized=value.get("admin_authorized") is True,
            authorized_by=str(value.get("authorized_by") or "").strip(),
            updated_at=str(value.get("updated_at") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "requirement_id": self.requirement_id,
            "scope": self.scope,
            "secret_ref_id": self.secret_ref_id,
            "credential_actor": self.credential_actor,
            "admin_authorized": self.admin_authorized,
        }
        if self.user_id:
            result["user_id"] = self.user_id
        if self.workspace_id:
            result["workspace_id"] = self.workspace_id
        if self.authorized_by:
            result["authorized_by"] = self.authorized_by
        if self.updated_at:
            result["updated_at"] = self.updated_at
        return result


@dataclass(frozen=True)
class CapabilityDependencyGraph:
    """Validated dependency graph for a normalized capability manifest."""

    requirements: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    blocked_component_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityDependencyGraph":
        requirements = value.get("requirements")
        return cls(
            requirements={
                str(key): dict(item)
                for key, item in requirements.items()
                if isinstance(item, dict)
            }
            if isinstance(requirements, dict)
            else {},
            edges=_dict_items(value.get("edges")),
            blocked_component_ids=_string_items(value.get("blocked_component_ids")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": {
                key: dict(value) for key, value in self.requirements.items()
            },
            "edges": [dict(item) for item in self.edges],
            "blocked_component_ids": list(self.blocked_component_ids),
        }


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _finding_items(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for key, items in value.items():
        if isinstance(items, list):
            result[str(key)] = _dict_items(items)
    return result


def _credential_placement(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"server", "local_peer", "both"}:
        return text
    if text == "peer":
        return "local_peer"
    return "server"


def _reject_plaintext_secret_fields(value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        key_text = str(key).strip().lower()
        if key_text in CREDENTIAL_SECRET_FIELD_NAMES:
            raise ValueError(
                "secret values must not enter capability package payloads"
            )
        if isinstance(item, dict):
            _reject_plaintext_secret_fields(item)
        elif isinstance(item, list):
            for child in item:
                _reject_plaintext_secret_fields(child)
