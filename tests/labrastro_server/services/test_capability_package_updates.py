from __future__ import annotations

from labrastro_server.services.capability_package_updates import (
    apply_rollback_transition_patch,
    apply_update_transition_patch,
    build_update_transition_patch,
    detect_upstream_version,
    manifest_diff,
    manifest_diff_has_changes,
    normalize_update_transition_payload,
)


def test_main_branch_version_displays_as_main_at_commit() -> None:
    assert (
        detect_upstream_version({"source_ref": "main", "commit_sha": "abcdef123"})
        == "main@abcdef1"
    )


def test_manifest_diff_is_backend_computed() -> None:
    diff = manifest_diff(
        {"components": [{"id": "skill:waza/read"}]},
        {
            "components": [
                {"id": "skill:waza/read"},
                {"id": "skill:waza/write"},
            ],
        },
    )

    assert diff["added_components"] == ["skill:waza/write"]
    assert diff["removed_components"] == []


def test_manifest_diff_detects_same_count_non_component_section_changes() -> None:
    diff = manifest_diff(
        {
            "package": {"name": "Waza", "risk_level": "low"},
            "components": [{"id": "skill:waza/read", "summary": "Read"}],
            "dependency_edges": [
                {"from": "skill:waza/read", "to": "shared:executable:gh"}
            ],
            "environment_requirements": [
                {"id": "envreq:executable:gh", "check": "gh --version"}
            ],
            "credential_requirements": [
                {"id": "credreq:github", "provider": "github"}
            ],
            "install_plans": [{"id": "install:gh", "action": "check"}],
            "activation_rules": {"hooks": "trusted_only"},
            "exposed_file_closures": [
                {"component_id": "skill:waza/read", "paths": ["SKILL.md"]}
            ],
            "update_metadata": {"upstream_version": "main@1111111"},
        },
        {
            "package": {"name": "Waza", "risk_level": "medium"},
            "components": [{"id": "skill:waza/read", "summary": "Read"}],
            "dependency_edges": [
                {"from": "skill:waza/read", "to": "shared:executable:git"}
            ],
            "environment_requirements": [
                {"id": "envreq:executable:gh", "check": "gh auth status"}
            ],
            "credential_requirements": [
                {"id": "credreq:github", "provider": "github-oauth"}
            ],
            "install_plans": [{"id": "install:gh", "action": "install"}],
            "activation_rules": {"hooks": "default_on"},
            "exposed_file_closures": [
                {
                    "component_id": "skill:waza/read",
                    "paths": ["SKILL.md", "references/read.md"],
                }
            ],
            "update_metadata": {"upstream_version": "main@2222222"},
        },
    )

    assert diff["dependency_edge_delta"] == 0
    assert diff["environment_requirement_delta"] == 0
    assert diff["credential_requirement_delta"] == 0
    assert set(diff["changed_sections"]) == {
        "package",
        "dependency_edges",
        "environment_requirements",
        "credential_requirements",
        "install_plans",
        "activation_rules",
        "exposed_file_closures",
        "update_metadata",
    }
    assert manifest_diff_has_changes(diff) is True


def test_manifest_diff_treats_missing_and_empty_sections_as_equivalent() -> None:
    diff = manifest_diff(
        {"components": [{"id": "skill:waza/read"}]},
        {
            "package": {},
            "components": [{"id": "skill:waza/read"}],
            "dependency_edges": [],
            "environment_requirements": [],
            "credential_requirements": [],
            "install_plans": [],
            "activation_rules": {},
            "exposed_file_closures": [],
            "update_metadata": {},
        },
    )

    assert diff["changed_sections"] == []
    assert manifest_diff_has_changes(diff) is False


def test_transition_payload_normalizes_source_snapshot_and_manifest_aliases() -> None:
    payload = normalize_update_transition_payload(
        {
            "source_snapshot": {
                "id": "snap-new",
                "ref": "main",
                "sha": "2222222",
                "tag": "v2.0.0",
            },
            "manifest": {
                "components": [{"id": "skill:waza/read"}],
                "dependency_edges": [
                    {"from": "skill:waza/read", "to": "shared:executable:gh"}
                ],
            },
            "transition_id": "transition-2",
            "change_summary": "new read dependency",
        }
    )

    assert payload.has_transition is True
    assert payload.next_source_snapshot["snapshot_id"] == "snap-new"
    assert payload.next_source_snapshot["source_ref"] == "main"
    assert payload.next_source_snapshot["commit_sha"] == "2222222"
    assert payload.next_source_snapshot["upstream_version"] == "v2.0.0"
    assert payload.next_manifest["components"] == [{"id": "skill:waza/read"}]
    assert payload.transition_id == "transition-2"
    assert payload.impact_summary == "new read dependency"


def test_update_transition_patch_keeps_upstream_snapshot_and_rollback_pointer() -> None:
    patch = build_update_transition_patch(
        package_id="waza",
        current_package={
            "source_snapshot": {
                "snapshot_id": "snap-old",
                "source_ref": "main",
                "commit_sha": "1111111",
            },
            "manifest": {"components": [{"id": "skill:waza/read"}]},
        },
        next_source_snapshot={
            "snapshot_id": "snap-new",
            "source_ref": "main",
            "commit_sha": "2222222",
        },
        next_manifest={
            "components": [
                {"id": "skill:waza/read"},
                {"id": "skill:waza/write"},
            ],
        },
    )

    assert patch["package_id"] == "waza"
    assert patch["upstream_version"] == "main@2222222"
    assert patch["source_snapshot"]["snapshot_id"] == "snap-new"
    assert patch["previous_snapshot_id"] == "snap-old"
    assert patch["manifest_diff"]["added_components"] == ["skill:waza/write"]
    assert patch["state"]["update_state"] == "candidate_ready"


def test_apply_update_transition_patch_does_not_auto_activate_without_approval() -> None:
    updated = apply_update_transition_patch(
        {
            "enabled": True,
            "status": "installed",
            "source_snapshot": {"snapshot_id": "snap-old"},
            "manifest": {"components": [{"id": "skill:waza/read"}]},
        },
        {
            "candidate_id": "cand-1",
            "source_snapshot": {"snapshot_id": "snap-new"},
            "manifest": {"components": [{"id": "skill:waza/write"}]},
            "manifest_diff": {"added_components": ["skill:waza/write"]},
            "upstream_version": "v2.0.0",
            "previous_snapshot_id": "snap-old",
        },
        activation_approved=False,
    )

    assert updated["enabled"] is False
    assert updated["source_snapshot"]["snapshot_id"] == "snap-new"
    assert updated["rollback"]["snapshot_id"] == "snap-old"
    assert updated["state"]["install_state"] == "installed"
    assert updated["state"]["activation_state"] == "inactive"
    assert updated["state"]["update_state"] == "rollback_available"


def test_apply_update_transition_patch_keeps_active_package_only_with_explicit_approval() -> None:
    updated = apply_update_transition_patch(
        {"enabled": True, "status": "installed"},
        {"source_snapshot": {"snapshot_id": "snap-new"}, "manifest": {}},
        activation_approved=True,
    )

    assert updated["enabled"] is True
    assert updated["state"]["activation_state"] == "active"


def test_apply_update_transition_patch_uses_projected_activation_not_enabled_flag() -> None:
    updated = apply_update_transition_patch(
        {
            "enabled": True,
            "status": "installed",
            "state": {"activation_state": "inactive"},
        },
        {"source_snapshot": {"snapshot_id": "snap-new"}, "manifest": {}},
        activation_approved=True,
    )

    assert updated["enabled"] is False
    assert updated["state"]["activation_state"] == "inactive"


def test_apply_rollback_transition_patch_uses_projected_activation_not_enabled_flag() -> None:
    restored = apply_rollback_transition_patch(
        {
            "enabled": True,
            "status": "installed",
            "source_snapshot": {"snapshot_id": "snap-new"},
            "rollback": {
                "snapshot_id": "snap-old",
                "source_snapshot": {"snapshot_id": "snap-old"},
            },
            "state": {
                "activation_state": "inactive",
                "update_state": "rollback_available",
            },
        },
        activation_approved=True,
    )

    assert restored["enabled"] is False
    assert restored["state"]["activation_state"] == "inactive"


def test_apply_rollback_transition_patch_clears_consumed_rollback_metadata() -> None:
    restored = apply_rollback_transition_patch(
        {
            "enabled": False,
            "status": "installed",
            "source_snapshot": {"snapshot_id": "snap-new"},
            "manifest": {"components": [{"id": "skill:waza/write"}]},
            "rollback": {
                "snapshot_id": "snap-old",
                "source_snapshot": {"snapshot_id": "snap-old"},
                "manifest": {"components": [{"id": "skill:waza/read"}]},
            },
            "state": {"update_state": "rollback_available"},
        },
        activation_approved=False,
    )

    assert restored["source_snapshot"]["snapshot_id"] == "snap-old"
    assert restored["manifest"]["components"] == [{"id": "skill:waza/read"}]
    assert restored.get("rollback") in ({}, None)
    assert restored["state"]["update_state"] == "current"
