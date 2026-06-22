from __future__ import annotations

from http import HTTPStatus
import uuid
from typing import Any

from labrastro_server.interfaces.http.remote.protocol import (
    CapabilityPackageInstallResultRecord,
)
from labrastro_server.interfaces.http.remote.protocol.local_actions import (
    LOCAL_ACTION_TERMINAL_STATUSES,
    LocalActionRecord,
)
from labrastro_server.services.capability_package_install_plan import InstallAction


_PEER_INSTALL_PASSED = {"ok", "passed", "success", "succeeded", "installed"}
_PEER_INSTALL_FAILED = {"blocked", "error", "failed", "missing"}
_PEER_INSTALL_PENDING = {"checking", "installing", "pending", "running"}


class RemoteCapabilityPackageRoutes:
    def _handle_capability_package_install_plan(self) -> None:
        payload = self._read_json()
        peer_id = self._verify_peer_token(payload.get("peer_token"))
        if peer_id is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        plan = _build_peer_install_plan(self.service.capability_packages)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "type": "capabilityPackage.installPlan",
                "plan": plan,
                "peer_status": self._capability_package_peer_status(peer_id, plan),
            },
        )

    def _sync_capability_package_local_actions(self, peer_id: str) -> None:
        local_action_service = getattr(self.service, "local_action_service", None)
        if local_action_service is None:
            return
        plan = _build_peer_install_plan(self.service.capability_packages)
        peer_status = self._capability_package_peer_status(peer_id, plan)
        for action in plan.get("actions", []):
            if not isinstance(action, dict):
                continue
            identity = _install_action_identity(action)
            action_status = _dict(peer_status.get("actions", {}).get(identity["key"]))
            if (
                action_status.get("check_state") == "passed"
                and action_status.get("install_state") == "installed"
            ):
                continue
            if _capability_package_has_active_local_action(
                local_action_service,
                peer_id=peer_id,
                action_key=identity["key"],
            ):
                continue
            action_kind = _first_action_string(action, "type", "action_kind")
            if not action_kind:
                continue
            local_action = LocalActionRecord.from_dict(
                {
                    "scope": "admin_task_scoped",
                    "admin_task_id": f"capability-package-install:{peer_id}:{identity['key']}",
                    "requested_by": "capability_package_install",
                    "peer_id": peer_id,
                    "local_action_id": f"capability-package-install:{uuid.uuid4().hex}",
                    "action_kind": action_kind,
                    "status": "waiting_peer",
                    "payload": {
                        "desired_action": dict(action),
                        "capability_package_install": dict(identity),
                    },
                }
            )
            local_action_service.create_local_action(local_action)

    def _record_capability_package_local_action_result(
        self,
        peer_id: str,
        action: Any,
    ) -> None:
        payload = _dict(getattr(action, "payload", {}))
        marker = _dict(payload.get("capability_package_install"))
        desired_action = _dict(payload.get("desired_action"))
        if not marker or not desired_action:
            return
        result_payload = _dict(getattr(action, "result", {}))
        result_payload.setdefault("plan_id", marker.get("plan_id", ""))
        result_payload.setdefault("action_id", marker.get("action_id", ""))
        result_payload.setdefault("package_id", marker.get("package_id", ""))
        result_payload.setdefault("component_id", marker.get("component_id", ""))
        result_payload.setdefault("target", "local_peer")
        action_status = str(getattr(action, "status", "") or "").strip()
        result_payload.setdefault(
            "status",
            "passed" if action_status == "completed" else "failed",
        )
        result_payload["local_action_id"] = getattr(action, "local_action_id", "")
        if getattr(action, "error", None):
            result_payload.setdefault("error", getattr(action, "error"))
        try:
            result = CapabilityPackageInstallResultRecord.from_dict(result_payload)
        except Exception:
            return
        result_record = result.to_dict()
        result_record["local_action_id"] = getattr(action, "local_action_id", "")
        with self.service._capability_package_peer_results_lock:
            peer_results = self.service._capability_package_peer_results.setdefault(
                peer_id, {}
            )
            peer_results[_peer_result_key(result_record)] = result_record

    def _capability_package_peer_status(
        self,
        peer_id: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        with self.service._capability_package_peer_results_lock:
            peer_results = dict(
                self.service._capability_package_peer_results.get(peer_id, {})
            )
        actions: dict[str, Any] = {}
        for action in plan.get("actions", []):
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("id") or action.get("action_id") or "").strip()
            if not action_id:
                continue
            action_key = _peer_action_key(action)
            peer_result = peer_results.get(action_key)
            actions[action_key] = _peer_action_status(action, peer_result)
        return {
            "type": "capabilityPackage.peerStatus",
            "peer_id": peer_id,
            "plan_id": str(plan.get("plan_id") or ""),
            "actions": actions,
        }


def _build_peer_install_plan(capability_packages: dict[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    plan_ids: list[str] = []
    for package_id, package in sorted((capability_packages or {}).items()):
        for plan in _package_install_plans(package):
            plan_id = str(plan.get("plan_id") or plan.get("id") or "").strip()
            if not plan_id:
                plan_id = f"{package_id}:local-peer"
            if plan_id not in plan_ids:
                plan_ids.append(plan_id)
            raw_actions = plan.get("actions")
            if not isinstance(raw_actions, list):
                continue
            for raw_action in raw_actions:
                action = _peer_install_action(
                    raw_action,
                    package_id=str(package_id),
                    plan_id=plan_id,
                )
                if action:
                    actions.append(action)
    return {
        "plan_id": plan_ids[0] if len(plan_ids) == 1 else "capability-package-peer-install",
        "plan_ids": plan_ids,
        "actions": actions,
    }


def _package_install_plans(package: Any) -> list[dict[str, Any]]:
    raw = _capability_value(package, "install_plans", [])
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    raw_manifest = _capability_value(package, "manifest", None)
    if isinstance(raw_manifest, dict):
        raw_manifest_plans = raw_manifest.get("install_plans")
        if isinstance(raw_manifest_plans, list):
            return [dict(item) for item in raw_manifest_plans if isinstance(item, dict)]
    return []


def _peer_install_action(
    raw_action: Any,
    *,
    package_id: str,
    plan_id: str,
) -> dict[str, Any] | None:
    if not isinstance(raw_action, dict):
        return None
    try:
        action_model = InstallAction.from_dict(raw_action)
    except ValueError:
        return None
    target = action_model.target
    if target not in {"local_peer", "both"}:
        return None
    action_id = action_model.id
    action = {**dict(raw_action), **action_model.to_dict()}
    if target == "both":
        action["original_target"] = "both"
    identity = _install_action_identity(
        action,
        fallback_package_id=package_id,
        fallback_plan_id=plan_id,
    )
    action["id"] = action_id
    action["action_id"] = action_id
    action["plan_id"] = identity["plan_id"]
    action["package_id"] = identity["package_id"]
    if identity["component_id"]:
        action["component_id"] = identity["component_id"]
    expected_hash = _expected_action_content_hash(action)
    if expected_hash:
        action["expected_content_hash"] = expected_hash
    action["target"] = "local_peer"
    return action


def _peer_action_status(
    action: dict[str, Any],
    peer_result: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = _install_action_identity(action)
    base = {
        "plan_id": identity["plan_id"],
        "action_id": identity["action_id"],
        "package_id": identity["package_id"],
        "component_id": identity["component_id"],
        "target": "local_peer",
        "desired_action": dict(action),
        "peer_result": dict(peer_result) if isinstance(peer_result, dict) else None,
    }
    if not isinstance(peer_result, dict):
        return {
            **base,
            "check_state": "pending",
            "install_state": "registered",
        }
    if _peer_result_is_stale(action, peer_result):
        return {
            **base,
            "check_state": "stale",
            "install_state": "registered",
        }
    status = str(peer_result.get("status") or "").strip().lower()
    if status in _PEER_INSTALL_PASSED:
        return {
            **base,
            "check_state": "passed",
            "install_state": "installed",
        }
    if status in _PEER_INSTALL_FAILED:
        return {
            **base,
            "check_state": "failed",
            "install_state": "failed",
        }
    if status in _PEER_INSTALL_PENDING:
        return {
            **base,
            "check_state": "pending",
            "install_state": "registered",
        }
    return {
        **base,
        "check_state": "unknown",
        "install_state": "registered",
    }


def _peer_result_is_stale(
    action: dict[str, Any],
    peer_result: dict[str, Any],
) -> bool:
    action_identity = _install_action_identity(action)
    result_identity = _install_action_identity(peer_result)
    if result_identity["plan_id"] != action_identity["plan_id"]:
        return True
    if str(peer_result.get("target") or "") != "local_peer":
        return True
    if result_identity["package_id"] != action_identity["package_id"]:
        return True
    if result_identity["component_id"] != action_identity["component_id"]:
        return True
    expected_hash = _expected_action_content_hash(action)
    if expected_hash and str(peer_result.get("content_hash") or "") != expected_hash:
        return True
    return False


def _peer_action_key(action: dict[str, Any]) -> str:
    return _install_action_identity(action)["key"]


def _peer_result_key(result: dict[str, Any]) -> str:
    return _install_action_identity(result)["key"]


def _capability_package_has_active_local_action(
    local_action_service: Any,
    *,
    peer_id: str,
    action_key: str,
) -> bool:
    list_actions = getattr(local_action_service, "list_local_actions", None)
    if not callable(list_actions):
        return False
    for action in list_actions():
        if str(getattr(action, "peer_id", "") or "") != peer_id:
            continue
        if getattr(action, "status", "") in LOCAL_ACTION_TERMINAL_STATUSES:
            continue
        payload = _dict(getattr(action, "payload", {}))
        marker = _dict(payload.get("capability_package_install"))
        if marker.get("key") == action_key:
            return True
    return False


def _expected_action_content_hash(action: dict[str, Any]) -> str:
    return _first_action_string(
        action,
        "expected_content_hash",
        "expectedContentHash",
        "content_hash",
        "contentHash",
        "lock_hash",
        "lockHash",
    )


def _install_action_identity(
    action: dict[str, Any],
    *,
    fallback_package_id: str = "",
    fallback_plan_id: str = "",
) -> dict[str, str]:
    plan_id = _first_action_string(
        action,
        "plan_id",
        "planId",
        fallback=fallback_plan_id,
    )
    action_id = _first_action_string(action, "action_id", "actionId", "id")
    package_id = _first_action_string(
        action,
        "package_id",
        "packageId",
        fallback=fallback_package_id,
    )
    component_id = _first_action_string(action, "component_id", "componentId")
    return {
        "plan_id": plan_id,
        "action_id": action_id,
        "package_id": package_id,
        "component_id": component_id,
        "key": "|".join([package_id, plan_id, action_id, component_id]),
    }


def _first_action_string(
    action: dict[str, Any],
    *keys: str,
    fallback: str = "",
) -> str:
    for key in keys:
        value = str(action.get(key) or "").strip()
        if value:
            return value
    params = action.get("params")
    if isinstance(params, dict):
        for key in keys:
            value = str(params.get(key) or "").strip()
            if value:
                return value
    return str(fallback or "").strip()


def _capability_value(item: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(field_name, default)
    return getattr(item, field_name, default)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
