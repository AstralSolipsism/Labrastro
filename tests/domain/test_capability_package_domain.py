from reuleauxcoder.domain.capability_packages import (
    ACTIVATION_STATES,
    CHECK_STATES,
    CREDENTIAL_STATES,
    CapabilityManifest,
    CapabilitySourceSnapshot,
    INSTALL_STATES,
    MAPPING_STATES,
    RUNTIME_STATES,
    UPDATE_STATES,
    capability_package_state_projection,
)


def test_state_axes_are_authoritative() -> None:
    assert INSTALL_STATES == {
        "not_installed",
        "registered",
        "materialized",
        "installed",
        "blocked",
        "failed",
    }
    assert ACTIVATION_STATES == {"inactive", "active", "degraded", "blocked"}
    assert RUNTIME_STATES == {
        "not_applicable",
        "stopped",
        "starting",
        "running",
        "connected",
        "failed",
    }
    assert CHECK_STATES == {
        "unknown",
        "pending",
        "passed",
        "missing",
        "failed",
        "stale",
    }
    assert CREDENTIAL_STATES == {
        "not_required",
        "missing",
        "bound",
        "verified",
        "failed",
    }
    assert UPDATE_STATES == {
        "not_checked",
        "current",
        "update_available",
        "candidate_ready",
        "updating",
        "rollback_available",
        "failed",
    }
    assert MAPPING_STATES == {"mapped", "unmapped", "mapping_required", "invalid"}


def test_legacy_enabled_status_projects_to_state_axes() -> None:
    projection = capability_package_state_projection(
        {
            "enabled": False,
            "status": "installed",
        }
    )

    assert projection["install_state"] == "installed"
    assert projection["activation_state"] == "inactive"
    assert projection["runtime_state"] == "not_applicable"
    assert projection["check_state"] == "unknown"


def test_legacy_enabled_package_projects_to_active_only_when_installed() -> None:
    projection = capability_package_state_projection(
        {
            "enabled": True,
            "status": "installed",
        }
    )

    assert projection["install_state"] == "installed"
    assert projection["activation_state"] == "active"


def test_legacy_failed_package_does_not_project_to_active() -> None:
    projection = capability_package_state_projection(
        {
            "enabled": True,
            "status": "failed",
        }
    )

    assert projection["install_state"] == "failed"
    assert projection["activation_state"] == "blocked"
    assert projection["check_state"] == "failed"


def test_source_snapshot_uses_upstream_version_for_display() -> None:
    snapshot = CapabilitySourceSnapshot.from_dict(
        {
            "package_id": "waza",
            "source_type": "github_repo",
            "source_url": "https://github.com/tw93/Waza",
            "source_ref": "main",
            "commit_sha": "abc1234",
            "upstream_version": "",
            "snapshot_id": "snap-1",
            "snapshot_path": "capability-packages/waza/main-abc1234/source",
            "content_hash": "sha256:1",
        }
    )

    assert snapshot.display_version == "main@abc1234"


def test_source_snapshot_accepts_source_aliases() -> None:
    snapshot = CapabilitySourceSnapshot.from_dict(
        {
            "package": "waza",
            "type": "github_repo",
            "repo_url": "https://github.com/tw93/Waza",
            "ref": "main",
            "sha": "abcdef123",
            "tag": "v1.2.3",
            "id": "snap-1",
            "path": "capability-packages/waza/main-abcdef123/source",
            "hash": "sha256:1",
        }
    )

    assert snapshot.package_id == "waza"
    assert snapshot.source_url == "https://github.com/tw93/Waza"
    assert snapshot.source_ref == "main"
    assert snapshot.commit_sha == "abcdef123"
    assert snapshot.upstream_version == "v1.2.3"
    assert snapshot.snapshot_id == "snap-1"
    assert snapshot.snapshot_path.endswith("source")
    assert snapshot.content_hash == "sha256:1"


def test_manifest_keeps_unmapped_findings_out_of_components() -> None:
    manifest = CapabilityManifest.from_dict(
        {
            "package": {"id": "waza"},
            "components": [{"id": "skill:waza/read", "type": "skill"}],
            "unmapped_findings": {
                "unclassified_requirements": [
                    {"observed": "pip install --user readability-lxml"}
                ]
            },
        }
    )

    assert manifest.components[0]["id"] == "skill:waza/read"
    assert (
        manifest.unmapped_findings["unclassified_requirements"][0]["observed"]
        == "pip install --user readability-lxml"
    )
