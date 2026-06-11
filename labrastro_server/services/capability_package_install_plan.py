"""Typed install plan models for capability package execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INSTALL_ACTION_TYPES = {
    "check_executable",
    "install_python_packages",
    "install_node_packages",
    "materialize_skill_files",
    "register_mcp_server",
    "bind_credential_requirement",
    "run_version_check",
}

INSTALL_ACTION_TARGETS = {"server", "local_peer", "both"}


def normalize_install_action_id(value: dict[str, Any]) -> str:
    """Return the canonical action id from supported wire aliases."""

    return str(value.get("id") or value.get("action_id") or "").strip()


@dataclass(frozen=True)
class InstallAction:
    id: str
    type: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    component_id: str = ""
    requirement_id: str = ""
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstallAction":
        action_type = str(value.get("type") or "").strip()
        if action_type not in INSTALL_ACTION_TYPES:
            raise ValueError(f"unknown install action type: {action_type or '<missing>'}")
        target = str(value.get("target") or "").strip()
        if target not in INSTALL_ACTION_TARGETS:
            raise ValueError(f"unknown install action target: {target or '<missing>'}")
        action_id = normalize_install_action_id(value)
        if not action_id:
            raise ValueError("install action id is required")
        raw_params = value.get("params")
        raw_depends_on = value.get("depends_on")
        return cls(
            id=action_id,
            type=action_type,
            target=target,
            params=dict(raw_params) if isinstance(raw_params, dict) else {},
            component_id=str(value.get("component_id") or "").strip(),
            requirement_id=str(value.get("requirement_id") or "").strip(),
            depends_on=[
                str(item)
                for item in raw_depends_on
                if str(item).strip()
            ]
            if isinstance(raw_depends_on, list)
            else [],
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "target": self.target,
            "params": dict(self.params),
        }
        if self.component_id:
            result["component_id"] = self.component_id
        if self.requirement_id:
            result["requirement_id"] = self.requirement_id
        if self.depends_on:
            result["depends_on"] = list(self.depends_on)
        return result


@dataclass(frozen=True)
class InstallPlan:
    package_id: str
    actions: list[InstallAction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstallPlan":
        raw_actions = value.get("actions")
        return cls(
            package_id=str(value.get("package_id") or "").strip(),
            actions=[
                InstallAction.from_dict(item)
                for item in raw_actions
                if isinstance(item, dict)
            ]
            if isinstance(raw_actions, list)
            else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class InstallActionResult:
    action_id: str
    action_type: str
    target: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    target_facts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "target": self.target,
            "status": self.status,
            "details": dict(self.details),
            "target_facts": {
                key: dict(value) for key, value in self.target_facts.items()
            },
        }


__all__ = [
    "INSTALL_ACTION_TARGETS",
    "INSTALL_ACTION_TYPES",
    "InstallAction",
    "InstallActionResult",
    "InstallPlan",
    "normalize_install_action_id",
]
