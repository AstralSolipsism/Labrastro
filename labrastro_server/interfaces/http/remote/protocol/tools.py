"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class ExecToolRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30
    expected_state: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
            "expected_state": self.expected_state,
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolRequest":
        return cls(
            tool_name=d["tool_name"],
            args=d.get("args", {}),
            cwd=d.get("cwd"),
            timeout_sec=d.get("timeout_sec", 30),
            tool_call_id=d.get("tool_call_id"),
            expected_state=(
                dict(d.get("expected_state", {}))
                if isinstance(d.get("expected_state", {}), dict)
                else {}
            ),
        )

@dataclass
class ExecToolResult:
    ok: bool
    result: str = ""
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolResult":
        return cls(
            ok=d["ok"],
            result=d.get("result", ""),
            error_code=d.get("error_code"),
            error_message=d.get("error_message"),
            meta=d.get("meta", {}),
        )

@dataclass
class ToolPreviewRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolPreviewRequest":
        return cls(
            tool_name=d["tool_name"],
            args=d.get("args", {}),
            cwd=d.get("cwd"),
            timeout_sec=d.get("timeout_sec", 30),
        )

@dataclass
class ToolPreviewResult:
    ok: bool
    sections: list[dict[str, Any]] = field(default_factory=list)
    resolved_path: str | None = None
    old_sha256: str | None = None
    old_exists: bool | None = None
    old_size: int | None = None
    old_mtime_ns: int | None = None
    diff: str = ""
    original_text: str | None = None
    modified_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sections": self.sections,
            "resolved_path": self.resolved_path,
            "old_sha256": self.old_sha256,
            "old_exists": self.old_exists,
            "old_size": self.old_size,
            "old_mtime_ns": self.old_mtime_ns,
            "diff": self.diff,
            "original_text": self.original_text,
            "modified_text": self.modified_text,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolPreviewResult":
        return cls(
            ok=bool(d.get("ok", False)),
            sections=[
                dict(item)
                for item in d.get("sections", [])
                if isinstance(item, dict)
            ],
            resolved_path=d.get("resolved_path"),
            old_sha256=d.get("old_sha256"),
            old_exists=(
                bool(d["old_exists"]) if d.get("old_exists") is not None else None
            ),
            old_size=int(d["old_size"]) if d.get("old_size") is not None else None,
            old_mtime_ns=(
                int(d["old_mtime_ns"]) if d.get("old_mtime_ns") is not None else None
            ),
            diff=str(d.get("diff", "")),
            original_text=d.get("original_text"),
            modified_text=d.get("modified_text"),
            error_code=d.get("error_code"),
            error_message=d.get("error_message"),
            meta=d.get("meta", {}) if isinstance(d.get("meta", {}), dict) else {},
        )


# ---------------------------------------------------------------------------
# Stream chunk (MVP: shell only if needed; struct kept for forward-compat)
# ---------------------------------------------------------------------------

@dataclass
class ToolStreamChunk:
    chunk_type: str  # "stdout" | "stderr" | "exit"
    data: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_type": self.chunk_type,
            "data": self.data,
            "meta": self.meta,
            "tool_call_id": self.tool_call_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolStreamChunk":
        return cls(
            chunk_type=d["chunk_type"],
            data=d.get("data", ""),
            meta=d.get("meta", {}) if isinstance(d.get("meta", {}), dict) else {},
            tool_call_id=d.get("tool_call_id"),
        )


# ---------------------------------------------------------------------------
# Disconnect / Cleanup
# ---------------------------------------------------------------------------

@dataclass
class CleanupRequest:
    pass

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupRequest":
        return cls()

@dataclass
class CleanupResult:
    ok: bool
    removed_items: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "removed_items": self.removed_items,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupResult":
        return cls(
            ok=d["ok"],
            removed_items=d.get("removed_items", []),
            error_message=d.get("error_message"),
        )


# ---------------------------------------------------------------------------
# Generic error
# ---------------------------------------------------------------------------

__all__ = [
    "ExecToolRequest",
    "ExecToolResult",
    "ToolPreviewRequest",
    "ToolPreviewResult",
    "ToolStreamChunk",
    "CleanupRequest",
    "CleanupResult",
]
