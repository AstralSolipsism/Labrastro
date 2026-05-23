from __future__ import annotations

from reuleauxcoder.extensions.tools.builtin.grep import GrepTool


def test_grep_matches_text_file(tmp_path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta gamma\n", encoding="utf-8")

    result = GrepTool().execute(pattern="gamma", path=str(target))

    assert f"{target}:2: beta gamma" in result


def test_grep_skips_direct_binary_file_with_nul(tmp_path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"needle\x00hidden\n")

    result = GrepTool().execute(pattern="needle", path=str(target))

    assert result == f"Skipped binary file: {target}"
    assert "\x00" not in result
    assert "hidden" not in result


def test_grep_keeps_text_matches_and_reports_skipped_binary_count(tmp_path) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("alpha\nneedle in text\n", encoding="utf-8")
    binary_file = tmp_path / "blob.bin"
    binary_file.write_bytes(b"needle\x00hidden\n")

    result = GrepTool().execute(pattern="needle", path=str(tmp_path))

    assert f"{text_file}:2: needle in text" in result
    assert "Skipped 1 binary file(s)." in result
    assert "\x00" not in result
    assert "hidden" not in result


def test_grep_truncates_single_long_matching_line(tmp_path) -> None:
    target = tmp_path / "long.txt"
    long_line = "needle " + ("x" * 5000)
    target.write_text(long_line + "\n", encoding="utf-8")

    result = GrepTool().execute(pattern="needle", path=str(target))

    assert f"{target}:1: needle " in result
    assert "... (line truncated)" in result
    assert "x" * 5000 not in result
