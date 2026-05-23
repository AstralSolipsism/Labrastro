"""Content search with regex support."""

from __future__ import annotations

import re
from pathlib import Path

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
}

_BINARY_SAMPLE_BYTES = 8192
_MAX_MATCH_LINE_CHARS = 4096


@register_tool
class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with regex. "
        "Returns matching lines with file path and line number."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: cwd)",
            },
            "include": {
                "type": "string",
                "description": "Only search files matching this glob (e.g. '*.py')",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(self, pattern: str, path: str = ".", include: str | None = None) -> str:
        return self.run_backend(pattern=pattern, path=path, include=include)

    @backend_handler("remote_relay")
    def _execute_remote(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern must be a non-empty string"
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        if include is not None and not isinstance(include, str):
            return "Error: include must be a string when provided"
        return self.backend.exec_tool(
            "grep", {"pattern": pattern, "path": path, "include": include}
        )

    @backend_handler("local")
    def _execute_local(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex: {e}"

        base = Path(path).expanduser().resolve()
        if not base.exists():
            return f"Error: {path} not found"

        if base.is_file():
            files = [base]
        else:
            files = self._walk(base, include)

        matches = []
        skipped_binary = 0
        for fp in files:
            text, is_binary = self._read_searchable_text(fp)
            if is_binary:
                if base.is_file():
                    return f"Skipped binary file: {fp}"
                skipped_binary += 1
                continue
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(self._format_match(fp, lineno, line))
                    if len(matches) >= 200:
                        matches.append("... (200 match limit reached)")
                        return "\n".join(matches)

        if skipped_binary:
            matches.append(f"Skipped {skipped_binary} binary file(s).")

        return "\n".join(matches) if matches else "No matches found."

    @staticmethod
    def _walk(root: Path, include: str | None) -> list[Path]:
        results = []
        for item in root.rglob(include or "*"):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if item.is_file():
                results.append(item)
            if len(results) >= 5000:
                break
        return results

    @staticmethod
    def _read_searchable_text(path: Path) -> tuple[str | None, bool]:
        try:
            with path.open("rb") as fh:
                sample = fh.read(_BINARY_SAMPLE_BYTES)
                if b"\x00" in sample:
                    return None, True
                rest = fh.read()
        except OSError:
            return None, False

        if b"\x00" in rest:
            return None, True
        return (sample + rest).decode("utf-8", errors="replace"), False

    @staticmethod
    def _format_match(path: Path, lineno: int, line: str) -> str:
        text = line.rstrip()
        if len(text) > _MAX_MATCH_LINE_CHARS:
            text = text[:_MAX_MATCH_LINE_CHARS].rstrip() + "... (line truncated)"
        return f"{path}:{lineno}: {text}"
