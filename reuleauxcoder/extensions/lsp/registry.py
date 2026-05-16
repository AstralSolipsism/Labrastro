"""Language detection, server commands, and workspace-root rules."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class LanguageId(str, Enum):
    PYTHON = "python"
    RUST = "rust"
    GO = "go"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    C = "c"
    CPP = "cpp"
    BASH = "bash"
    YAML = "yaml"


_EXT_TO_LANGUAGE: dict[str, LanguageId] = {
    ".py": LanguageId.PYTHON,
    ".pyi": LanguageId.PYTHON,
    ".rs": LanguageId.RUST,
    ".go": LanguageId.GO,
    ".ts": LanguageId.TYPESCRIPT,
    ".tsx": LanguageId.TYPESCRIPT,
    ".js": LanguageId.JAVASCRIPT,
    ".jsx": LanguageId.JAVASCRIPT,
    ".mjs": LanguageId.JAVASCRIPT,
    ".cjs": LanguageId.JAVASCRIPT,
    ".c": LanguageId.C,
    ".h": LanguageId.C,
    ".cpp": LanguageId.CPP,
    ".cc": LanguageId.CPP,
    ".cxx": LanguageId.CPP,
    ".hpp": LanguageId.CPP,
    ".hxx": LanguageId.CPP,
    ".hh": LanguageId.CPP,
    ".ino": LanguageId.CPP,
    ".pde": LanguageId.CPP,
    ".sh": LanguageId.BASH,
    ".bash": LanguageId.BASH,
    ".yaml": LanguageId.YAML,
    ".yml": LanguageId.YAML,
}

_LANGUAGE_ID_STRINGS: dict[LanguageId, str] = {
    LanguageId.PYTHON: "python",
    LanguageId.RUST: "rust",
    LanguageId.GO: "go",
    LanguageId.TYPESCRIPT: "typescript",
    LanguageId.JAVASCRIPT: "javascript",
    LanguageId.C: "c",
    LanguageId.CPP: "cpp",
    LanguageId.BASH: "shellscript",
    LanguageId.YAML: "yaml",
}

_SERVER_COMMANDS: dict[LanguageId, tuple[str, list[str]]] = {
    LanguageId.PYTHON: (
        "npx",
        ["-y", "--package", "pyright", "pyright-langserver", "--stdio"],
    ),
    LanguageId.RUST: ("rust-analyzer", []),
    LanguageId.GO: ("gopls", ["serve"]),
    LanguageId.TYPESCRIPT: (
        "npx",
        [
            "-y",
            "--package",
            "typescript",
            "--package",
            "typescript-language-server",
            "typescript-language-server",
            "--stdio",
        ],
    ),
    LanguageId.JAVASCRIPT: (
        "npx",
        [
            "-y",
            "--package",
            "typescript",
            "--package",
            "typescript-language-server",
            "typescript-language-server",
            "--stdio",
        ],
    ),
    LanguageId.C: ("clangd", []),
    LanguageId.CPP: ("clangd", []),
    LanguageId.BASH: ("npx", ["-y", "bash-language-server", "start"]),
    LanguageId.YAML: ("npx", ["-y", "yaml-language-server", "--stdio"]),
}

_ROOT_MARKERS: dict[LanguageId, list[str]] = {
    LanguageId.RUST: ["Cargo.toml"],
    LanguageId.GO: ["go.mod"],
    LanguageId.PYTHON: ["pyproject.toml", "setup.py", "setup.cfg"],
    LanguageId.TYPESCRIPT: ["tsconfig.json", "package.json"],
    LanguageId.JAVASCRIPT: ["package.json"],
    LanguageId.C: ["compile_commands.json", "Makefile", "CMakeLists.txt"],
    LanguageId.CPP: ["compile_commands.json", "Makefile", "CMakeLists.txt"],
}


def detect_language(file_path: str | Path) -> LanguageId | None:
    return _EXT_TO_LANGUAGE.get(Path(file_path).suffix.lower())


def get_language_id_string(language: LanguageId) -> str:
    return _LANGUAGE_ID_STRINGS.get(language, "")


def get_server_command(language: LanguageId) -> tuple[str, list[str]]:
    return _SERVER_COMMANDS.get(language, ("", []))


def get_root_markers(language: LanguageId) -> list[str]:
    return list(_ROOT_MARKERS.get(language, []))


def resolve_workspace_root(
    file_path: str | Path,
    language: LanguageId,
    *,
    cwd: Path | None = None,
    override: str | None = None,
) -> Path:
    """Resolve a language-server workspace root."""
    base_cwd = (cwd or Path.cwd()).resolve()
    if override:
        override_path = Path(override)
        if not override_path.is_absolute():
            override_path = base_cwd / override_path
        return override_path.resolve()

    path = Path(file_path)
    if not path.is_absolute():
        path = base_cwd / path
    current = path.resolve().parent
    for marker in get_root_markers(language):
        probe = current
        while True:
            if (probe / marker).exists():
                return probe
            if probe == probe.parent:
                break
            probe = probe.parent
    return base_cwd


def iter_supported_extensions() -> list[str]:
    return sorted(_EXT_TO_LANGUAGE)


def iter_supported_languages() -> list[LanguageId]:
    return sorted(_SERVER_COMMANDS, key=lambda item: item.value)
