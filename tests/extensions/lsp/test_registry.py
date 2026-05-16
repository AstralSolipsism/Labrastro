from pathlib import Path

from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    detect_language,
    get_server_command,
    resolve_workspace_root,
)


def test_detect_language_by_extension() -> None:
    assert detect_language("main.py") is LanguageId.PYTHON
    assert detect_language("main.go") is LanguageId.GO
    assert detect_language("README.md") is None


def test_resolve_workspace_root_uses_marker(tmp_path: Path) -> None:
    project = tmp_path / "project"
    src = project / "pkg"
    src.mkdir(parents=True)
    (project / "go.mod").write_text("module example\n")
    target = src / "main.go"
    target.write_text("package main\n")

    assert resolve_workspace_root(target, LanguageId.GO, cwd=tmp_path) == project


def test_python_server_command_uses_pyright_langserver() -> None:
    command, args = get_server_command(LanguageId.PYTHON)

    assert command == "npx"
    assert "pyright-langserver" in args
