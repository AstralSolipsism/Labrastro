from __future__ import annotations

from pathlib import Path

import reuleauxcoder.domain.files.file_mutation_service as file_mutation_module
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.apply_patch import ApplyPatchTool
from reuleauxcoder.extensions.tools.builtin.draft_document import DraftDocumentBeginTool
from reuleauxcoder.domain.files import FileMutationService
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.interfaces.shared.approval_preview import build_preview_diff


def _backend(tmp_path: Path) -> LocalToolBackend:
    return LocalToolBackend(
        ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
    )


def _read_text(path: Path) -> str:
    with path.open("r", newline="") as handle:
        return handle.read()


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
    assert result.plan_hash
    assert [change.path for change in result.changes] == ["one.txt", "two.txt"]


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
