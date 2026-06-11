import pytest

from labrastro_server.services.capability_package_credentials import (
    credential_audit_payload,
    credential_resolution_for_requirements,
    resolve_credential_binding,
)
from reuleauxcoder.domain.capability_packages import (
    CapabilityCredentialBinding,
    CapabilityCredentialRequirement,
)


def test_user_binding_wins_over_workspace_and_global() -> None:
    binding = resolve_credential_binding(
        requirement_id="credreq:github",
        user_id="user-a",
        workspace_id="workspace-1",
        bindings=[
            {
                "scope": "server_global",
                "requirement_id": "credreq:github",
                "secret_ref_id": "global",
                "admin_authorized": True,
            },
            {
                "scope": "workspace",
                "workspace_id": "workspace-1",
                "requirement_id": "credreq:github",
                "secret_ref_id": "workspace",
            },
            {
                "scope": "user",
                "user_id": "user-a",
                "requirement_id": "credreq:github",
                "secret_ref_id": "user",
            },
        ],
    )

    assert binding["secret_ref_id"] == "user"
    assert binding["scope"] == "user"
    assert binding["credential_actor"] == "user_delegated"


def test_secret_value_is_rejected_in_public_payload() -> None:
    with pytest.raises(
        ValueError,
        match="secret values must not enter capability package payloads",
    ):
        resolve_credential_binding(
            requirement_id="credreq:github",
            user_id="user-a",
            workspace_id="workspace-1",
            bindings=[{"scope": "user", "secret_value": "ghp_secret"}],
        )


def test_workspace_binding_wins_when_user_binding_is_for_another_user() -> None:
    binding = resolve_credential_binding(
        requirement_id="credreq:github",
        user_id="user-a",
        workspace_id="workspace-1",
        bindings=[
            {
                "scope": "server_global",
                "requirement_id": "credreq:github",
                "secret_ref_id": "global",
                "admin_authorized": True,
            },
            {
                "scope": "workspace",
                "workspace_id": "workspace-1",
                "requirement_id": "credreq:github",
                "secret_ref_id": "workspace",
            },
            {
                "scope": "user",
                "user_id": "user-b",
                "requirement_id": "credreq:github",
                "secret_ref_id": "other-user",
            },
        ],
    )

    assert binding["secret_ref_id"] == "workspace"
    assert binding["scope"] == "workspace"


def test_server_global_binding_requires_admin_authorization() -> None:
    binding = resolve_credential_binding(
        requirement_id="credreq:github",
        user_id="user-a",
        workspace_id="workspace-1",
        bindings=[
            {
                "scope": "server_global",
                "requirement_id": "credreq:github",
                "secret_ref_id": "global",
            },
        ],
    )

    assert binding["state"] == "missing"
    assert binding["secret_ref_id"] == ""


def test_credential_resolution_marks_package_state_without_secret_values() -> None:
    resolutions = credential_resolution_for_requirements(
        requirements=[
            {
                "id": "credreq:github",
                "provider": "github",
                "kind": "oauth",
                "placement": "server",
                "required_by": ["mcp:github"],
            }
        ],
        bindings=[
            {
                "scope": "workspace",
                "workspace_id": "workspace-1",
                "requirement_id": "credreq:github",
                "secret_ref_id": "github-workspace",
            }
        ],
        user_id="user-a",
        workspace_id="workspace-1",
    )

    assert resolutions == [
        {
            "requirement_id": "credreq:github",
            "provider": "github",
            "kind": "oauth",
            "placement": "server",
            "required_by": ["mcp:github"],
            "state": "bound",
            "scope": "workspace",
            "secret_ref_id": "github-workspace",
            "credential_actor": "user_delegated",
            "message": "workspace credential binding is selected",
        }
    ]
    assert "secret_value" not in str(resolutions)


def test_credential_domain_models_roundtrip_without_plaintext_secret() -> None:
    requirement = CapabilityCredentialRequirement.from_dict(
        {
            "id": "credreq:github",
            "provider": "github",
            "kind": "token",
            "placement": "both",
            "allowed_scopes": ["repo:read"],
            "required_by": ["skill:review"],
            "credential_actor": "service_account",
        }
    )
    binding = CapabilityCredentialBinding.from_dict(
        {
            "requirement_id": "credreq:github",
            "scope": "server_global",
            "secret_ref_id": "github-app",
            "credential_actor": "service_account",
            "admin_authorized": True,
        }
    )

    assert requirement.to_dict()["allowed_scopes"] == ["repo:read"]
    assert binding.to_dict()["admin_authorized"] is True
    with pytest.raises(ValueError):
        CapabilityCredentialBinding.from_dict(
            {"requirement_id": "credreq:github", "scope": "user", "token": "plain"}
        )


def test_credential_binding_accepts_id_and_secret_ref_aliases() -> None:
    binding = CapabilityCredentialBinding.from_dict(
        {
            "id": "credreq:github",
            "scope": "workspace",
            "workspace_id": "workspace-1",
            "secret_ref": "github-workspace",
        }
    )

    assert binding.requirement_id == "credreq:github"
    assert binding.secret_ref_id == "github-workspace"
    assert binding.to_dict()["requirement_id"] == "credreq:github"
    assert binding.to_dict()["secret_ref_id"] == "github-workspace"


def test_credential_audit_payload_never_contains_secret_material() -> None:
    payload = credential_audit_payload(
        event="capability_credential_bound",
        actor_user_id="admin",
        binding={
            "requirement_id": "credreq:github",
            "scope": "user",
            "user_id": "user-a",
            "secret_ref_id": "github-user",
            "secret_value": "ghp_secret",
        },
    )

    assert payload == {
        "event": "capability_credential_bound",
        "actor_user_id": "admin",
        "requirement_id": "credreq:github",
        "scope": "user",
        "user_id": "user-a",
        "workspace_id": "",
        "secret_ref_id": "github-user",
        "credential_actor": "user_delegated",
    }
    assert "ghp_secret" not in str(payload)
