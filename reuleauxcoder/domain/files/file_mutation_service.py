"""Runtime-owned text file mutation service."""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
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
    candidate_hash: str | None = None
    preview_identity: dict[str, object] = field(default_factory=dict)
    approved_save_candidate: dict[str, object] = field(default_factory=dict)

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
            "candidate_hash": self.candidate_hash,
            "preview_identity": dict(self.preview_identity),
            "approved_save_candidate": dict(self.approved_save_candidate),
        }


@dataclass(frozen=True, slots=True)
class MutationOperationDescriptor:
    kind: FileChangeKind
    path: str
    resolved_path: str
    move_path: str | None = None
    move_resolved_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "path": self.path,
            "resolved_path": self.resolved_path,
        }
        if self.move_path:
            payload["move_path"] = self.move_path
        if self.move_resolved_path:
            payload["move_resolved_path"] = self.move_resolved_path
        return payload


@dataclass(frozen=True, slots=True)
class MutationPlan:
    plan_id: str
    tool_name: str
    workspace_id: str
    execution_target: str
    path_space: str
    operations: tuple[MutationOperationDescriptor, ...]
    changes: tuple[FileChange, ...]
    diff: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
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
            candidate = _approved_save_candidate_from_plan(
                planned,
                args={"patch": patch},
            )
            return FileMutationResult(
                status="in_progress",
                changes=planned.changes,
                diff=planned.diff,
                message=_format_success_message(list(planned.changes)),
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                candidate_hash=str(candidate.get("candidate_hash") or "") or None,
                preview_identity=_dict_value(candidate.get("preview_identity")),
                approved_save_candidate=candidate,
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
            candidate = _approved_save_candidate_from_plan(
                planned,
                args={"patch": patch},
            )

            return FileMutationResult(
                status="completed",
                changes=planned.changes,
                diff=planned.diff,
                message=_format_success_message(list(planned.changes)),
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                candidate_hash=str(candidate.get("candidate_hash") or "") or None,
                preview_identity=_dict_value(candidate.get("preview_identity")),
                approved_save_candidate=candidate,
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
            candidate = _approved_save_candidate_from_plan(
                planned,
                args={"target_path": target_path, "content": content},
            )
            return FileMutationResult(
                status="completed",
                changes=planned.changes,
                diff=planned.diff,
                message=f"Committed document {target_path}",
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                candidate_hash=str(candidate.get("candidate_hash") or "") or None,
                preview_identity=_dict_value(candidate.get("preview_identity")),
                approved_save_candidate=candidate,
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
            candidate = _approved_save_candidate_from_plan(
                planned,
                args={"target_path": target_path, "content": content},
            )
            return FileMutationResult(
                status="in_progress",
                changes=planned.changes,
                diff=planned.diff,
                message=f"Preview document commit {target_path}",
                duration_ms=_elapsed_ms(started_at),
                plan_id=planned.plan.plan_id,
                candidate_hash=str(candidate.get("candidate_hash") or "") or None,
                preview_identity=_dict_value(candidate.get("preview_identity")),
                approved_save_candidate=candidate,
            )
        except Exception as exc:
            return FileMutationResult(
                status="failed",
                error=str(exc),
                message=f"Error: {exc}",
                duration_ms=_elapsed_ms(started_at),
            )

    def save_candidate(self, candidate: dict[str, object]) -> FileMutationResult:
        started_at = time.time()
        try:
            _validate_save_candidate(candidate)
            operations = _candidate_operations(candidate)
            planned_operations = []
            for operation in operations:
                path = self._resolve_workspace_path(operation["path"])
                move_path = (
                    self._resolve_workspace_path(operation.get("move_path"))
                    if operation.get("move_path")
                    else None
                )
                patch_operation = _PatchOperation(
                    kind=operation["kind"],
                    path=operation["path"],
                    move_path=operation.get("move_path"),
                )
                planned_operations.append(
                    (
                        patch_operation,
                        path,
                        move_path,
                        None,
                        operation.get("new_content"),
                    )
                )
            _apply_planned_operations_transactionally(tuple(planned_operations))
            changes = _candidate_changes(candidate)
            diff = str(candidate.get("diff") or "")
            preview_identity = _dict_value(candidate.get("preview_identity"))
            return FileMutationResult(
                status="completed",
                changes=changes,
                diff=diff,
                message=_format_success_message(list(changes)),
                duration_ms=_elapsed_ms(started_at),
                plan_id=str(candidate.get("plan_id") or "") or None,
                candidate_hash=str(candidate.get("candidate_hash") or "") or None,
                preview_identity=preview_identity,
                approved_save_candidate=dict(candidate),
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
        if path.exists():
            raise FileMutationError(
                "draft document target already exists; "
                f"use apply_patch to modify existing files: {target_path}"
            )
        old_content = None
        diff = _unified_diff("", content, target_path)
        kind: FileChangeKind = "add"
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
        operation_descriptors = tuple(
            _operation_descriptor(operation, path, move_path)
            for operation, path, move_path, _old_content, _new_content in planned_operations
        )
        plan_id = f"mutation-{uuid.uuid4().hex}"
        return MutationPlan(
            plan_id=plan_id,
            tool_name=tool_name,
            workspace_id=str(self.workspace_root),
            execution_target=self.execution_target,
            path_space=self.path_space,
            operations=operation_descriptors,
            changes=changes,
            diff=diff,
        )

    def _resolve_workspace_path(self, file_path: str | None) -> Path:
        if not isinstance(file_path, str) or not file_path.strip():
            raise FileMutationError("file path must be a non-empty string")
        candidate = Path(file_path)
        if (
            candidate.is_absolute()
            or PurePosixPath(file_path).is_absolute()
            or PureWindowsPath(file_path).is_absolute()
            or file_path.startswith(("/", "\\"))
        ):
            raise FileMutationError(f"absolute paths are not allowed: {file_path}")
        if PureWindowsPath(file_path).drive:
            raise FileMutationError(
                f"drive-relative paths are not allowed: {file_path}"
            )
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


def _approved_save_candidate_from_plan(
    planned: _PlannedPatch,
    *,
    args: dict[str, object],
) -> dict[str, object]:
    plan = planned.plan
    operations: list[dict[str, object]] = []
    for operation, _path, _move_path, _old_content, new_content in planned.operations:
        item: dict[str, object] = {
            "kind": operation.kind,
            "path": operation.path,
        }
        if operation.move_path:
            item["move_path"] = operation.move_path
        if operation.kind != "delete":
            item["new_content"] = new_content or ""
        operations.append(item)
    args_hash = _stable_sha256(args)
    identity_payload: dict[str, object] = {
        "plan_id": plan.plan_id,
        "tool_name": plan.tool_name,
        "workspace_id": plan.workspace_id,
        "execution_target": plan.execution_target,
        "path_space": plan.path_space,
        "args_hash": args_hash,
        "operations": [
            {
                key: value
                for key, value in operation.items()
                if key in {"kind", "path", "move_path"}
            }
            for operation in operations
        ],
    }
    candidate_hash = _stable_sha256(identity_payload)
    base: dict[str, object] = {
        "plan_id": plan.plan_id,
        "tool_name": plan.tool_name,
        "workspace_id": plan.workspace_id,
        "execution_target": plan.execution_target,
        "path_space": plan.path_space,
        "args_hash": args_hash,
        "operations": operations,
        "changes": [change.to_dict() for change in planned.changes],
        "diff": planned.diff,
    }
    preview_identity = {
        "plan_id": plan.plan_id,
        "candidate_hash": candidate_hash,
        "tool_name": plan.tool_name,
        "workspace_id": plan.workspace_id,
        "execution_target": plan.execution_target,
        "path_space": plan.path_space,
        "args_hash": args_hash,
    }
    return {
        **base,
        "candidate_hash": candidate_hash,
        "preview_identity": preview_identity,
    }


def _validate_save_candidate(candidate: dict[str, object]) -> None:
    if not isinstance(candidate, dict) or not candidate:
        raise FileMutationError("approved_save_candidate is required")
    preview_identity = candidate.get("preview_identity")
    if not isinstance(preview_identity, dict) or not preview_identity:
        raise FileMutationError("approved_save_candidate preview_identity is required")
    for key in (
        "plan_id",
        "candidate_hash",
        "tool_name",
        "workspace_id",
        "execution_target",
        "path_space",
        "args_hash",
    ):
        if not str(preview_identity.get(key) or "").strip():
            raise FileMutationError(
                f"approved_save_candidate preview_identity.{key} is required"
            )


def _candidate_operations(candidate: dict[str, object]) -> list[dict[str, str]]:
    if not isinstance(candidate, dict) or not candidate:
        raise FileMutationError("approved_save_candidate is required")
    raw_operations = candidate.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise FileMutationError("approved_save_candidate operations are required")
    operations: list[dict[str, str]] = []
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise FileMutationError("approved_save_candidate operation must be an object")
        kind = str(raw.get("kind") or "")
        if kind not in {"add", "update", "delete", "move"}:
            raise FileMutationError(f"unsupported approved_save_candidate operation: {kind}")
        path = str(raw.get("path") or "").strip()
        if not path:
            raise FileMutationError("approved_save_candidate operation path is required")
        item: dict[str, str] = {"kind": kind, "path": path}
        move_path = str(raw.get("move_path") or "").strip()
        if move_path:
            item["move_path"] = move_path
        if kind != "delete":
            if "new_content" not in raw:
                raise FileMutationError(
                    "approved_save_candidate operation new_content is required"
                )
            item["new_content"] = str(raw.get("new_content") or "")
        operations.append(item)
    return operations


def _candidate_changes(candidate: dict[str, object]) -> tuple[FileChange, ...]:
    raw_changes = candidate.get("changes")
    if not isinstance(raw_changes, list):
        return ()
    changes: list[FileChange] = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        if kind not in {"add", "update", "delete", "move"}:
            continue
        path = str(raw.get("path") or "")
        if not path:
            continue
        changes.append(
            FileChange(
                path=path,
                kind=kind,
                diff=str(raw.get("diff") or ""),
                move_path=str(raw.get("move_path") or "") or None,
            )
        )
    return tuple(changes)


def _dict_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


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
                _delete_file_if_present(path)
                continue
            if operation.kind == "move":
                assert move_path is not None
                assert new_content is not None
                move_path.parent.mkdir(parents=True, exist_ok=True)
                _write_text(move_path, new_content)
                if path != move_path:
                    _delete_file_if_present(path)
                continue
            assert new_content is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_text(path, new_content)
    except Exception:
        _restore_snapshots(snapshots)
        raise


def _delete_file_if_present(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise FileMutationError(f"{path} is not a file")
    path.unlink()


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


def _operation_descriptor(
    operation: _PatchOperation,
    path: Path,
    move_path: Path | None,
) -> MutationOperationDescriptor:
    return MutationOperationDescriptor(
        kind=operation.kind,
        path=operation.path,
        move_path=operation.move_path,
        resolved_path=str(path),
        move_resolved_path=str(move_path) if move_path is not None else None,
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
