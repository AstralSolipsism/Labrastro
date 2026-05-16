"""LSP diagnostics and compact rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFORMATION = 3
SEVERITY_HINT = 4

_SEVERITY_LABELS = {
    SEVERITY_ERROR: "ERROR",
    SEVERITY_WARNING: "WARNING",
    SEVERITY_INFORMATION: "INFO",
    SEVERITY_HINT: "HINT",
}


@dataclass(slots=True)
class Diagnostic:
    """A single diagnostic using 1-based line and character positions."""

    line: int
    character: int
    message: str
    severity: int = SEVERITY_ERROR
    code: str | None = None

    @property
    def severity_label(self) -> str:
        return _SEVERITY_LABELS.get(self.severity, "UNKNOWN")

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    @property
    def is_warning(self) -> bool:
        return self.severity == SEVERITY_WARNING


@dataclass(slots=True)
class DiagnosticBlock:
    """Diagnostics for one workspace-relative file."""

    file_path: str
    items: list[Diagnostic] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.items


def diagnostic_from_lsp(raw: dict) -> Diagnostic:
    """Convert an LSP diagnostic object into the local compact model."""
    start = (raw.get("range") or {}).get("start") or {}
    severity = raw.get("severity", SEVERITY_ERROR)
    try:
        severity = int(severity)
    except (TypeError, ValueError):
        severity = SEVERITY_ERROR
    code = raw.get("code")
    return Diagnostic(
        line=int(start.get("line", 0)) + 1,
        character=int(start.get("character", 0)) + 1,
        message=str(raw.get("message", "")),
        severity=severity,
        code=str(code) if code is not None else None,
    )


def render_blocks(
    blocks: list[DiagnosticBlock],
    *,
    max_diagnostics: int = 20,
    include_warnings: bool = False,
) -> str | None:
    """Render diagnostic blocks into XML-like context."""
    parts: list[str] = []
    for block in blocks:
        items = list(block.items)
        if not include_warnings:
            items = [item for item in items if item.is_error]
        if not items:
            continue
        items = sorted(items, key=lambda item: (item.severity, item.line, item.character))
        lines = [f'<diagnostics file="{escape(block.file_path, quote=True)}">']
        for item in items[:max_diagnostics]:
            message = escape(item.message.splitlines()[0], quote=False)
            lines.append(
                f"  {item.severity_label} [{item.line}:{item.character}] {message}"
            )
        lines.append("</diagnostics>")
        parts.append("\n".join(lines))
    if not parts:
        return None
    return "\n\n".join(parts)
