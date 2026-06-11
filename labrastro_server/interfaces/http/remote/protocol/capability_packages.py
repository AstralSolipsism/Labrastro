"""Capability package peer install protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CapabilityPackageInstallResultRecord:
    plan_id: str
    action_id: str
    package_id: str
    component_id: str
    target: str
    status: str
    version: str = ""
    content_hash: str = ""
    message: str = ""
    timestamp: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityPackageInstallResultRecord":
        if not isinstance(value, dict):
            raise ValueError("install result must be an object")
        plan_id = str(value.get("plan_id") or "").strip()
        action_id = str(value.get("action_id") or value.get("id") or "").strip()
        package_id = str(value.get("package_id") or "").strip()
        target = str(value.get("target") or "").strip()
        status = str(value.get("status") or "").strip()
        if not plan_id:
            raise ValueError("plan_id is required")
        if not action_id:
            raise ValueError("action_id is required")
        if not package_id:
            raise ValueError("package_id is required")
        if target != "local_peer":
            raise ValueError("capability package peer install results must target local_peer")
        if not status:
            raise ValueError("status is required")
        details = value.get("details")
        return cls(
            plan_id=plan_id,
            action_id=action_id,
            package_id=package_id,
            component_id=str(value.get("component_id") or "").strip(),
            target=target,
            status=status,
            version=str(value.get("version") or "").strip(),
            content_hash=str(value.get("content_hash") or "").strip(),
            message=str(value.get("message") or "").strip(),
            timestamp=str(value.get("timestamp") or "").strip(),
            details=dict(details) if isinstance(details, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "package_id": self.package_id,
            "component_id": self.component_id,
            "target": self.target,
            "status": self.status,
            "version": self.version,
            "content_hash": self.content_hash,
            "message": self.message,
            "timestamp": self.timestamp,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


__all__ = ["CapabilityPackageInstallResultRecord"]
