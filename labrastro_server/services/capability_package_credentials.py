"""Credential requirement and binding helpers for capability packages."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.capability_packages import (
    CapabilityCredentialBinding,
    CapabilityCredentialRequirement,
)


_SCOPE_ORDER = ("user", "workspace", "server_global")


def resolve_credential_binding(
    *,
    requirement_id: str,
    user_id: str,
    workspace_id: str,
    bindings: list[dict[str, Any]],
    allow_global: bool = True,
    credential_actor: str = "user_delegated",
) -> dict[str, Any]:
    """Resolve one credential binding without exposing credential values."""

    normalized_requirement_id = str(requirement_id or "").strip()
    if not normalized_requirement_id:
        raise ValueError("requirement_id is required")
    candidates = [
        CapabilityCredentialBinding.from_dict(item)
        for item in bindings
        if isinstance(item, dict)
    ]
    matching = [
        item
        for item in candidates
        if item.requirement_id == normalized_requirement_id
        and item.secret_ref_id
        and _binding_applies(
            item,
            user_id=str(user_id or "").strip(),
            workspace_id=str(workspace_id or "").strip(),
            allow_global=allow_global,
        )
    ]
    matching.sort(key=lambda item: _SCOPE_ORDER.index(item.scope))
    if not matching:
        return {
            "requirement_id": normalized_requirement_id,
            "state": "missing",
            "scope": "",
            "secret_ref_id": "",
            "credential_actor": _credential_actor(credential_actor),
            "message": "credential binding is missing",
        }
    selected = matching[0]
    result = selected.to_dict()
    result["state"] = "bound"
    result["message"] = f"{selected.scope} credential binding is selected"
    return result


def credential_resolution_for_requirements(
    *,
    requirements: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    user_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Resolve every requirement into a public, action-oriented state record."""

    results: list[dict[str, Any]] = []
    for raw_requirement in requirements:
        if not isinstance(raw_requirement, dict):
            continue
        requirement = CapabilityCredentialRequirement.from_dict(raw_requirement)
        binding = resolve_credential_binding(
            requirement_id=requirement.id,
            user_id=user_id,
            workspace_id=workspace_id,
            bindings=bindings,
            allow_global=requirement.allow_global,
            credential_actor=requirement.credential_actor,
        )
        results.append(
            {
                "requirement_id": requirement.id,
                "provider": requirement.provider,
                "kind": requirement.kind,
                "placement": requirement.placement,
                "required_by": list(requirement.required_by),
                "state": "bound" if binding.get("state") == "bound" else "missing",
                "scope": str(binding.get("scope") or ""),
                "secret_ref_id": str(binding.get("secret_ref_id") or ""),
                "credential_actor": str(
                    binding.get("credential_actor") or requirement.credential_actor
                ),
                "message": str(binding.get("message") or ""),
            }
        )
    return results


def public_credential_package_projection(
    *,
    requirements: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    user_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    resolutions = credential_resolution_for_requirements(
        requirements=requirements,
        bindings=bindings,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    if not resolutions:
        state = "not_required"
    elif all(item.get("state") == "bound" for item in resolutions):
        state = "bound"
    else:
        state = "missing"
    return {
        "credential_state": state,
        "credential_requirements": resolutions,
    }


def credential_audit_payload(
    *,
    event: str,
    actor_user_id: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event": str(event or "").strip(),
        "actor_user_id": str(actor_user_id or "").strip(),
        "requirement_id": str(binding.get("requirement_id") or "").strip(),
        "scope": str(binding.get("scope") or "").strip(),
        "user_id": str(binding.get("user_id") or "").strip(),
        "workspace_id": str(binding.get("workspace_id") or "").strip(),
        "secret_ref_id": str(binding.get("secret_ref_id") or "").strip(),
        "credential_actor": _credential_actor(binding.get("credential_actor")),
    }


def _binding_applies(
    binding: CapabilityCredentialBinding,
    *,
    user_id: str,
    workspace_id: str,
    allow_global: bool,
) -> bool:
    if binding.scope == "user":
        return bool(user_id and binding.user_id == user_id)
    if binding.scope == "workspace":
        return bool(workspace_id and binding.workspace_id == workspace_id)
    if binding.scope == "server_global":
        return bool(
            allow_global
            and (binding.admin_authorized or bool(binding.authorized_by))
        )
    return False


def _credential_actor(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in {"user_delegated", "service_account"} else "user_delegated"


__all__ = [
    "credential_audit_payload",
    "credential_resolution_for_requirements",
    "public_credential_package_projection",
    "resolve_credential_binding",
]
