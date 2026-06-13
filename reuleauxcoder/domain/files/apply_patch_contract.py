"""Authoritative model-visible contract for the apply_patch tool."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from reuleauxcoder.domain.files.file_mutation_service import (
    FileMutationError,
    _parse_patch,
    _split_hunks,
)


APPLY_PATCH_VALID_HEADERS = (
    "*** Add File:",
    "*** Update File:",
    "*** Delete File:",
    "*** Move to:",
)

APPLY_PATCH_BASE_EXAMPLE = "\n".join(
    [
        "*** Begin Patch",
        "*** Update File: src/app.py",
        "@@",
        '-print("Hi")',
        '+print("Hello")',
        "*** End Patch",
    ]
)

APPLY_PATCH_CONTRACT_TEXT = """Use the JSON function wrapper with a patch string. Patch must be a complete apply_patch document inside the JSON patch string:
*** Begin Patch
*** Update File: relative/path
@@
-old line
+new line
*** End Patch

Valid operation headers are only:
*** Add File: <relative path>
*** Update File: <relative path>
*** Delete File: <relative path>
*** Move to: <relative path>

Add File content lines must start with +. Update File must contain @@ hunks whose lines start with space, -, or +. Paths must be workspace-relative. Do not use *** File:, *** Action:, diff --git, ---/+++ unified diff headers, absolute paths, Windows drive-relative paths such as C:foo.txt, or path traversal. Use draft_document_begin for new long Markdown documents instead of passing long document content through apply_patch."""

APPLY_PATCH_TOOL_DESCRIPTION = (
    "Apply one structured apply_patch document to workspace text files. This is "
    "the only model-visible file mutation protocol for adding, updating, "
    "deleting, or moving text files. Use the JSON function wrapper with a "
    "`patch` string; do not use shell, write_file, edit_file, old_string/"
    "new_string, or any parallel file-writing protocol.\n\n"
    f"{APPLY_PATCH_CONTRACT_TEXT}"
)

APPLY_PATCH_PARAMETER_DESCRIPTION = APPLY_PATCH_CONTRACT_TEXT

APPLY_PATCH_RETRY_HINT = (
    "Use exactly this shape:\n"
    f"{APPLY_PATCH_BASE_EXAMPLE}\n\n"
    "Do not use *** File:, *** Action:, or unified diff headers."
)


def validate_apply_patch_contract(patch: str) -> None:
    """Validate syntax-level apply_patch contract without reading or writing files."""

    operations = _parse_patch(patch)
    for operation in operations:
        _validate_contract_path(operation.path)
        if operation.move_path:
            _validate_contract_path(operation.move_path)
        if operation.kind == "add":
            _validate_add_lines(operation.lines)
        elif operation.kind == "update":
            _validate_update_lines(operation.lines, operation.path)
        elif operation.kind == "move" and operation.lines:
            _validate_update_lines(operation.lines, operation.path)


def apply_patch_contract_error_message(error: str) -> str:
    return f"Error: invalid apply_patch patch: {error}\n\n{APPLY_PATCH_RETRY_HINT}"


def _validate_contract_path(path: str | None) -> None:
    if not isinstance(path, str) or not path.strip():
        raise FileMutationError("file path must be a non-empty string")
    value = path.strip()
    if (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.startswith(("/", "\\"))
    ):
        raise FileMutationError(f"absolute paths are not allowed: {path}")
    if PureWindowsPath(value).drive:
        raise FileMutationError(f"drive-relative paths are not allowed: {path}")
    posix_parts = PurePosixPath(value).parts
    windows_parts = PureWindowsPath(value).parts
    if ".." in posix_parts or ".." in windows_parts:
        raise FileMutationError(f"path traversal is not allowed: {path}")


def _validate_add_lines(lines: tuple[str, ...]) -> None:
    for line in lines:
        if not line.startswith("+"):
            raise FileMutationError("Add File lines must start with +")


def _validate_update_lines(lines: tuple[str, ...], path: str) -> None:
    hunks = _split_hunks(lines)
    if not hunks:
        raise FileMutationError(f"Update File requires at least one hunk: {path}")
    for hunk in hunks:
        old_segment_seen = False
        for line in hunk:
            if not line:
                raise FileMutationError("hunk lines must start with space, -, or +")
            prefix = line[0]
            if prefix not in {" ", "-", "+"}:
                raise FileMutationError("hunk lines must start with space, -, or +")
            if prefix in {" ", "-"}:
                old_segment_seen = True
        if not old_segment_seen:
            raise FileMutationError("update hunk must include context or removed lines")
