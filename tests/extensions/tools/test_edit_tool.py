from __future__ import annotations

from pathlib import Path

from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.interfaces.shared.approval_preview import build_preview_diff


def _write_raw(path: Path, content: str) -> None:
    with path.open("w", newline="") as handle:
        handle.write(content)


def _read_raw(path: Path) -> str:
    with path.open("r", newline="") as handle:
        return handle.read()


def test_local_edit_matches_old_string_across_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    _write_raw(target, "alpha\r\nbeta\r\ngamma\r\n")

    result = EditFileTool().execute(
        file_path=str(target),
        old_string="alpha\nbeta",
        new_string="one\ntwo",
    )

    assert result.startswith("Edited ")
    assert _read_raw(target) == "one\r\ntwo\r\ngamma\r\n"


def test_local_edit_keeps_safe_match_counts_after_line_ending_fallback(
    tmp_path: Path,
) -> None:
    target = tmp_path / "main.txt"
    _write_raw(target, "alpha\r\nbeta\r\nalpha\r\nbeta\r\n")

    result = EditFileTool().execute(
        file_path=str(target),
        old_string="alpha\nbeta",
        new_string="one\ntwo",
    )

    assert "old_string appears 2 times" in result
    assert _read_raw(target) == "alpha\r\nbeta\r\nalpha\r\nbeta\r\n"


def test_approval_preview_uses_shared_edit_matching(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    _write_raw(target, "alpha\r\nbeta\r\ngamma\r\n")

    diff = build_preview_diff(
        ApprovalRequest(
            tool_name="edit_file",
            tool_args={
                "file_path": str(target),
                "old_string": "alpha\nbeta",
                "new_string": "one\ntwo",
            },
            tool_source="builtin",
        )
    )

    assert diff is not None
    assert "-alpha" in diff
    assert "+one" in diff
    assert _read_raw(target) == "alpha\r\nbeta\r\ngamma\r\n"
