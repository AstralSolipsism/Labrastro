"""Runtime-owned text file mutation service."""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Literal
import uuid


FileChangeKind = Literal["add", "update", "delete", "move"]
FileChangeStatus = Literal["in_progress", "completed", "failed", "declined", "cancelled"]


class FileMutationError(ValueError):
    """Raised when a requested text mutation violates the file protocol."""


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: FileChangeKind
    diff: str
    move_path: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {
            "path": self.path,
            "kind": self.kind,
            "diff": self.diff,
        }
        if self.move_path:
            payload["move_path"] = self.move_path
        return payload


@dataclass(frozen=True, slots=True)
class FileMutationResult:
    status: FileChangeStatus
    changes: tuple[FileChange, ...] = field(default_factory=tuple)
    diff: str = ""
    message: str = ""
    error: str | None = None
    duration_ms: int = 0
    plan_id: str | None = None
    plan_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.error is None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "changes": [item.to_dict() for item in self.changes],
            "diff": self.diff,
            "message": self.message,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
        }


@dataclass(frozen=True, slots=True)
class MutationOperationState:
    kind: FileChangeKind
    path: str
    resolved_path: str
    old_exists: bool
    old_sha256: str | None = None
    old_size: int | None = None
    move_path: str | None = None
    move_resolved_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "path": self.path,
            "resolved_path": self.resolved_path,
            "old_exists": self.old_exists,
        }
        if self.old_sha256 is not None:
            payload["old_sha256"] = self.old_sha256
        if self.old_size is not None:
            payload["old_size"] = self.old_size
        if self.move_path:
            payload["move_path"] = self.move_path
        if self.move_resolved_path:
            payload["move_resolved_path"] = self.move_resolved_path
        return payload


@dataclass(frozen=True, slots=True)
class MutationPlan:
    plan_id: str
    plan_hash: str
    tool_name: str
    workspace_id: str
    execution_target: str
    path_space: str
    operations: tuple[MutationOperationState, ...]
    changes: tuple[FileChange, ...]
    diff: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "tool_name": self.tool_name,
            "workspace_id": self.workspace_id,
            "execution_target": self.execution_target,
            "path_space": self.path_space,
            "operations": [item.to_dict() for item in self.operations],
            "changes": [item.to_dict() for item in self.changes],
            "diff": self.diff,
        }


@dataclass(frozen=True, slots=True)
class _PatchOperation:
    kind: FileChangeKind
    path: str
    move_path: str | None = None
    lines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PlannedPatch:
    operations: tuple[tuple[_PatchOperation, Path, Path | None, str | None, str | None], ...]
    changes: tuple[FileChange, ...]
    diff: str
    plan: MutationPlan


@dataclass(frozen=True, slots=True)
class _PathSnapshot:
    path: Path
    exists: bool
    data: bytes | None


class FileMutationService:
    """Apply validated text mutations inside a single workspace root."""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        execution_target: str = "local_workspace",
        path_space: str = "local_workspace",
    ) -> None:
        self.workspace_root = Path(workspace_root or os.getcwd()).expanduser().resolve()
        self.execution_target = execution_target
        self.path_space = path_space

    def preview_text_patch(self, patch: str) -> FileMutationResult:
        started_at = time.time()
        try:
            planned = self._plan_text_patch(patch)
            return FileMutationResult(
                status="in_progress",
                changes=planned.changes,
                diff=planned.diff,
                message=_format_success_message(list(planned.changes)),
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                plan_hash=planned.plan.plan_hash,
            )
        except Exception as exc:
            return FileMutationResult(
                status="failed",
                error=str(exc),
                message=f"Error: {exc}",
                duration_ms=_elapsed_ms(started_at),
            )

    def apply_text_patch(self, patch: str) -> FileMutationResult:
        started_at = time.time()
        try:
            planned = self._plan_text_patch(patch)
            _apply_planned_operations_transactionally(planned.operations)

            return FileMutationResult(
                status="completed",
                changes=planned.changes,
                diff=planned.diff,
                message=_format_success_message(list(planned.changes)),
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                plan_hash=planned.plan.plan_hash,
            )
        except Exception as exc:
            return FileMutationResult(
                status="failed",
                error=str(exc),
                message=f"Error: {exc}",
                duration_ms=_elapsed_ms(started_at),
            )

    def _plan_text_patch(self, patch: str) -> _PlannedPatch:
        operations = _parse_patch(patch)
        planned: list[tuple[_PatchOperation, Path, Path | None, str | None, str | None]] = []

        for operation in operations:
            path = self._resolve_workspace_path(operation.path)
            move_path = (
                self._resolve_workspace_path(operation.move_path)
                if operation.move_path
                else None
            )
            old_content = _read_existing_text(path)
            if old_content is not None and _is_binary_text(old_content):
                raise FileMutationError(f"{operation.path} appears to be binary")
            new_content = self._planned_content(operation, old_content)
            if new_content is not None and _is_binary_text(new_content):
                raise FileMutationError(f"{operation.path} patch contains binary data")
            if move_path is not None and move_path.exists() and move_path != path:
                raise FileMutationError(f"move target already exists: {operation.move_path}")
            planned.append((operation, path, move_path, old_content, new_content))

        changes = []
        for operation, _path, move_path, old_content, new_content in planned:
            target_label = operation.move_path if move_path is not None else operation.path
            diff = _unified_diff(
                old_content or "",
                new_content or "",
                target_label or operation.path,
            )
            changes.append(
                FileChange(
                    path=operation.path,
                    kind=operation.kind,
                    diff=diff,
                    move_path=operation.move_path,
                )
            )

        diff = "\n".join(change.diff for change in changes if change.diff).strip()
        plan = self._build_plan(
            tool_name="apply_patch",
            planned_operations=tuple(planned),
            changes=tuple(changes),
            diff=diff,
        )
        return _PlannedPatch(
            operations=tuple(planned),
            changes=tuple(changes),
            diff=diff,
            plan=plan,
        )

    def commit_document(self, target_path: str, content: str) -> FileMutationResult:
        started_at = time.time()
        try:
            planned = self._plan_document_commit(
                target_path,
                content,
            )
            _apply_planned_operations_transactionally(planned.operations)
            return FileMutationResult(
                status="completed",
                changes=planned.changes,
                diff=planned.diff,
                message=f"Committed document {target_path}",
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                plan_hash=planned.plan.plan_hash,
            )
        except Exception as exc:
            return FileMutationResult(
                status="failed",
                error=str(exc),
                message=f"Error: {exc}",
                duration_ms=_elapsed_ms(started_at),
            )

    def preview_document_commit(self, target_path: str, content: str) -> FileMutationResult:
        started_at = time.time()
        try:
            planned = self._plan_document_commit(
                target_path,
                content,
            )
            return FileMutationResult(
                status="in_progress",
                changes=planned.changes,
                diff=planned.diff,
                message=f"Preview document commit {target_path}",
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                plan_hash=planned.plan.plan_hash,
            )
        except Exception as exc:
            return FileMutationResult(
                status="failed",
                error=str(exc),
                message=f"Error: {exc}",
                duration_ms=_elapsed_ms(started_at),
            )

    def _plan_document_commit(
        self,
        target_path: str,
        content: str,
    ) -> _PlannedPatch:
        path = self._resolve_workspace_path(target_path)
        if not isinstance(content, str):
            raise FileMutationError("document content must be a string")
        if _is_binary_text(content):
            raise FileMutationError("document content appears to be binary")
        old_content = _read_existing_text(path)
        diff = _unified_diff(old_content or "", content, target_path)
        kind: FileChangeKind = "update" if old_content is not None else "add"
        change = FileChange(
            path=target_path,
            kind=kind,
            diff=diff,
        )
        operation = _PatchOperation(kind=kind, path=target_path)
        operations = ((operation, path, None, old_content, content),)
        plan = self._build_plan(
            tool_name="draft_document_commit",
            planned_operations=operations,
            changes=(change,),
            diff=diff,
        )
        return _PlannedPatch(
            operations=operations,
            changes=(change,),
            diff=diff,
            plan=plan,
        )

    def _build_plan(
        self,
        *,
        tool_name: str,
        planned_operations: tuple[
            tuple[_PatchOperation, Path, Path | None, str | None, str | None], ...
        ],
        changes: tuple[FileChange, ...],
        diff: str,
    ) -> MutationPlan:
        operation_states = tuple(
            _operation_state(operation, path, move_path, old_content)
            for operation, path, move_path, old_content, _new_content in planned_operations
        )
        plan_id = f"mutation-{uuid.uuid4().hex}"
        hash_payload = {
            "tool_name": tool_name,
            "workspace_id": str(self.workspace_root),
            "execution_target": self.execution_target,
            "path_space": self.path_space,
            "operations": [item.to_dict() for item in operation_states],
            "changes": [item.to_dict() for item in changes],
            "diff": diff,
        }
        plan_hash = _stable_sha256(hash_payload)
        return MutationPlan(
            plan_id=plan_id,
            plan_hash=plan_hash,
            tool_name=tool_name,
            workspace_id=str(self.workspace_root),
            execution_target=self.execution_target,
            path_space=self.path_space,
            operations=operation_states,
            changes=changes,
            diff=diff,
        )

    def _resolve_workspace_path(self, file_path: str | None) -> Path:
        if not isinstance(file_path, str) or not file_path.strip():
            raise FileMutationError("file path must be a non-empty string")
        candidate = Path(file_path)
        if candidate.is_absolute():
            raise FileMutationError(f"absolute paths are not allowed: {file_path}")
        if any(part == ".." for part in candidate.parts):
            raise FileMutationError(f"path traversal is not allowed: {file_path}")
        resolved = (self.workspace_root / candidate).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise FileMutationError(f"path escapes workspace root: {file_path}") from exc
        return resolved

    def _planned_content(self, operation: _PatchOperation, old_content: str | None) -> str | None:
        if operation.kind == "add":
            if old_content is not None:
                raise FileMutationError(f"file already exists: {operation.path}")
            return _content_from_added_lines(operation.lines)
        if operation.kind == "delete":
            if old_content is None:
                raise FileMutationError(f"file does not exist: {operation.path}")
            return None
        if operation.kind in {"update", "move"}:
            if old_content is None:
                raise FileMutationError(f"file does not exist: {operation.path}")
            if operation.kind == "move" and not operation.lines:
                return old_content
            return _apply_update_hunks(old_content, operation.lines, operation.path)
        raise FileMutationError(f"unsupported patch operation: {operation.kind}")


def _parse_patch(patch: str) -> tuple[_PatchOperation, ...]:
    if not isinstance(patch, str) or not patch.strip():
        raise FileMutationError("patch must be a non-empty string")
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise FileMutationError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise FileMutationError("patch must end with *** End Patch")

    operations: list[_PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: ") :].strip()
            body, index = _collect_until_next_section(lines, index + 1)
            operations.append(_PatchOperation(kind="add", path=path, lines=tuple(body)))
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: ") :].strip()
            operations.append(_PatchOperation(kind="delete", path=path))
            index += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: ") :].strip()
            index += 1
            move_path = None
            if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                move_path = lines[index][len("*** Move to: ") :].strip()
                index += 1
            body, index = _collect_until_next_section(lines, index)
            operations.append(
                _PatchOperation(
                    kind="move" if move_path else "update",
                    path=path,
                    move_path=move_path,
                    lines=tuple(body),
                )
            )
            continue
        raise FileMutationError(f"unexpected patch line: {line}")
    if not operations:
        raise FileMutationError("patch contains no file operations")
    return tuple(operations)


def _collect_until_next_section(lines: list[str], index: int) -> tuple[list[str], int]:
    body: list[str] = []
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        body.append(lines[index])
        index += 1
    return body, index


def _content_from_added_lines(lines: tuple[str, ...]) -> str:
    content_lines: list[str] = []
    for line in lines:
        if not line.startswith("+"):
            raise FileMutationError("Add File lines must start with +")
        content_lines.append(line[1:])
    return "\n".join(content_lines) + ("\n" if content_lines else "")


def _apply_update_hunks(old_content: str, lines: tuple[str, ...], path: str) -> str:
    current = old_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if current and current[-1] == "":
        current = current[:-1]
    hunks = _split_hunks(lines)
    if not hunks:
        raise FileMutationError(f"Update File requires at least one hunk: {path}")
    cursor = 0
    for hunk in hunks:
        old_segment: list[str] = []
        new_segment: list[str] = []
        for line in hunk:
            if not line:
                raise FileMutationError("hunk lines must start with space, -, or +")
            prefix = line[0]
            text = line[1:]
            if prefix == " ":
                old_segment.append(text)
                new_segment.append(text)
            elif prefix == "-":
                old_segment.append(text)
            elif prefix == "+":
                new_segment.append(text)
            else:
                raise FileMutationError("hunk lines must start with space, -, or +")
        if not old_segment:
            raise FileMutationError("update hunk must include context or removed lines")
        match = _find_unique_segment(current, old_segment, cursor)
        current[match : match + len(old_segment)] = new_segment
        cursor = match + len(new_segment)
    return "\n".join(current) + ("\n" if old_content.endswith(("\n", "\r")) else "")


def _split_hunks(lines: tuple[str, ...]) -> list[list[str]]:
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("@@"):
            if current is not None:
                hunks.append(current)
            current = []
            continue
        if current is None:
            if not line.strip():
                continue
            raise FileMutationError("Update File hunks must start with @@")
        current.append(line)
    if current is not None:
        hunks.append(current)
    return hunks


def _find_unique_segment(lines: list[str], segment: list[str], start: int) -> int:
    matches: list[int] = []
    for index in range(start, len(lines) - len(segment) + 1):
        if lines[index : index + len(segment)] == segment:
            matches.append(index)
    if not matches and start:
        for index in range(0, start):
            if lines[index : index + len(segment)] == segment:
                matches.append(index)
    if not matches:
        raise FileMutationError("patch context does not match file")
    if len(matches) > 1:
        raise FileMutationError("patch context matches multiple locations")
    return matches[0]


def _read_existing_text(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise FileMutationError(f"{path} is not a file")
    with path.open("r", errors="surrogateescape", newline="") as handle:
        return handle.read()


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, newline="")


def _apply_planned_operations_transactionally(
    operations: tuple[
        tuple[_PatchOperation, Path, Path | None, str | None, str | None], ...
    ],
) -> None:
    snapshots = _snapshot_touched_paths(operations)
    try:
        for operation, path, move_path, _old_content, new_content in operations:
            if operation.kind == "delete":
                path.unlink()
                continue
            if operation.kind == "move":
                assert move_path is not None
                assert new_content is not None
                move_path.parent.mkdir(parents=True, exist_ok=True)
                _write_text(move_path, new_content)
                if path != move_path and path.exists():
                    path.unlink()
                continue
            assert new_content is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_text(path, new_content)
    except Exception:
        _restore_snapshots(snapshots)
        raise


def _snapshot_touched_paths(
    operations: tuple[
        tuple[_PatchOperation, Path, Path | None, str | None, str | None], ...
    ],
) -> tuple[_PathSnapshot, ...]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for _operation, path, move_path, _old_content, _new_content in operations:
        for candidate in (path, move_path):
            if candidate is None or candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)

    snapshots: list[_PathSnapshot] = []
    for path in paths:
        if path.exists():
            if not path.is_file():
                raise FileMutationError(f"{path} is not a file")
            snapshots.append(_PathSnapshot(path=path, exists=True, data=path.read_bytes()))
        else:
            snapshots.append(_PathSnapshot(path=path, exists=False, data=None))
    return tuple(snapshots)


def _restore_snapshots(snapshots: tuple[_PathSnapshot, ...]) -> None:
    for snapshot in reversed(snapshots):
        if snapshot.exists:
            snapshot.path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.path.write_bytes(snapshot.data or b"")
            continue
        if snapshot.path.exists():
            if not snapshot.path.is_file():
                continue
            snapshot.path.unlink()


def _operation_state(
    operation: _PatchOperation,
    path: Path,
    move_path: Path | None,
    old_content: str | None,
) -> MutationOperationState:
    old_bytes = path.read_bytes() if old_content is not None and path.exists() else None
    return MutationOperationState(
        kind=operation.kind,
        path=operation.path,
        move_path=operation.move_path,
        resolved_path=str(path),
        move_resolved_path=str(move_path) if move_path is not None else None,
        old_exists=old_content is not None,
        old_sha256=hashlib.sha256(old_bytes).hexdigest()
        if old_bytes is not None
        else None,
        old_size=len(old_bytes) if old_bytes is not None else None,
    )


def _stable_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_binary_text(value: str) -> bool:
    return "\x00" in value


def _unified_diff(old: str, new: str, filename: str, context: int = 3) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    return "".join(diff)


def _format_success_message(changes: list[FileChange]) -> str:
    if not changes:
        return "No file changes"
    counts = {"add": 0, "update": 0, "delete": 0, "move": 0}
    for change in changes:
        counts[change.kind] += 1
    details = ", ".join(f"{key}={value}" for key, value in counts.items() if value)
    return f"Applied patch ({details})"


def _elapsed_ms(started_at: float) -> int:
    return int((time.time() - started_at) * 1000)
