"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolMutationPreviewState:
    plan_id: str | None = None
    plan_hash: str | None = None
    operations: list[dict[str, Any]] = field(default_factory=list)
    resolved_path: str | None = None
    old_sha256: str | None = None
    old_exists: bool | None = None
    old_size: int | None = None

    def is_empty(self) -> bool:
        return (
            self.plan_id is None
            and self.plan_hash is None
            and not self.operations
            and self.resolved_path is None
            and self.old_sha256 is None
            and self.old_exists is None
            and self.old_size is None
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.plan_id is not None:
            payload["plan_id"] = self.plan_id
        if self.plan_hash is not None:
            payload["plan_hash"] = self.plan_hash
        if self.operations:
            payload["operations"] = [dict(item) for item in self.operations]
        if self.resolved_path is not None:
            payload["resolved_path"] = self.resolved_path
        if self.old_sha256 is not None:
            payload["old_sha256"] = self.old_sha256
        if self.old_exists is not None:
            payload["old_exists"] = self.old_exists
        if self.old_size is not None:
            payload["old_size"] = self.old_size
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ToolMutationPreviewState | None":
        if not isinstance(d, dict) or not d:
            return None
        state = cls(
            plan_id=str(d["plan_id"]) if d.get("plan_id") is not None else None,
            plan_hash=str(d["plan_hash"]) if d.get("plan_hash") is not None else None,
            operations=[
                dict(item)
                for item in d.get("operations", [])
                if isinstance(item, dict)
            ],
            resolved_path=(
                str(d["resolved_path"]) if d.get("resolved_path") is not None else None
            ),
            old_sha256=str(d["old_sha256"]) if d.get("old_sha256") is not None else None,
            old_exists=(
                bool(d["old_exists"]) if d.get("old_exists") is not None else None
            ),
            old_size=int(d["old_size"]) if d.get("old_size") is not None else None,
        )
        return None if state.is_empty() else state

    @classmethod
    def from_preview(
        cls, preview: "ToolPreviewResult"
    ) -> "ToolMutationPreviewState | None":
        return cls.from_dict(
            {
                "resolved_path": preview.resolved_path,
                "old_sha256": preview.old_sha256,
                "old_exists": preview.old_exists,
                "old_size": preview.old_size,
                "plan_id": preview.meta.get("plan_id"),
                "plan_hash": preview.meta.get("plan_hash"),
                "operations": preview.meta.get("operations", []),
            }
        )


@dataclass
class ExecToolRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30
    expected_state: ToolMutationPreviewState | None = None
    tool_call_id: str | None = None
    permission_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
            "expected_state": (
                self.expected_state.to_dict() if self.expected_state is not None else {}
            ),
            "tool_call_id": self.tool_call_id,
            "permission_context": dict(self.permission_context or {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolRequest":
        return cls(
            tool_name=d["tool_name"],
            args=d.get("args", {}),
            cwd=d.get("cwd"),
            timeout_sec=d.get("timeout_sec", 30),
            tool_call_id=d.get("tool_call_id"),
            expected_state=ToolMutationPreviewState.from_dict(d.get("expected_state")),
            permission_context=d.get("permission_context", {})
            if isinstance(d.get("permission_context", {}), dict)
            else {},
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
# Stream chunk transport
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
    "ToolMutationPreviewState",
    "ExecToolRequest",
    "ExecToolResult",
    "ToolPreviewRequest",
    "ToolPreviewResult",
    "ToolStreamChunk",
    "CleanupRequest",
    "CleanupResult",
]
