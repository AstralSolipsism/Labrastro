from __future__ import annotations

from labrastro_server.adapters.reuleauxcoder.local_action_files import (
    LocalActionPreviewBinder,
    LocalActionSaveCandidateBinder,
    missing_required_save_candidate_fields,
    preview_identity_from_candidate,
)


def _candidate(tool_name: str = "apply_patch") -> dict:
    return {
        "tool_name": tool_name,
        "preview_identity": {
            "plan_id": "plan-1",
            "candidate_hash": "candidate-1",
            "tool_name": tool_name,
            "workspace_id": "/workspace",
            "execution_target": "local_action",
            "path_space": "local_workspace",
            "args_hash": "args-1",
        },
        "operations": [
            {
                "kind": "update",
                "path": "README.md",
                "new_content": "new",
            }
        ],
    }


def test_local_action_save_candidate_binder_remembers_by_action_args_and_cwd() -> None:
    binder = LocalActionSaveCandidateBinder(cwd="/workspace")
    candidate = _candidate()

    binder.remember_approved_candidate(
        "apply_patch",
        {"patch": "patch-a"},
        candidate,
    )

    assert binder.pop_approved_candidate("apply_patch", {"patch": "patch-b"}) is None
    assert (
        binder.pop_approved_candidate("apply_patch", {"patch": "patch-a"})
        == candidate
    )
    assert binder.pop_approved_candidate("apply_patch", {"patch": "patch-a"}) is None


def test_local_action_preview_binder_requires_complete_save_candidate() -> None:
    binder = LocalActionPreviewBinder()

    result = binder.mutation_preview_from_payload(
        "apply_patch",
        {"sections": [{"content": "diff"}]},
    )

    assert result.status == "failed"
    assert "approved_save_candidate" in (result.error or "")


def test_local_action_preview_binder_projects_candidate_identity_and_changes() -> None:
    binder = LocalActionPreviewBinder()
    candidate = _candidate()

    result = binder.mutation_preview_from_payload(
        "apply_patch",
        {
            "sections": [
                {
                    "path": "README.md",
                    "change_kind": "update",
                    "content": "--- old\n+++ new\n",
                }
            ],
            "approved_save_candidate": candidate,
            "resolved_path": "/workspace/README.md",
            "diff": "--- old\n+++ new\n",
        },
    )

    assert result.status == "in_progress"
    assert result.approved_save_candidate == candidate
    assert result.preview_identity == preview_identity_from_candidate(candidate)
    assert result.changes[0].path == "README.md"


def test_local_action_save_candidate_validation_names_missing_fields() -> None:
    assert missing_required_save_candidate_fields("read_workspace_file", None) == []
    missing = missing_required_save_candidate_fields(
        "apply_patch",
        {
            "tool_name": "apply_patch",
            "preview_identity": {"plan_id": "plan-1"},
            "operations": [],
        },
    )

    assert "preview_identity.candidate_hash" in missing
    assert "operations" in missing
