"""Tests for the list_file built-in tool."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from reuleauxcoder.extensions.tools.builtin.list_file import (
    ListFileTool,
    _format_mode,
    _sanitize_name,
)


class TestSanitizeName:
    def test_passes_plain_names(self) -> None:
        assert _sanitize_name("test.py") == "test.py"
        assert _sanitize_name("README.md") == "README.md"

    def test_escapes_markdown_sensitive_characters(self) -> None:
        assert _sanitize_name("test`file`.py") == r"test\`file\`.py"
        assert _sanitize_name("*important*.md") == r"\*important\*.md"
        assert _sanitize_name("my_file.txt") == r"my\_file.txt"
        assert _sanitize_name("[docs].md") == r"\[docs\].md"
        assert _sanitize_name("a|b.txt") == r"a\|b.txt"
        assert _sanitize_name("<tag>.xml") == r"\<tag\>.xml"


class TestFormatMode:
    def test_directory(self) -> None:
        mode = stat.S_IFDIR | 0o755
        assert _format_mode(mode) == "drwxr-xr-x"

    def test_regular_file(self) -> None:
        mode = stat.S_IFREG | 0o644
        assert _format_mode(mode) == "-rw-r--r--"


class TestListFileExecute:
    @pytest.fixture
    def tool(self) -> ListFileTool:
        return ListFileTool()

    def test_default_listing(self, tool: ListFileTool, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("hello")
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / ".hidden").write_text("secret")

        result = tool.execute(path=str(tmp_path))
        lines = result.split("\n")

        assert lines[0] == f"{tmp_path}/:"
        names = [line.split()[-1] for line in lines[1:]]
        assert ".hidden" in names
        assert "README.md" in names
        assert "main.py" in names

    def test_all_false_hides_dotfiles(
        self, tool: ListFileTool, tmp_path: Path
    ) -> None:
        (tmp_path / "README.md").write_text("")
        (tmp_path / ".hidden").write_text("")

        result = tool.execute(path=str(tmp_path), all=False)

        assert "README.md" in result
        assert ".hidden" not in result

    def test_long_false(self, tool: ListFileTool, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("x")

        result = tool.execute(path=str(tmp_path), long=False)

        assert str(tmp_path) + ":" not in result.rsplit("\n", 1)[0]
        assert "main.py" in result

    def test_pattern_filter(self, tool: ListFileTool, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("")
        (tmp_path / "README.md").write_text("")

        result = tool.execute(path=str(tmp_path), pattern="*.py")

        assert "main.py" in result
        assert "README.md" not in result

    def test_single_file(self, tool: ListFileTool, tmp_path: Path) -> None:
        target = tmp_path / "main.py"
        target.write_text("print('hi')")

        result = tool.execute(path=str(target), long=False)

        assert result == "main.py"

    def test_sanitize_in_output(self, tool: ListFileTool, tmp_path: Path) -> None:
        (tmp_path / "tricky`name`.py").write_text("")

        result = tool.execute(path=str(tmp_path), long=False)

        assert r"\`" in result
