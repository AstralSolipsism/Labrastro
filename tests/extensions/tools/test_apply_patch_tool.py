from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import reuleauxcoder.domain.files.file_mutation_service as file_mutation_module
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.builtin.draft_document import DraftDocumentBeginTool
from reuleauxcoder.domain.files import FileMutationService
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.interfaces.shared.approval_preview import build_preview_diff


_CONTRACT_FIXTURE = Path(__file__).parents[2] / "fixtures" / "apply_patch_contract.json"


def _load_contract_fixture() -> dict:
    return json.loads(_CONTRACT_FIXTURE.read_text(encoding="utf-8"))


def _patch_text(item: dict) -> str:
    return "\n".join(item["patch"])


def _write_fixture_files(root: Path, setup: dict[str, str]) -> None:
    for relative_path, content in setup.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, newline="")


def _backend(tmp_path: Path) -> LocalToolBackend:
    return LocalToolBackend(
        ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
    )


def _read_text(path: Path) -> str:
    with path.open("r", newline="") as handle:
        return handle.read()


def test_apply_patch_contract_fixtures_drive_python_preview(tmp_path: Path) -> None:
    contract = _load_contract_fixture()

    for item in contract["valid"]:
        workspace = tmp_path / "valid" / item["name"]
        workspace.mkdir(parents=True)
        _write_fixture_files(workspace, item["setup"])

        result = FileMutationService(workspace).preview_text_patch(_patch_text(item))

        assert result.status == "in_progress", item["name"]
        assert [
            {
                "path": change.path,
                "kind": change.kind,
                **({"move_path": change.move_path} if change.move_path else {}),
            }
            for change in result.changes
        ] == item["expected_changes"]

    for item in contract["invalid"]:
        workspace = tmp_path / "invalid" / item["name"]
        workspace.mkdir(parents=True)
        _write_fixture_files(workspace, item["setup"])

        result = FileMutationService(workspace).preview_text_patch(_patch_text(item))

        assert result.status == "failed", item["name"]
        assert result.error is not None
        assert item["error_contains"] in result.error

    for item in contract["path_invalid"]:
        workspace = tmp_path / "path_invalid" / item["name"]
        workspace.mkdir(parents=True)
        _write_fixture_files(workspace, item["setup"])

        result = ApplyPatchTool().preflight_validate(patch=_patch_text(item))

        assert result is not None, item["name"]
        assert item["error_contains"] in result

    for item in contract["semantic_invalid"]:
        workspace = tmp_path / "semantic_invalid" / item["name"]
        workspace.mkdir(parents=True)
        _write_fixture_files(workspace, item["setup"])

        result = FileMutationService(workspace).preview_text_patch(_patch_text(item))

        assert result.status == "failed", item["name"]
        assert result.error is not None
        assert item["error_contains"] in result.error


def test_apply_patch_tool_schema_exposes_full_contract() -> None:
    schema = ApplyPatchTool().schema()
    function = schema["function"]
    patch_description = function["parameters"]["properties"]["patch"]["description"]
    visible_contract = f"{function['description']}\n{patch_description}"

    assert "*** Add File:" in visible_contract
    assert "*** Update File:" in visible_contract
    assert "*** Delete File:" in visible_contract
    assert "Add File" in visible_contract and "must start with +" in visible_contract
    assert "*** File:" in visible_contract and "Do not" in visible_contract
    assert "C:foo.txt" in visible_contract
    assert "draft_document_begin" in visible_contract


def test_apply_patch_preflight_rejects_contract_invalid_patch_before_backend() -> None:
    contract = _load_contract_fixture()
    invalid_patch = next(
        item for item in contract["invalid"] if item["name"] == "file_action_headers"
    )

    result = ApplyPatchTool().preflight_validate(patch=_patch_text(invalid_patch))

    assert result is not None
    assert invalid_patch["error_contains"] in result
    assert "*** Update File:" in result
    assert "*** File:" in result


def test_apply_patch_updates_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("alpha\nbeta\n", newline="")
    tool = ApplyPatchTool(backend=_backend(tmp_path))

    result = tool.execute(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: main.txt",
                "@@",
                "-alpha",
                "+omega",
                " beta",
                "*** End Patch",
            ]
        )
    )

    assert result.startswith("Applied patch")
    assert "+omega" in result
    assert _read_text(target) == "omega\nbeta\n"


def test_apply_patch_adds_file_and_rejects_workspace_escape(tmp_path: Path) -> None:
    tool = ApplyPatchTool(backend=_backend(tmp_path))

    result = tool.execute(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Add File: docs/new.md",
                "+hello",
                "*** End Patch",
            ]
        )
    )

    assert result.startswith("Applied patch")
    assert _read_text(tmp_path / "docs" / "new.md") == "hello\n"

    denied = tool.execute(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Add File: ../outside.md",
                "+bad",
                "*** End Patch",
            ]
        )
    )
    assert "path traversal is not allowed" in denied


def test_apply_patch_rejects_duplicate_context_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("same\nsame\n", newline="")
    tool = ApplyPatchTool(backend=_backend(tmp_path))

    result = tool.execute(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: main.txt",
                "@@",
                "-same",
                "+other",
                "*** End Patch",
            ]
        )
    )

    assert "patch context matches multiple locations" in result
    assert _read_text(target) == "same\nsame\n"


def test_apply_patch_moves_file_without_rewriting_content(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    target = tmp_path / "new.txt"
    source.write_text("alpha\n", newline="")
    tool = ApplyPatchTool(backend=_backend(tmp_path))

    result = tool.execute(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: old.txt",
                "*** Move to: new.txt",
                "*** End Patch",
            ]
        )
    )

    assert result.startswith("Applied patch")
    assert not source.exists()
    assert _read_text(target) == "alpha\n"


def test_apply_patch_approval_preview_uses_same_patch_planner(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("alpha\nbeta\n", newline="")

    diff = build_preview_diff(
        ApprovalRequest(
            tool_name="apply_patch",
            tool_args={
                "patch": "\n".join(
                    [
                        "*** Begin Patch",
                        "*** Update File: main.txt",
                        "@@",
                        "-alpha",
                        "+omega",
                        " beta",
                        "*** End Patch",
                    ]
                )
            },
            metadata={"runtime_workspace_root": str(tmp_path)},
        )
    )

    assert diff is not None
    assert "-alpha" in diff
    assert "+omega" in diff
    assert _read_text(target) == "alpha\nbeta\n"


def test_apply_patch_rejects_patch_argument_over_runtime_limit(tmp_path: Path) -> None:
    tool = ApplyPatchTool(backend=_backend(tmp_path))

    result = tool.preflight_validate(patch="x" * (64 * 1024 + 1))

    assert result is not None
    assert "exceeds 64 KiB" in result


def test_file_mutation_service_rolls_back_multi_file_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0
    original_write_text = file_mutation_module._write_text

    def fail_second_write(path: Path, content: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        original_write_text(path, content)

    monkeypatch.setattr(file_mutation_module, "_write_text", fail_second_write)
    result = FileMutationService(tmp_path).apply_text_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Add File: one.txt",
                "+one",
                "*** Add File: two.txt",
                "+two",
                "*** End Patch",
            ]
        )
    )

    assert result.status == "failed"
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()


def test_file_mutation_plan_describes_all_operations(tmp_path: Path) -> None:
    result = FileMutationService(tmp_path).preview_text_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Add File: one.txt",
                "+one",
                "*** Add File: two.txt",
                "+two",
                "*** End Patch",
            ]
        )
    )

    assert result.status == "in_progress"
    assert result.plan_id
    assert result.candidate_hash
    assert [change.path for change in result.changes] == ["one.txt", "two.txt"]


def test_file_mutation_service_saves_approved_candidate_without_reapplying_old_patch(
    tmp_path: Path,
) -> None:
    target = tmp_path / "main.txt"
    target.write_text("alpha\n", newline="")
    service = FileMutationService(tmp_path)
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: main.txt",
            "@@",
            "-alpha",
            "+model candidate",
            "*** End Patch",
        ]
    )

    preview = service.preview_text_patch(patch)
    approved_candidate = deepcopy(preview.approved_save_candidate)
    approved_candidate["operations"][0]["new_content"] = "user confirmed\n"
    target.write_text("manual edit during approval\n", newline="")

    result = service.save_candidate(approved_candidate)

    assert result.status == "completed"
    assert result.error is None
    assert _read_text(target) == "user confirmed\n"
    assert result.preview_identity == preview.preview_identity


def test_file_mutation_service_delete_candidate_accepts_already_missing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "gone.txt"
    target.write_text("remove me\n", newline="")
    service = FileMutationService(tmp_path)
    preview = service.preview_text_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Delete File: gone.txt",
                "*** End Patch",
            ]
        )
    )
    target.unlink()

    result = service.save_candidate(deepcopy(preview.approved_save_candidate))

    assert result.status == "completed"
    assert result.error is None
    assert not target.exists()


def test_file_mutation_service_move_candidate_accepts_already_missing_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "old.py"
    target = tmp_path / "src" / "new.py"
    source.parent.mkdir()
    source.write_text("name = 'old'\n", newline="")
    service = FileMutationService(tmp_path)
    preview = service.preview_text_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Update File: src/old.py",
                "*** Move to: src/new.py",
                "@@",
                "-name = 'old'",
                "+name = 'new'",
                "*** End Patch",
            ]
        )
    )
    source.unlink()

    result = service.save_candidate(deepcopy(preview.approved_save_candidate))

    assert result.status == "completed"
    assert result.error is None
    assert not source.exists()
    assert _read_text(target) == "name = 'new'\n"


def test_file_mutation_service_delete_candidate_rejects_directory_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "gone.txt"
    target.write_text("remove me\n", newline="")
    service = FileMutationService(tmp_path)
    preview = service.preview_text_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Delete File: gone.txt",
                "*** End Patch",
            ]
        )
    )
    target.unlink()
    target.mkdir()

    result = service.save_candidate(deepcopy(preview.approved_save_candidate))

    assert result.status == "failed"
    assert result.error is not None
    assert "is not a file" in result.error
    assert target.is_dir()


def test_file_mutation_service_move_candidate_rejects_directory_source_and_keeps_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "old.py"
    target = tmp_path / "src" / "new.py"
    source.parent.mkdir()
    source.write_text("name = 'old'\n", newline="")
    service = FileMutationService(tmp_path)
    preview = service.preview_text_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Update File: src/old.py",
                "*** Move to: src/new.py",
                "@@",
                "-name = 'old'",
                "+name = 'new'",
                "*** End Patch",
            ]
        )
    )
    source.unlink()
    source.mkdir()
    target.write_text("target before\n", newline="")

    result = service.save_candidate(deepcopy(preview.approved_save_candidate))

    assert result.status == "failed"
    assert result.error is not None
    assert "is not a file" in result.error
    assert source.is_dir()
    assert _read_text(target) == "target before\n"


def test_document_commit_rejects_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "architecture.md"
    target.parent.mkdir()
    target.write_text("existing\n", encoding="utf-8")
    service = FileMutationService(tmp_path)

    preview = service.preview_document_commit("docs/architecture.md", "# New\n")

    assert preview.status == "failed"
    assert preview.error is not None
    assert "already exists" in preview.error
    assert "apply_patch" in preview.error
    assert target.read_text(encoding="utf-8") == "existing\n"

    result = service.commit_document("docs/architecture.md", "# New\n")

    assert result.status == "failed"
    assert result.error is not None
    assert "already exists" in result.error
    assert "apply_patch" in result.error
    assert target.read_text(encoding="utf-8") == "existing\n"


def test_draft_document_begin_rejects_content_argument(tmp_path: Path) -> None:
    tool = DraftDocumentBeginTool(backend=_backend(tmp_path))

    result = tool.preflight_validate(
        target_path="docs/architecture.md",
        title="Architecture",
        format="markdown",
        content="# hidden body",
    )

    assert result is not None
    assert "must not include content" in result
