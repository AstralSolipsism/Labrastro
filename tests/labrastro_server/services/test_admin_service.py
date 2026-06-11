from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from labrastro_server.services.admin.service import (
    AdminConfigResult,
    RemoteAdminConfigManager,
    lifecycle_hook_recent_results_from_agent_runs,
    parse_mcp_servers_config,
)
from reuleauxcoder.domain.hooks.lifecycle import lifecycle_registry_from_config
from reuleauxcoder.services.config.loader import ConfigLoader


class MemoryAdminManager(RemoteAdminConfigManager):
    def __init__(self, config_path: Path, **kwargs) -> None:
        super().__init__(config_path=config_path, **kwargs)
        self.data: dict[str, object] = {}

    def _load_data(self) -> dict:
        return deepcopy(self.data)

    def _commit_config(self, data: dict, previous_data: dict):
        del previous_data
        self.data = deepcopy(data)
        return None


class FailingCommitAdminManager(MemoryAdminManager):
    def _commit_config(self, data: dict, previous_data: dict):
        del data, previous_data
        return AdminConfigResult(
            False,
            {"error": "config_reload_failed", "message": "reload failed"},
            500,
        )


def test_accept_capability_package_installs_without_activation(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.accept_capability_package_draft(
        {
            "package_id": "review",
            "draft": {
                "id": "review",
                "name": "Review",
                "components": [
                    {
                        "kind": "skill",
                        "name": "code-review",
                        "skill_content": (
                            "---\n"
                            "name: code-review\n"
                            "description: Review changes.\n"
                            "---\n"
                            "Review.\n"
                        ),
                    }
                ],
                "evidence": [{"title": "fixture", "excerpt": "review"}],
                "risk_level": "low",
            },
        }
    )

    assert result.ok is True
    package = result.payload["capability_package"]
    assert package["state"]["install_state"] in {"materialized", "installed"}
    assert package["state"]["activation_state"] == "inactive"
    assert manager.data["capability_packages"]["review"]["enabled"] is False
    assert manager.data["capability_components"]["skill:code-review"]["enabled"] is False
    assert manager.data["skills"]["items"]["code-review"]["enabled"] is False

    enabled = manager.enable_capability_package(
        {"package_id": "review", "enabled": True}
    )

    assert enabled.ok is True
    assert manager.data["capability_packages"]["review"]["enabled"] is True
    assert manager.data["capability_components"]["skill:code-review"]["enabled"] is True
    assert manager.data["skills"]["items"]["code-review"]["enabled"] is True


def test_accept_capability_package_rolls_back_config_when_skill_file_write_fails(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    (tmp_path / "skills").write_text("not a directory", encoding="utf-8")

    result = manager.accept_capability_package_draft(
        {
            "package_id": "review",
            "draft": {
                "id": "review",
                "name": "Review",
                "components": [
                    {
                        "kind": "skill",
                        "name": "code-review",
                        "skill_content": (
                            "---\n"
                            "name: code-review\n"
                            "description: Review changes.\n"
                            "---\n"
                            "Review.\n"
                        ),
                    }
                ],
                "evidence": [{"title": "fixture", "excerpt": "review"}],
                "risk_level": "low",
            },
        }
    )

    assert result.ok is False
    assert result.payload["error"] == "capability_package_skill_file_operation_failed"
    assert manager.data.get("capability_packages", {}) == {}
    assert manager.data.get("capability_components", {}) == {}
    assert manager.data.get("skills", {}).get("items", {}) == {}


def test_disable_capability_package_keeps_shared_component_active_for_other_owner(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    skill_content = (
        "---\n"
        "name: shared-review\n"
        "description: Shared review skill.\n"
        "---\n"
        "Review.\n"
    )
    manager.data = {
        "capability_packages": {
            "review-a": {
                "enabled": True,
                "status": "installed",
                "state": {"activation_state": "active"},
                "components": ["skill:shared-review"],
            },
            "review-b": {
                "enabled": True,
                "status": "installed",
                "state": {"activation_state": "active"},
                "components": ["skill:shared-review"],
            },
        },
        "capability_components": {
            "skill:shared-review": {
                "id": "skill:shared-review",
                "kind": "skill",
                "name": "shared-review",
                "enabled": True,
                "status": "installed",
                "managed_by": "capability_package",
                "package_ids": ["review-a", "review-b"],
                "config": {"skill_content": skill_content},
            }
        },
        "skills": {
            "items": {
                "shared-review": {
                    "name": "shared-review",
                    "enabled": True,
                    "managed_by": "capability_package",
                    "component_id": "skill:shared-review",
                    "package_ids": ["review-a", "review-b"],
                }
            }
        },
    }

    disabled_a = manager.enable_capability_package(
        {"package_id": "review-a", "enabled": False}
    )

    assert disabled_a.ok is True
    assert manager.data["capability_components"]["skill:shared-review"]["enabled"] is True
    assert manager.data["skills"]["items"]["shared-review"]["enabled"] is True

    disabled_b = manager.enable_capability_package(
        {"package_id": "review-b", "enabled": False}
    )

    assert disabled_b.ok is True
    assert manager.data["capability_components"]["skill:shared-review"]["enabled"] is False
    assert manager.data["skills"]["items"]["shared-review"]["enabled"] is False


def test_prepare_capability_package_update_records_candidate_without_activation(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": True,
                "status": "installed",
                "source_snapshot": {
                    "snapshot_id": "snap-old",
                    "source_ref": "main",
                    "commit_sha": "1111111",
                },
                "manifest": {"components": [{"id": "skill:waza/read"}]},
                "components": ["skill:waza/read"],
            }
        }
    }

    result = manager.prepare_capability_package_update(
        {
            "package_id": "waza",
            "candidate_snapshot": {
                "snapshot_id": "snap-new",
                "source_ref": "main",
                "commit_sha": "2222222",
            },
            "candidate_manifest": {
                "components": [
                    {"id": "skill:waza/read"},
                    {"id": "skill:waza/write"},
                ]
            },
        }
    )

    assert result.ok is True
    package = manager.data["capability_packages"]["waza"]
    assert package["enabled"] is True
    assert package["state"]["activation_state"] == "active"
    assert package["state"]["update_state"] == "candidate_ready"
    assert package["update_candidate"]["upstream_version"] == "main@2222222"
    assert package["update_candidate"]["manifest_diff"]["added_components"] == [
        "skill:waza/write"
    ]


def test_prepare_capability_package_update_accepts_alias_payload(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": True,
                "status": "installed",
                "source_snapshot": {
                    "snapshot_id": "snap-old",
                    "source_ref": "main",
                    "commit_sha": "1111111",
                },
                "manifest": {"components": [{"id": "skill:waza/read"}]},
                "components": ["skill:waza/read"],
            }
        }
    }

    result = manager.prepare_capability_package_update(
        {
            "package_id": "waza",
            "source_snapshot": {
                "id": "snap-new",
                "ref": "main",
                "sha": "2222222",
                "tag": "v2.0.0",
            },
            "manifest": {
                "components": [
                    {"id": "skill:waza/read"},
                    {"id": "skill:waza/write"},
                ]
            },
            "update_candidate_id": "cand-alias",
        }
    )

    assert result.ok is True
    package = manager.data["capability_packages"]["waza"]
    assert package["enabled"] is True
    candidate = package["update_candidate"]
    assert candidate["candidate_id"] == "cand-alias"
    assert candidate["upstream_version"] == "v2.0.0"
    assert candidate["source_snapshot"]["snapshot_id"] == "snap-new"
    assert candidate["source_snapshot"]["source_ref"] == "main"
    assert candidate["source_snapshot"]["commit_sha"] == "2222222"
    assert candidate["manifest_diff"]["added_components"] == ["skill:waza/write"]
    assert package["state"]["activation_state"] == "active"
    assert package["state"]["update_state"] == "candidate_ready"


def test_apply_capability_package_update_candidate_does_not_auto_activate(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    old_content = "---\nname: waza-read\ndescription: Read Waza.\n---\nRead.\n"
    new_content = "---\nname: waza-write\ndescription: Write Waza.\n---\nWrite.\n"
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": True,
                "status": "installed",
                "source_snapshot": {"snapshot_id": "snap-old"},
                "manifest": {
                    "components": [
                        {
                            "id": "skill:waza/read",
                            "kind": "skill",
                            "name": "waza-read",
                            "config": {"skill_content": old_content},
                        }
                    ]
                },
                "update_candidate": {
                    "candidate_id": "cand-1",
                    "source_snapshot": {
                        "snapshot_id": "snap-new",
                        "source_ref": "main",
                        "commit_sha": "2222222",
                    },
                    "manifest": {
                        "components": [
                            {
                                "id": "skill:waza/write",
                                "kind": "skill",
                                "name": "waza-write",
                                "config": {"skill_content": new_content},
                            }
                        ]
                    },
                    "manifest_diff": {
                        "added_components": ["skill:waza/write"],
                        "removed_components": ["skill:waza/read"],
                    },
                    "upstream_version": "main@2222222",
                    "rollback_snapshot_id": "snap-old",
                    "rollback_source_snapshot": {"snapshot_id": "snap-old"},
                    "rollback_manifest": {
                        "components": [
                            {
                                "id": "skill:waza/read",
                                "kind": "skill",
                                "name": "waza-read",
                                "config": {"skill_content": old_content},
                            }
                        ]
                    },
                },
            }
        }
    }

    result = manager.apply_capability_package_update(
        {"package_id": "waza", "activation_approved": False}
    )

    assert result.ok is True
    package = manager.data["capability_packages"]["waza"]
    assert package["enabled"] is False
    assert package["components"] == ["skill:waza/write"]
    assert package["state"]["activation_state"] == "inactive"
    assert package["state"]["update_state"] == "rollback_available"
    assert package["rollback"]["snapshot_id"] == "snap-old"


def test_apply_capability_package_update_converges_materialized_resources(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    old_content = (
        "---\n"
        "name: waza-read\n"
        "description: Read Waza.\n"
        "---\n"
        "Read.\n"
    )
    new_content = (
        "---\n"
        "name: waza-write\n"
        "description: Write Waza.\n"
        "---\n"
        "Write.\n"
    )
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": True,
                "status": "installed",
                "state": {"activation_state": "active"},
                "source_snapshot": {"snapshot_id": "snap-old"},
                "manifest": {
                    "components": [
                        {
                            "id": "skill:waza-read",
                            "kind": "skill",
                            "name": "waza-read",
                            "config": {"skill_content": old_content},
                        }
                    ]
                },
                "components": ["skill:waza-read"],
                "update_candidate": {
                    "candidate_id": "cand-1",
                    "source_snapshot": {"snapshot_id": "snap-new"},
                    "manifest": {
                        "components": [
                            {
                                "id": "skill:waza-write",
                                "kind": "skill",
                                "name": "waza-write",
                                "config": {"skill_content": new_content},
                            }
                        ]
                    },
                    "manifest_diff": {
                        "added_components": ["skill:waza-write"],
                        "removed_components": ["skill:waza-read"],
                    },
                    "rollback_snapshot_id": "snap-old",
                    "rollback_source_snapshot": {"snapshot_id": "snap-old"},
                    "rollback_manifest": {
                        "components": [
                            {
                                "id": "skill:waza-read",
                                "kind": "skill",
                                "name": "waza-read",
                                "config": {"skill_content": old_content},
                            }
                        ]
                    },
                },
            }
        },
        "capability_components": {
            "skill:waza-read": {
                "id": "skill:waza-read",
                "kind": "skill",
                "name": "waza-read",
                "enabled": True,
                "status": "installed",
                "managed_by": "capability_package",
                "package_ids": ["waza"],
                "config": {"skill_content": old_content},
            }
        },
        "skills": {
            "items": {
                "waza-read": {
                    "name": "waza-read",
                    "enabled": True,
                    "managed_by": "capability_package",
                    "component_id": "skill:waza-read",
                    "package_ids": ["waza"],
                }
            }
        },
    }

    result = manager.apply_capability_package_update(
        {"package_id": "waza", "activation_approved": True}
    )

    assert result.ok is True
    assert "skill:waza-read" not in manager.data["capability_components"]
    assert "waza-read" not in manager.data["skills"]["items"]
    assert manager.data["capability_components"]["skill:waza-write"]["package_ids"] == [
        "waza"
    ]
    assert manager.data["skills"]["items"]["waza-write"]["component_id"] == (
        "skill:waza-write"
    )
    installed_path = (
        tmp_path
        / "skills"
        / "packages"
        / "components"
        / "skill-waza-write"
        / "SKILL.md"
    )
    assert installed_path.read_text(encoding="utf-8") == new_content


def test_rollback_capability_package_update_converges_materialized_resources(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    old_content = (
        "---\n"
        "name: waza-read\n"
        "description: Read Waza.\n"
        "---\n"
        "Read.\n"
    )
    new_content = (
        "---\n"
        "name: waza-write\n"
        "description: Write Waza.\n"
        "---\n"
        "Write.\n"
    )
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": True,
                "status": "installed",
                "state": {
                    "activation_state": "active",
                    "update_state": "rollback_available",
                },
                "source_snapshot": {"snapshot_id": "snap-new"},
                "manifest": {
                    "components": [
                        {
                            "id": "skill:waza-write",
                            "kind": "skill",
                            "name": "waza-write",
                            "config": {"skill_content": new_content},
                        }
                    ]
                },
                "components": ["skill:waza-write"],
                "rollback": {
                    "snapshot_id": "snap-old",
                    "source_snapshot": {"snapshot_id": "snap-old"},
                    "manifest": {
                        "components": [
                            {
                                "id": "skill:waza-read",
                                "kind": "skill",
                                "name": "waza-read",
                                "config": {"skill_content": old_content},
                            }
                        ]
                    },
                },
            }
        },
        "capability_components": {
            "skill:waza-write": {
                "id": "skill:waza-write",
                "kind": "skill",
                "name": "waza-write",
                "enabled": True,
                "status": "installed",
                "managed_by": "capability_package",
                "package_ids": ["waza"],
                "config": {"skill_content": new_content},
            }
        },
        "skills": {
            "items": {
                "waza-write": {
                    "name": "waza-write",
                    "enabled": True,
                    "managed_by": "capability_package",
                    "component_id": "skill:waza-write",
                    "package_ids": ["waza"],
                }
            }
        },
    }

    result = manager.rollback_capability_package_update(
        {"package_id": "waza", "activation_approved": True}
    )

    assert result.ok is True
    assert "skill:waza-write" not in manager.data["capability_components"]
    assert "waza-write" not in manager.data["skills"]["items"]
    assert manager.data["capability_components"]["skill:waza-read"]["package_ids"] == [
        "waza"
    ]
    assert manager.data["skills"]["items"]["waza-read"]["component_id"] == (
        "skill:waza-read"
    )
    installed_path = (
        tmp_path
        / "skills"
        / "packages"
        / "components"
        / "skill-waza-read"
        / "SKILL.md"
    )
    assert installed_path.read_text(encoding="utf-8") == old_content


def test_rollback_capability_package_update_rejects_empty_rollback_metadata(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": False,
                "status": "installed",
                "state": {"update_state": "rollback_available"},
                "rollback": {},
            }
        }
    }

    result = manager.rollback_capability_package_update({"package_id": "waza"})

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "rollback_not_available"
    assert manager.data["capability_packages"]["waza"]["rollback"] == {}
    assert manager.data["capability_packages"]["waza"]["state"]["update_state"] == (
        "rollback_available"
    )


def test_rollback_capability_package_update_rejects_consumed_rollback(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": False,
                "status": "installed",
                "state": {"update_state": "current"},
                "rollback": {
                    "snapshot_id": "snap-old",
                    "source_snapshot": {"snapshot_id": "snap-old"},
                },
            }
        }
    }

    result = manager.rollback_capability_package_update({"package_id": "waza"})

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "rollback_not_available"
    assert manager.data["capability_packages"]["waza"]["rollback"]["snapshot_id"] == (
        "snap-old"
    )
    assert manager.data["capability_packages"]["waza"]["state"]["update_state"] == (
        "current"
    )


def test_check_capability_package_update_does_not_report_no_diff_candidate(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "waza": {
                "enabled": True,
                "status": "installed",
                "source_snapshot": {
                    "snapshot_id": "snap-old",
                    "source_ref": "main",
                    "commit_sha": "1111111",
                },
                "manifest": {"components": [{"id": "skill:waza/read"}]},
                "components": ["skill:waza/read"],
            }
        }
    }

    result = manager.check_capability_package_update(
        {
            "package_id": "waza",
            "candidate_snapshot": {
                "snapshot_id": "snap-old",
                "source_ref": "main",
                "commit_sha": "1111111",
            },
            "candidate_manifest": {"components": [{"id": "skill:waza/read"}]},
        }
    )

    assert result.ok is True
    assert result.payload["update_available"] is False
    assert result.payload["update_candidate"]["manifest_diff"]["added_components"] == []


def test_server_settings_projects_capability_package_credential_bindings(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "current_user_id": "user-a",
        "workspace_id": "workspace-1",
        "capability_packages": {
            "github-tools": {
                "name": "GitHub tools",
                "credential_requirements": [
                    {
                        "id": "credreq:github",
                        "provider": "github",
                        "kind": "oauth",
                        "placement": "server",
                        "required_by": ["mcp:github"],
                    }
                ],
                "credential_bindings": [
                    {
                        "requirement_id": "credreq:github",
                        "scope": "workspace",
                        "workspace_id": "workspace-1",
                        "secret_ref_id": "github-workspace",
                    },
                    {
                        "requirement_id": "credreq:github",
                        "scope": "user",
                        "user_id": "user-a",
                        "secret_ref_id": "github-user",
                    },
                ],
            }
        },
    }

    settings = manager.read_server_settings()["settings"]
    package = settings["capability_packages"]["github-tools"]

    assert package["credential_state"] == "bound"
    assert package["credential_requirements"][0] == {
        "requirement_id": "credreq:github",
        "provider": "github",
        "kind": "oauth",
        "placement": "server",
        "required_by": ["mcp:github"],
        "state": "bound",
        "scope": "user",
        "secret_ref_id": "github-user",
        "credential_actor": "user_delegated",
        "message": "user credential binding is selected",
    }
    assert "secret_value" not in str(package)


def test_server_settings_does_not_apply_unscoped_global_credential_binding_to_packages(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "current_user_id": "user-a",
        "workspace_id": "workspace-1",
        "capability_credential_bindings": [
            {
                "requirement_id": "credreq:github",
                "scope": "workspace",
                "workspace_id": "workspace-1",
                "secret_ref_id": "github-workspace",
            }
        ],
        "capability_packages": {
            "github-tools": {
                "name": "GitHub tools",
                "credential_requirements": [
                    {
                        "id": "credreq:github",
                        "provider": "github",
                        "kind": "oauth",
                        "placement": "server",
                    }
                ],
            },
            "repo-review": {
                "name": "Repo review",
                "credential_requirements": [
                    {
                        "id": "credreq:github",
                        "provider": "github",
                        "kind": "oauth",
                        "placement": "server",
                    }
                ],
            },
        },
    }

    settings = manager.read_server_settings()["settings"]

    assert settings["capability_packages"]["github-tools"]["credential_state"] == "missing"
    assert settings["capability_packages"]["repo-review"]["credential_state"] == "missing"


def test_accept_capability_package_preserves_credential_requirements(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.accept_capability_package_draft(
        {
            "package_id": "github-tools",
            "draft": {
                "id": "github-tools",
                "name": "GitHub tools",
                "components": [
                    {
                        "kind": "skill",
                        "name": "review",
                        "skill_content": (
                            "---\n"
                            "name: review\n"
                            "description: Review GitHub changes.\n"
                            "---\n"
                            "Review.\n"
                        ),
                    }
                ],
                "credential_requirements": [
                    {
                        "id": "credreq:github",
                        "provider": "github",
                        "kind": "oauth",
                        "placement": "server",
                        "required_by": ["skill:review"],
                    }
                ],
                "evidence": [{"title": "fixture", "excerpt": "github"}],
                "risk_level": "low",
            },
        }
    )

    assert result.ok is True
    package = manager.data["capability_packages"]["github-tools"]
    assert package["credential_requirements"][0]["id"] == "credreq:github"
    assert package["credential_requirements"][0]["required_by"] == ["skill:review"]
    assert "secret_value" not in str(package)


def test_record_skill_installs_pasted_skill_content_to_standard_path(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes before merging.\n"
        "---\n\n"
        "Use the repository review checklist.\n"
    )

    result = manager.record_skill(
        {
            "skill_content": skill_content,
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "enabled": True,
        }
    )

    installed_path = tmp_path / "skills" / "user" / "code-review" / "SKILL.md"
    assert result.ok is True
    assert installed_path.read_text(encoding="utf-8") == skill_content
    skill = manager.data["skills"]["items"]["code-review"]
    assert skill["display_name"] == "Code review"
    assert skill["summary"] == "Review repository changes before merging."
    assert skill["description"] == "Review code changes before merging."
    assert skill["path_hint"] == str(installed_path)
    assert "skill_content" not in skill
    assert result.payload["skill"]["display_name"] == "Code review"
    assert result.payload["skill"]["summary"] == "Review repository changes before merging."


def test_record_skill_does_not_auto_grant_agent_capability_refs(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data["agents"] = {
        "coder": {
            "id": "coder",
            "capability_refs": [],
        }
    }
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes before merging.\n"
        "---\n\n"
        "Use the repository review checklist.\n"
    )

    result = manager.record_skill(
        {
            "skill_content": skill_content,
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "enabled": True,
        }
    )

    assert result.ok is True
    assert manager.data["agents"]["coder"]["capability_refs"] == []


def test_record_skill_preserves_lifecycle_hooks(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    hooks = [
        {
            "event": "UserPromptSubmit",
            "handler_type": "prompt",
            "handler_ref": "skills/code-review/SKILL.md",
            "display_name": "Code review prompt context",
            "summary": "Adds code review context.",
            "permissions": [],
            "trust": "trusted",
        }
    ]

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": hooks,
        }
    )

    assert result.ok is True
    expected_hooks = [dict(hooks[0], placement="server", trust="pending_review")]
    assert manager.data["skills"]["items"]["code-review"]["hooks"] == expected_hooks
    assert result.payload["skill"]["hooks"] == expected_hooks


def test_record_skill_keeps_lifecycle_placement_separate_from_runtime_footprint(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "runtime_footprint": {
                "runs_on": "local_peer",
                "install_required_on": ["local_peer"],
                "config_required_on": ["local_peer"],
            },
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "handler_type": "prompt",
                    "display_name": "Code review prompt context",
                    "summary": "Adds code review context.",
                    "permissions": [],
                }
            ],
        }
    )

    skill = manager.data["skills"]["items"]["code-review"]
    assert result.ok is True
    assert skill["runtime_footprint"]["runs_on"] == "local_peer"
    assert skill["hooks"][0]["placement"] == "server"
    assert result.payload["skill"]["hooks"][0]["placement"] == "server"


def test_record_skill_rejects_invalid_lifecycle_hook_before_config_write(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "handler_type": "prompt",
                    "display_name": "Code review prompt context",
                    "summary": "Adds code review context.",
                    "permissions": [],
                    "matcher": {"tool": {"name": "shell"}},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "matcher" in result.payload["message"]
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_rejects_internal_lifecycle_handler_before_config_write(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "handler_type": "internal",
                    "handler_ref": "ProjectContextStartupNotifier",
                    "display_name": "Unsafe internal hook",
                    "summary": "Attempts to call a Python hook from public skill config.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "internal handlers" in result.payload["message"]
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_rejects_legacy_lifecycle_tool_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    legacy_field = "tool_" + "name"

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "PreToolUse",
                    "handler_type": "prompt",
                    "display_name": "Code review tool guard",
                    "summary": "Adds code review context for matching tools.",
                    "permissions": [],
                    "matcher": {legacy_field: "shell"},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert legacy_field in result.payload["message"]
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_accepts_lifecycle_tool_names_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "PreToolUse",
                    "handler_type": "prompt",
                    "display_name": "Code review tool guard",
                    "summary": "Adds code review context for matching tools.",
                    "permissions": [],
                    "matcher": {"tool_names": "shell"},
                }
            ],
        }
    )

    assert result.ok is True
    hooks = manager.data["skills"]["items"]["code-review"]["hooks"]
    assert hooks[0]["matcher"] == {"tool_names": "shell"}


def test_record_skill_rejects_invalid_pasted_skill_content(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill({"skill_content": "missing frontmatter"})

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_skill_content"
    assert "frontmatter" in result.payload["message"]
    assert not (tmp_path / "skills").exists()


def test_record_skill_does_not_commit_config_when_skill_file_install_fails(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    manager._standalone_skill_install_path = lambda _skill_name: blocked_parent / "SKILL.md"  # type: ignore[method-assign]

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
        }
    )

    assert result.ok is False
    assert result.status == 500
    assert result.payload["error"] == "skill_content_install_failed"
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_removes_new_skill_file_when_config_commit_fails(tmp_path: Path) -> None:
    manager = FailingCommitAdminManager(tmp_path / "config.yaml")
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes before merging.\n"
        "---\n\n"
        "Use the repository review checklist.\n"
    )
    installed_path = tmp_path / "skills" / "user" / "code-review" / "SKILL.md"

    result = manager.record_skill({"skill_content": skill_content})

    assert result.ok is False
    assert result.payload["error"] == "config_reload_failed"
    assert not installed_path.exists()
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_restores_existing_skill_file_when_config_commit_fails(tmp_path: Path) -> None:
    manager = FailingCommitAdminManager(tmp_path / "config.yaml")
    installed_path = tmp_path / "skills" / "user" / "code-review" / "SKILL.md"
    installed_path.parent.mkdir(parents=True)
    previous_content = (
        "---\n"
        "name: code-review\n"
        "description: Previous description.\n"
        "---\n\n"
        "Previous body.\n"
    )
    installed_path.write_text(previous_content, encoding="utf-8")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
        }
    )

    assert result.ok is False
    assert result.payload["error"] == "config_reload_failed"
    assert installed_path.read_text(encoding="utf-8") == previous_content
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_parse_mcp_servers_config_reads_standard_json() -> None:
    raw = """
    {
      "mcpServers": {
        "edgeone-pages-mcp-server": {
          "command": "npx",
          "args": ["edgeone-pages-mcp"],
          "env": {"EDGEONE_TOKEN": "${EDGEONE_TOKEN}"}
        }
      }
    }
    """

    drafts = parse_mcp_servers_config(raw)

    assert drafts == [
        {
            "name": "edgeone-pages-mcp-server",
            "command": "npx",
            "args": ["edgeone-pages-mcp"],
            "env": {"EDGEONE_TOKEN": "${EDGEONE_TOKEN}"},
        }
    ]


def test_record_mcp_server_installs_from_standard_mcp_json(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    raw = {
        "mcpServers": {
            "edgeone-pages-mcp-server": {
                "command": "npx",
                "args": ["edgeone-pages-mcp"],
            }
        }
    }

    result = manager.record_mcp_server(
        {
            "name": "",
            "mcp_config": raw,
            "display_name": "EdgeOne Pages",
            "summary": "Deploy pages through EdgeOne.",
            "runtime_footprint": {
                "runs_on": "local_peer",
                "install_required_on": ["local_peer"],
                "config_required_on": ["local_peer"],
            },
        }
    )

    assert result.ok is True
    server = manager.data["mcp"]["servers"]["edgeone-pages-mcp-server"]
    assert server["command"] == "npx"
    assert server["args"] == ["edgeone-pages-mcp"]
    assert server["display_name"] == "EdgeOne Pages"
    assert server["summary"] == "Deploy pages through EdgeOne."
    assert server["placement"] == "peer"
    assert server["runtime_footprint"] == {
        "runs_on": "local_peer",
        "install_required_on": ["local_peer"],
        "config_required_on": ["local_peer"],
        "user_message": "需要在本机安装/配置",
    }
    assert "mcp_config" not in server
    assert "mcpServers" not in server


def test_record_mcp_server_preserves_lifecycle_hooks(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    hooks = [
        {
            "event": "PostToolUse",
            "handler_type": "mcp_tool",
            "handler_ref": "github.audit",
            "display_name": "GitHub audit",
            "summary": "Records GitHub MCP tool results.",
            "permissions": ["audit.write"],
            "trust": "trusted",
        }
    ]

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": hooks,
        }
    )

    assert result.ok is True
    expected_hooks = [dict(hooks[0], placement="server", trust="pending_review")]
    assert manager.data["mcp"]["servers"]["github"]["hooks"] == expected_hooks
    assert result.payload["mcp_server"]["hooks"] == expected_hooks


def test_record_mcp_server_keeps_lifecycle_placement_separate_from_runtime_footprint(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "runtime_footprint": {
                "runs_on": "local_peer",
                "install_required_on": ["local_peer"],
                "config_required_on": ["local_peer"],
            },
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "handler_ref": "github.audit",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                }
            ],
        }
    )

    server = manager.data["mcp"]["servers"]["github"]
    assert result.ok is True
    assert server["placement"] == "peer"
    assert server["runtime_footprint"]["runs_on"] == "local_peer"
    assert server["hooks"][0]["placement"] == "server"
    assert result.payload["mcp_server"]["hooks"][0]["placement"] == "server"


def test_record_mcp_server_rejects_invalid_lifecycle_hook_before_config_write(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "placement": "local_peer",
                    "handler_type": "mcp_tool",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "placement" in result.payload["message"]
    assert "github" not in manager.data.get("mcp", {}).get("servers", {})


def test_record_mcp_server_rejects_internal_lifecycle_handler_before_config_write(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "internal",
                    "handler_ref": "memory_context",
                    "display_name": "Unsafe internal hook",
                    "summary": "Attempts to call a Python hook from public MCP config.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "internal handlers" in result.payload["message"]
    assert "github" not in manager.data.get("mcp", {}).get("servers", {})


def test_record_mcp_server_rejects_legacy_lifecycle_tool_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    legacy_field = "tool_" + "source"

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                    "matcher": {legacy_field: "mcp"},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert legacy_field in result.payload["message"]
    assert "github" not in manager.data.get("mcp", {}).get("servers", {})


def test_record_mcp_server_accepts_lifecycle_tool_names_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                    "matcher": {"tool_names": ["search", "list_issues"]},
                }
            ],
        }
    )

    assert result.ok is True
    hooks = manager.data["mcp"]["servers"]["github"]["hooks"]
    assert hooks[0]["matcher"] == {"tool_names": ["search", "list_issues"]}


def test_parse_mcp_servers_config_reads_lifecycle_hooks_from_extended_json() -> None:
    raw = {
        "mcpServers": {
            "github": {
                "command": "github-mcp-server",
                "hooks": [
                    {
                        "event": "PostToolUse",
                        "handler_type": "mcp_tool",
                        "handler_ref": "github.audit",
                        "display_name": "GitHub audit",
                        "summary": "Records GitHub MCP tool results.",
                        "permissions": ["audit.write"],
                    }
                ],
            }
        }
    }

    drafts = parse_mcp_servers_config(raw)

    assert drafts[0]["hooks"] == raw["mcpServers"]["github"]["hooks"]


def test_record_mcp_server_rejects_invalid_mcp_json(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server({"mcp_config": "{}"})

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_mcp_config"


def test_skill_and_mcp_dashboard_return_user_display_fields(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "path_hint": "/skills/code-review/SKILL.md",
        }
    )
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
        }
    )

    skills = {item["name"]: item for item in manager.list_skills()["skills"]}
    mcps = {item["name"]: item for item in manager.list_mcp_servers()["mcp_servers"]}
    skill_rows = {item["id"]: item for item in manager.skills_dashboard()["items"]}
    mcp_rows = {item["id"]: item for item in manager.mcp_servers_dashboard()["items"]}

    assert skills["code-review"]["display_name"] == "Code review"
    assert skills["code-review"]["summary"] == "Review repository changes before merging."
    assert mcps["github"]["display_name"] == "GitHub tools"
    assert mcps["github"]["summary"] == "Read issues and pull requests."
    assert skill_rows["skill:code-review"]["display_name"] == "Code review"
    assert skill_rows["skill:code-review"]["summary"] == "Review repository changes before merging."
    assert mcp_rows["mcp:github"]["display_name"] == "GitHub tools"
    assert mcp_rows["mcp:github"]["summary"] == "Read issues and pull requests."
    assert skill_rows["skill:code-review"]["runtime_footprint"]["runs_on"] == "agent_only"
    assert mcp_rows["mcp:github"]["runtime_footprint"]["runs_on"] == "server"


def test_mcp_dashboard_keeps_lifecycle_hooks_separate_from_mcp_memory_provider_status(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "handler_ref": "github.audit",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": ["audit.write"],
                }
            ],
        }
    )
    manager.data["memory"] = {
        "enabled": True,
        "default_provider": "github_memory",
        "providers": {
            "github_memory": {
                "adapter": "mcp_memory",
                "mcp_server": "github",
                "hooks": [
                    {
                        "event": "UserPromptSubmit",
                        "handler_type": "mcp_tool",
                        "handler_ref": "github.memory_audit",
                        "display_name": "Memory audit",
                        "summary": "This must stay out of lifecycle hook views.",
                        "permissions": ["memory.read"],
                    }
                ],
            }
        },
        "sources": {
            "github_source": {
                "adapter": "mcp_memory",
                "target_provider": "github_memory",
                "mcp_server": "github",
            }
        },
        "tools": {
            "enabled": True,
            "provider": "github_memory",
            "recall": True,
        },
    }

    mcp_row = manager.mcp_servers_dashboard()["items"][0]
    settings = manager.read_server_settings()["settings"]
    memory_status = settings["memory_status"]

    assert [hook["id"] for hook in mcp_row["hook_views"]] == [
        "hook:mcp_server:github:PostToolUse:0"
    ]
    assert mcp_row["hook_views"][0]["source"] == "mcp_server"
    assert "github_memory" not in str(mcp_row["hook_views"])
    assert "memory_provider" not in str(mcp_row["hook_views"])

    assert memory_status["providers"] == [
        {
            "id": "github_memory",
            "is_default": True,
            "provider": "github_memory",
            "adapter": "mcp_memory",
            "configured": True,
            "enabled": True,
            "adapter_registered": False,
            "available": False,
            "status": "adapter_missing",
        }
    ]
    assert memory_status["sources"][0]["id"] == "github_source"
    assert memory_status["sources"][0]["target_provider"] == "github_memory"
    assert memory_status["tools"]["provider"] == "github_memory"
    assert "hook:" not in str(memory_status)
    assert "Memory audit" not in str(memory_status)


def test_mcp_dashboard_renders_lifecycle_hook_trust_risk_and_recent_result(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        lifecycle_hook_results_provider=lambda source, owner_id: {
            "hook:mcp_server:github:PreToolUse:0": {
                "status": "denied",
                "summary": f"{source}:{owner_id} denied GitHub mutation",
                "agent_run_id": "agent-run-mcp-1",
            }
        },
    )
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
            "args": ["stdio", "--verbose"],
            "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"},
            "hooks": [
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "mcp_tool",
                    "handler_ref": "github.guard",
                    "display_name": "GitHub MCP guard",
                    "summary": "Guards GitHub MCP tool calls.",
                    "permissions": ["mcp.invoke"],
                    "credentials": ["GITHUB_TOKEN"],
                    "risk_level": "high",
                    "trust": "trusted",
                }
            ],
        }
    )
    trust_result = manager.update_lifecycle_hook_trust(
        {"hook_id": "hook:mcp_server:github:PreToolUse:0", "trust": "trusted"}
    )

    row = manager.mcp_servers_dashboard()["items"][0]
    hook = row["hook_views"][0]

    assert trust_result.ok is True
    assert row["command"] == "github-mcp-server"
    assert row["args"] == ["stdio", "--verbose"]
    assert row["env"] == {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"}
    assert hook["id"] == "hook:mcp_server:github:PreToolUse:0"
    assert hook["source"] == "mcp_server"
    assert hook["trust"] == "trusted"
    assert hook["credentials"] == ["GITHUB_TOKEN"]
    assert hook["risk_level"] == "high"
    assert hook["recent_result"] == {
        "status": "denied",
        "summary": "mcp_server:github denied GitHub mutation",
        "agent_run_id": "agent-run-mcp-1",
    }


def test_skill_dashboard_returns_lifecycle_hook_summary_with_technical_details(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "skills/code-review/SKILL.md",
                    "matcher": {"trigger_source": "chat"},
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                    "risk_level": "low",
                    "trust": "trusted",
                }
            ],
        }
    )

    row = manager.skills_dashboard()["items"][0]

    assert row["hook_views"] == [
        {
            "id": "hook:skill:code-review:UserPromptSubmit:0",
            "event": "UserPromptSubmit",
            "source": "skill",
            "owner_id": "code-review",
            "owner_enabled": True,
            "owner_status": "installed",
            "owner_activation_state": "active",
            "placement": "server",
            "handler_type": "prompt",
            "display_name": "Review prompt guard",
            "summary": "Checks review prompts before the model sees them.",
            "trust": "pending_review",
            "enabled": False,
            "executable": False,
            "can_manage": True,
            "management_actions": [
                {
                    "trust": "trusted",
                    "label": "Trust hook",
                    "endpoint": "admin.lifecycle_hooks.trust",
                },
                {
                    "trust": "disabled",
                    "label": "Disable hook",
                    "endpoint": "admin.lifecycle_hooks.trust",
                },
                {
                    "trust": "blocked",
                    "label": "Block hook",
                    "endpoint": "admin.lifecycle_hooks.trust",
                },
            ],
            "unavailable_reason": "trust:pending_review",
            "placement_runtime": {
                "server": {
                    "executable": False,
                    "unavailable_reason": "trust:pending_review",
                },
            },
            "runtime_context_required": ["prompt_model"],
            "permissions": ["prompt.read"],
            "credentials": [],
            "risk_level": "low",
            "recent_result": {
                "status": "unrecorded",
                "summary": "No lifecycle executions recorded.",
            },
            "technical": {
                "handler_ref": "skills/code-review/SKILL.md",
                "matcher": {"trigger_source": "chat"},
            },
        }
    ]
    assert "handler_ref" not in row["hook_views"][0]


def test_skill_dashboard_returns_lifecycle_hook_credentials_and_recent_result(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "command",
                    "handler_ref": "python scripts/check.py",
                    "display_name": "Review shell guard",
                    "summary": "Checks shell commands.",
                    "permissions": ["shell.read"],
                    "credentials": ["GITHUB_TOKEN"],
                    "risk_level": "high",
                    "trust": "trusted",
                }
            ],
            "lifecycle_hook_results": {
                "hook:skill:code-review:PreToolUse:0": {
                    "status": "denied",
                    "summary": "Denied shell command",
                    "session_run_id": "session-run-1",
                }
            },
        }
    )

    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]

    assert hook["credentials"] == ["GITHUB_TOKEN"]
    assert hook["risk_level"] == "high"
    assert hook["recent_result"] == {
        "status": "denied",
        "summary": "Denied shell command",
        "session_run_id": "session-run-1",
    }


def test_skill_dashboard_uses_runtime_lifecycle_hook_recent_result_provider(tmp_path: Path) -> None:
    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        lifecycle_hook_results_provider=lambda source, owner_id: {
            "hook:skill:code-review:PreToolUse:0": {
                "status": "success",
                "summary": f"{source}:{owner_id} allowed shell command",
                "agent_run_id": "agent-run-1",
            }
        },
    )
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "command",
                    "handler_ref": "python scripts/check.py",
                    "display_name": "Review shell guard",
                    "summary": "Checks shell commands.",
                    "permissions": ["shell.read"],
                    "trust": "trusted",
                }
            ],
        }
    )

    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]

    assert hook["recent_result"] == {
        "status": "success",
        "summary": "skill:code-review allowed shell command",
        "agent_run_id": "agent-run-1",
    }


def test_lifecycle_hook_recent_results_from_agent_runs_filters_owner_and_keeps_latest() -> None:
    class _Event:
        def __init__(self, seq: int, payload: dict) -> None:
            self.seq = seq
            self.type = "lifecycle_hook"
            self.payload = payload

    class _Runtime:
        def list_agent_runs(self, *, limit: int = 50):
            del limit
            return [{"id": "run-new"}, {"id": "run-old"}]

        def list_events(self, task_id: str, *, after_seq: int = 0, limit: int = 500):
            del after_seq, limit
            if task_id == "run-old":
                return [
                    _Event(1, {
                        "data": {
                            "hook_id": "hook:skill:code-review:PreToolUse:0",
                            "source": "skill",
                            "event_name": "PreToolUse",
                            "display_name": "Review shell guard",
                            "decision": "allow",
                            "continue_flow": True,
                        }
                    })
                ]
            return [
                _Event(1, {
                    "data": {
                        "hook_id": "hook:mcp_server:github:PreToolUse:0",
                        "source": "mcp_server",
                        "event_name": "PreToolUse",
                    }
                }),
                _Event(2, {
                    "data": {
                        "hook_id": "hook:skill:code-review:PreToolUse:0",
                        "source": "skill",
                        "event_name": "PreToolUse",
                        "display_name": "Review shell guard",
                        "decision": "deny",
                        "continue_flow": False,
                        "message": "Denied shell command",
                    }
                }),
            ]

    results = lifecycle_hook_recent_results_from_agent_runs(
        _Runtime(),
        "skill",
        "code-review",
    )

    assert results == {
        "hook:skill:code-review:PreToolUse:0": {
            "status": "denied",
            "summary": "Denied shell command",
            "decision": "deny",
            "event": "PreToolUse",
            "agent_run_id": "run-new",
        }
    }


def test_lifecycle_hook_recent_results_scans_beyond_first_event_page() -> None:
    class _Event:
        def __init__(self, seq: int, event_type: str, payload: dict) -> None:
            self.seq = seq
            self.type = event_type
            self.payload = payload

    class _Runtime:
        def __init__(self) -> None:
            self.events = [
                *[
                    _Event(seq, "status", {"data": {"status": "running"}})
                    for seq in range(1, 1001)
                ],
                _Event(
                    1001,
                    "lifecycle_hook",
                    {
                        "data": {
                            "hook_id": "hook:skill:code-review:PreToolUse:0",
                            "source": "skill",
                            "event_name": "PreToolUse",
                            "decision": "allow",
                            "continue_flow": True,
                        }
                    },
                ),
            ]

        def list_agent_runs(self, *, limit: int = 50):
            del limit
            return [{"id": "run-long"}]

        def list_events(self, task_id: str, *, after_seq: int = 0, limit: int = 500):
            assert task_id == "run-long"
            return [event for event in self.events if event.seq > after_seq][:limit]

    results = lifecycle_hook_recent_results_from_agent_runs(
        _Runtime(),
        "skill",
        "code-review",
        event_limit=500,
    )

    assert results["hook:skill:code-review:PreToolUse:0"] == {
        "status": "success",
        "summary": "PreToolUse",
        "decision": "allow",
        "event": "PreToolUse",
        "agent_run_id": "run-long",
    }


def test_provider_test_reports_protocol_mismatch_hint(tmp_path: Path) -> None:
    def _fail_provider_test(_provider, _model: str, _prompt: str):
        raise RuntimeError("Error code: 500 - {'message': 'invalid csrf token'}")

    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        provider_test_handler=_fail_provider_test,
    )
    manager.data["providers"] = {
        "items": {
            "Zenmux": {
                "type": "anthropic_messages",
                "api_key": "sk-secret-should-not-leak",
                "base_url": "https://zenmux.ai/api/v1",
            }
        }
    }

    result = manager.test_provider(
        {
            "provider_id": "Zenmux",
            "model": "anthropic/claude-opus-4.7",
            "prompt": "ping",
        }
    )

    assert result.status == 500
    assert result.payload["error"] == "provider_test_failed"
    assert result.payload["provider_id"] == "Zenmux"
    assert result.payload["provider_type"] == "anthropic_messages"
    assert result.payload["suspected_reason"] == "provider_protocol_mismatch_suspected"
    assert "openai_chat" in result.payload["recommended_action"]
    assert "invalid csrf token" in result.payload["upstream_message"]
    assert "sk-secret" not in str(result.payload)


def test_behavior_catalog_exposes_lifecycle_event_wiring_status(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    catalog = manager.behavior_catalog()

    events = {item["event"]: item for item in catalog["lifecycle_hook_events"]}
    assert events["PermissionDenied"]["external_config_supported"] is True
    assert events["PermissionDenied"]["runtime_status"] == "external_config_supported"
    assert events["SessionStart"]["external_config_supported"] is False
    assert events["SessionStart"]["runtime_status"] == "external_event_unwired"
    assert events["UserPromptExpansion"]["external_config_supported"] is True
    assert events["UserPromptExpansion"]["runtime_status"] == "external_config_supported"
    assert events["TaskCreated"]["external_config_supported"] is True
    assert events["SubagentStart"]["external_config_supported"] is True
    assert events["PreCompact"]["external_config_supported"] is True
    assert events["PostCompact"]["external_config_supported"] is True
    assert events["ConfigChange"]["external_config_supported"] is True
    assert events["CwdChanged"]["external_config_supported"] is True
    assert events["FileChanged"]["external_config_supported"] is True
    assert events["Notification"]["external_config_supported"] is True
    assert events["WorktreeCreate"]["external_config_supported"] is False
    assert events["WorktreeCreate"]["runtime_status"] == "control_plane_audit_only"
    assert events["WorktreeCreate"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert events["WorktreeRemove"]["external_config_supported"] is False
    assert events["WorktreeRemove"]["runtime_status"] == "control_plane_audit_only"
    assert events["WorktreeRemove"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert events["TaskCompleted"]["external_config_supported"] is False
    assert events["TaskCompleted"]["runtime_status"] == "control_plane_audit_only"
    assert events["TaskCompleted"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert events["SubagentStop"]["external_config_supported"] is False
    assert events["SubagentStop"]["runtime_status"] == "control_plane_audit_only"
    assert events["SubagentStop"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )


def test_admin_commit_emits_config_change_lifecycle_after_successful_reload(
    tmp_path: Path,
) -> None:
    reloads: list[str] = []
    changes: list[dict] = []
    config_path = tmp_path / "config.yaml"
    manager = RemoteAdminConfigManager(
        config_path=config_path,
        reload_handler=lambda: reloads.append("reload"),
        config_change_handler=lambda event: changes.append(event),
    )

    result = manager._commit_config(
        {"models": {"active_main": "main"}},
        {"models": {"active_main": "old"}},
    )

    assert result is None
    assert reloads == ["reload"]
    assert len(changes) == 1
    event = changes[0]
    assert event["event_name"] == "ConfigChange"
    assert event["status"] == "committed"
    assert event["config_path"] == str(config_path)
    assert event["previous_etag"] != event["current_etag"]
    assert event["path_space"] == "server_config"


def test_lifecycle_hook_trust_update_changes_config_and_dashboard_execution_state(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                    "trust": "trusted",
                }
            ],
        }
    )

    hook_id = "hook:skill:code-review:UserPromptSubmit:0"
    before = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert before["trust"] == "pending_review"
    assert before["executable"] is False

    result = manager.update_lifecycle_hook_trust(
        {"hook_id": hook_id, "trust": "trusted"}
    )

    assert result.ok is True
    assert manager.data["skills"]["items"]["code-review"]["hooks"][0]["trust"] == "trusted"
    after = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert after["trust"] == "trusted"
    assert after["executable"] is False
    assert after["unavailable_reason"] == "handler_ref_unavailable:prompt"


def test_lifecycle_hook_trust_update_persists_all_states_and_runtime_filter(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "Review prompts before model input.",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )
    hook_id = "hook:skill:code-review:UserPromptSubmit:0"

    for trust in ("pending_review", "trusted", "disabled", "blocked"):
        result = manager.update_lifecycle_hook_trust(
            {"hook_id": hook_id, "trust": trust}
        )

        assert result.ok is True
        stored = manager.data["skills"]["items"]["code-review"]["hooks"][0]
        hook = manager.skills_dashboard()["items"][0]["hook_views"][0]
        assert stored["trust"] == trust
        assert hook["trust"] == trust
        if trust == "trusted":
            assert hook["executable"] is True
            assert hook["unavailable_reason"] == ""
        else:
            assert hook["executable"] is False
            assert hook["unavailable_reason"] == f"trust:{trust}"


def test_lifecycle_hook_dashboard_reports_both_placement_runtime_independently(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "both",
                    "handler_type": "prompt",
                    "handler_ref": "Review prompts before model input.",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )
    hook_id = "hook:skill:code-review:UserPromptSubmit:0"
    result = manager.update_lifecycle_hook_trust(
        {"hook_id": hook_id, "trust": "trusted"}
    )

    assert result.ok is True
    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert hook["placement"] == "both"
    assert hook["executable"] is True
    assert hook["unavailable_reason"] == ""
    assert hook["placement_runtime"]["server"] == {
        "executable": True,
        "unavailable_reason": "",
    }
    assert hook["placement_runtime"]["peer"] == {
        "executable": False,
        "unavailable_reason": "peer_runtime_unavailable",
    }


def test_lifecycle_hook_dashboard_reports_both_placement_for_all_public_resource_views(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    hook = {
        "event": "UserPromptSubmit",
        "placement": "both",
        "handler_type": "prompt",
        "handler_ref": "Review prompts before model input.",
        "display_name": "Review prompt guard",
        "summary": "Checks review prompts before the model sees them.",
        "permissions": ["prompt.read"],
        "trust": "trusted",
    }
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
            "hooks": [hook],
        }
    )
    manager.update_lifecycle_hook_trust(
        {
            "hook_id": "hook:mcp_server:github:UserPromptSubmit:0",
            "trust": "trusted",
        }
    )
    manager.data["capability_packages"] = {
        "review": {
            "enabled": True,
            "status": "installed",
            "hooks": [dict(hook)],
        }
    }
    manager.data["capability_components"] = {
        "mcp_server:github": {
            "kind": "mcp_server",
            "name": "github",
            "enabled": True,
            "status": "installed",
            "hooks": [dict(hook)],
        }
    }

    mcp_hook = manager.mcp_servers_dashboard()["items"][0]["hook_views"][0]
    settings = manager.read_server_settings()["settings"]
    package_hook = settings["capability_packages"]["review"]["hook_views"][0]
    component_hook = settings["capability_components"]["mcp_server:github"][
        "hook_views"
    ][0]

    for hook_view in (mcp_hook, package_hook, component_hook):
        assert hook_view["placement"] == "both"
        assert hook_view["executable"] is True
        assert hook_view["unavailable_reason"] == ""
        assert hook_view["placement_runtime"]["server"] == {
            "executable": True,
            "unavailable_reason": "",
        }
        assert hook_view["placement_runtime"]["peer"] == {
            "executable": False,
            "unavailable_reason": "peer_runtime_unavailable",
        }


def test_lifecycle_hook_dashboard_exposes_management_actions(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "skills/code-review/SKILL.md",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )

    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]

    assert hook["can_manage"] is True
    assert {item["trust"] for item in hook["management_actions"]} >= {
        "trusted",
        "disabled",
        "blocked",
    }
    assert all(item["endpoint"] == "admin.lifecycle_hooks.trust" for item in hook["management_actions"])


def test_lifecycle_hook_dashboard_marks_trusted_prompt_with_handler_ref_executable(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "skills/code-review/SKILL.md",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )
    hook_id = "hook:skill:code-review:UserPromptSubmit:0"

    result = manager.update_lifecycle_hook_trust(
        {"hook_id": hook_id, "trust": "trusted"}
    )

    assert result.ok is True
    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert hook["trust"] == "trusted"
    assert hook["executable"] is True
    assert hook["unavailable_reason"] == ""
    assert hook["runtime_context_required"] == ["prompt_model"]
    assert hook["placement_runtime"]["server"]["executable"] is True


def test_record_skill_rejects_raw_lifecycle_hook_id(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    result = manager.record_skill(
        {
            "name": "code-review",
            "hooks": [
                {
                    "id": "raw-review-id",
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "id" in result.payload["message"]
    assert "skills" not in manager.data


def test_server_settings_returns_canonical_package_and_component_hook_views(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "enabled": True,
                        "status": "installed",
                        "hooks": [
                            {
                                "event": "UserPromptSubmit",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package prompt",
                                "summary": "Adds review prompt context.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
                "capability_components": {
                    "skill:review": {
                        "kind": "skill",
                        "name": "review",
                        "enabled": False,
                        "hooks": [
                            {
                                "event": "PreToolUse",
                                "placement": "server",
                                "handler_type": "agent",
                                "display_name": "Review tool policy",
                                "summary": "Checks review tool use.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
            }
        }
    )
    assert result.ok is True

    settings = manager.read_server_settings()["settings"]
    package_config_hook = settings["capability_packages"]["review"]["hooks"][0]
    component_config_hook = settings["capability_components"]["skill:review"]["hooks"][0]
    package_hook = settings["capability_packages"]["review"]["hook_views"][0]
    component_hook = settings["capability_components"]["skill:review"]["hook_views"][0]

    assert "id" not in package_config_hook
    assert "owner_id" not in package_config_hook
    assert package_config_hook["event"] == "UserPromptSubmit"
    assert component_config_hook["event"] == "PreToolUse"
    assert package_hook["id"] == "hook:capability_package:review:UserPromptSubmit:0"
    assert package_hook["owner_id"] == "review"
    assert package_hook["trust"] == "trusted"
    assert package_hook["executable"] is False
    assert package_hook["unavailable_reason"] == "handler_ref_unavailable:prompt"
    assert component_hook["id"] == "hook:skill:skill:review:PreToolUse:0"
    assert component_hook["owner_id"] == "skill:review"
    assert component_hook["owner_enabled"] is False
    assert component_hook["executable"] is False
    assert component_hook["unavailable_reason"] == "owner_disabled"


def test_server_settings_package_and_component_hook_views_include_recent_results(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        lifecycle_hook_results_provider=lambda source, owner_id: {
            f"hook:{source}:{owner_id}:PreToolUse:0": {
                "status": "denied",
                "summary": f"{source}:{owner_id} denied tool use",
                "agent_run_id": "agent-run-component",
            },
            f"hook:{source}:{owner_id}:UserPromptSubmit:0": {
                "status": "success",
                "summary": f"{source}:{owner_id} accepted prompt",
                "agent_run_id": "agent-run-package",
            },
        },
    )
    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "enabled": True,
                        "status": "installed",
                        "hooks": [
                            {
                                "event": "UserPromptSubmit",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package prompt",
                                "summary": "Adds review prompt context.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
                "capability_components": {
                    "skill:review": {
                        "kind": "skill",
                        "name": "review",
                        "hooks": [
                            {
                                "event": "PreToolUse",
                                "placement": "server",
                                "handler_type": "agent",
                                "display_name": "Review tool policy",
                                "summary": "Checks review tool use.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is True
    settings = manager.read_server_settings()["settings"]
    package_hook = settings["capability_packages"]["review"]["hook_views"][0]
    component_hook = settings["capability_components"]["skill:review"]["hook_views"][0]

    assert package_hook["recent_result"] == {
        "status": "success",
        "summary": "capability_package:review accepted prompt",
        "agent_run_id": "agent-run-package",
    }
    assert component_hook["recent_result"] == {
        "status": "denied",
        "summary": "skill:skill:review denied tool use",
        "agent_run_id": "agent-run-component",
    }


def test_server_settings_rejects_hook_views_written_back_to_config(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "review": {
                "enabled": True,
                "status": "installed",
                "hooks": [
                    {
                        "event": "PostToolUse",
                        "placement": "server",
                        "handler_type": "prompt",
                        "handler_ref": "review.audit",
                        "matcher": {"tool_names": "read_file"},
                        "display_name": "Review audit",
                        "summary": "Audit tools.",
                        "permissions": [],
                        "trust": "trusted",
                    }
                ],
            }
        }
    }

    settings = manager.read_server_settings()["settings"]
    package = settings["capability_packages"]["review"]

    assert package["hooks"][0]["handler_ref"] == "review.audit"
    assert package["hooks"][0]["matcher"] == {"tool_names": "read_file"}
    assert package["hook_views"][0]["id"] == "hook:capability_package:review:PostToolUse:0"
    assert package["hook_views"][0]["technical"]["matcher"] == {"tool_names": "read_file"}

    result = manager.update_server_settings(
        {"capability_packages": settings["capability_packages"]}
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "hook_views" in result.payload["message"]

    clean_package = dict(package)
    clean_package.pop("hook_views")
    result = manager.update_server_settings(
        {"capability_packages": {"review": clean_package}}
    )

    assert result.ok is True
    stored_hook = manager.data["capability_packages"]["review"]["hooks"][0]
    assert stored_hook["handler_ref"] == "review.audit"
    assert stored_hook["matcher"] == {"tool_names": "read_file"}
    assert "technical" not in stored_hook
    config = ConfigLoader()._parse_config(manager.data)
    declarations = lifecycle_registry_from_config(config).query(
        event="PostToolUse",
        placement="server",
        trust="trusted",
    )
    assert len(declarations) == 1
    assert declarations[0].handler_ref == "review.audit"
    assert declarations[0].matcher == {"tool_names": "read_file"}


def test_server_settings_rejects_dashboard_hook_view_inside_config_hooks(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    dashboard_hook_view = {
        "id": "hook:capability_package:review:PostToolUse:0",
        "event": "PostToolUse",
        "source": "capability_package",
        "owner_id": "review",
        "owner_enabled": True,
        "owner_status": "installed",
        "placement": "server",
        "handler_type": "prompt",
        "display_name": "Review audit",
        "summary": "Audit tools.",
        "permissions": [],
        "trust": "trusted",
        "enabled": True,
        "executable": True,
        "can_manage": True,
        "unavailable_reason": "",
        "technical": {
            "handler_ref": "review.audit",
            "matcher": {"tool_names": "read_file"},
        },
    }

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [dashboard_hook_view],
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "id" in result.payload["message"]
    assert "review" not in manager.data.get("capability_packages", {})


def test_server_settings_rejects_invalid_lifecycle_hook_before_config_write(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [
                        {
                            "event": "UserPromptSubmit",
                            "placement": "server",
                            "handler_type": "python_class",
                            "display_name": "Review package startup",
                                "summary": "Adds review startup context.",
                                "permissions": [],
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "handler_type" in result.payload["message"]
    assert "review" not in manager.data.get("capability_packages", {})


def test_server_settings_rejects_legacy_lifecycle_tool_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    legacy_field = "mcp_" + "server"

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [
                            {
                                "event": "PostToolUse",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package tool hook",
                                "summary": "Adds review context for matching tools.",
                                "permissions": [],
                                "matcher": {legacy_field: "github"},
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert legacy_field in result.payload["message"]
    assert "review" not in manager.data.get("capability_packages", {})


def test_server_settings_accepts_lifecycle_tool_names_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [
                            {
                                "event": "PostToolUse",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package tool hook",
                                "summary": "Adds review context for matching tools.",
                                "permissions": [],
                                "matcher": {"tool_names": ["read_file", "apply_patch"]},
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is True
    settings = manager.read_server_settings()["settings"]
    hook = settings["capability_packages"]["review"]["hooks"][0]
    hook_view = settings["capability_packages"]["review"]["hook_views"][0]
    assert hook["matcher"] == {"tool_names": ["read_file", "apply_patch"]}
    assert hook_view["technical"]["matcher"] == {"tool_names": ["read_file", "apply_patch"]}


def test_server_settings_keeps_lifecycle_placement_separate_from_runtime_footprint(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "runtime_footprint": {
                            "runs_on": "local_peer",
                            "install_required_on": ["local_peer"],
                            "config_required_on": ["local_peer"],
                        },
                        "hooks": [
                            {
                                "event": "UserPromptSubmit",
                                "handler_type": "prompt",
                                "display_name": "Review package startup",
                                "summary": "Adds review startup context.",
                                "permissions": [],
                            }
                        ],
                    }
                },
                "capability_components": {
                    "skill:review": {
                        "kind": "skill",
                        "name": "review",
                        "runtime_footprint": {
                            "runs_on": "local_peer",
                            "install_required_on": ["local_peer"],
                            "config_required_on": ["local_peer"],
                        },
                        "hooks": [
                            {
                                "event": "PostToolUse",
                                "handler_type": "prompt",
                                "display_name": "Review component tool hook",
                                "summary": "Adds review context for matching tools.",
                                "permissions": [],
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is True
    package = manager.data["capability_packages"]["review"]
    component = manager.data["capability_components"]["skill:review"]
    assert package["runtime_footprint"]["runs_on"] == "local_peer"
    assert component["runtime_footprint"]["runs_on"] == "local_peer"
    assert package["hooks"][0]["placement"] == "server"
    assert component["hooks"][0]["placement"] == "server"


def test_skill_dashboard_aggregates_runtime_from_environment_refs(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_environment_requirement(
        {
            "id": "envreq:executable:gh",
            "kind": "executable",
            "name": "gh",
            "command": "gh",
            "placement": "peer",
        }
    )
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "environment_requirement_refs": ["envreq:executable:gh"],
        }
    )

    skills = {item["name"]: item for item in manager.list_skills()["skills"]}
    rows = {item["id"]: item for item in manager.skills_dashboard()["items"]}

    assert skills["code-review"]["environment_requirement_refs"] == ["envreq:executable:gh"]
    assert skills["code-review"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert rows["skill:code-review"]["runtime_footprint"]["runs_on"] == "local_peer"
