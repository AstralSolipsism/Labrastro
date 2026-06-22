"""Local-action helpers for workspace mutation preview/save candidates."""

from __future__ import annotations

import json
from typing import Any

from reuleauxcoder.domain.files import FileChange, FileMutationResult


MUTATION_ACTIONS_REQUIRING_SAVE_CANDIDATE = {
    "apply_patch",
    "draft_document_commit",
}


class LocalActionSaveCandidateBinder:
    """Bind approved save candidates to local-action mutation identities."""

    def __init__(self, *, cwd: str | None = None) -> None:
        self.cwd = cwd
        self._approved_save_candidates: dict[str, dict[str, Any]] = {}

    def remember_approved_candidate(
        self,
        action_kind: str,
        args: dict[str, Any],
        candidate: dict[str, Any] | None,
    ) -> None:
        self.bind_save_candidate(action_kind, args, candidate)

    def bind_save_candidate(
        self,
        action_kind: str,
        args: dict[str, Any],
        candidate: dict[str, Any] | None,
    ) -> None:
        if isinstance(candidate, dict) and candidate:
            self._approved_save_candidates[
                self.request_key(action_kind, args)
            ] = dict(candidate)

    def pop_approved_candidate(
        self,
        action_kind: str,
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self._approved_save_candidates.pop(
            self.request_key(action_kind, args),
            None,
        )

    def request_key(self, action_kind: str, args: dict[str, Any]) -> str:
        return json.dumps(
            {
                "action_kind": action_kind,
                "args": args,
                "cwd": self.cwd,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )


class LocalActionPreviewBinder:
    """Convert local-action preview payloads into mutation preview results."""

    def mutation_preview_from_payload(
        self,
        action_kind: str,
        preview_payload: dict[str, Any],
    ) -> FileMutationResult:
        candidate = approved_save_candidate_from_preview_payload(preview_payload)
        preview_identity = preview_identity_from_candidate(candidate)
        missing = missing_required_save_candidate_fields(action_kind, candidate)
        if missing:
            message = (
                "local action mutation preview missing required "
                "approved_save_candidate fields: "
                + ", ".join(missing)
            )
            return FileMutationResult(
                status="failed",
                message=f"Error: {message}",
                error=message,
            )
        return FileMutationResult(
            status="in_progress",
            changes=changes_from_preview_payload(preview_payload),
            diff=str(preview_payload.get("diff") or ""),
            message=f"Preview {action_kind}",
            plan_id=str(preview_identity.get("plan_id") or "") or None,
            candidate_hash=str(preview_identity.get("candidate_hash") or "") or None,
            preview_identity=preview_identity,
            approved_save_candidate=candidate or {},
        )


def approved_save_candidate_from_preview_payload(
    preview_payload: dict[str, Any],
) -> dict[str, Any] | None:
    raw = preview_payload.get("approved_save_candidate")
    if not isinstance(raw, dict):
        raw = preview_payload.get("save_candidate")
    return dict(raw) if isinstance(raw, dict) and raw else None


def preview_identity_from_candidate(
    candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    identity = candidate.get("preview_identity")
    return dict(identity) if isinstance(identity, dict) and identity else {}


def missing_required_save_candidate_fields(
    action_kind: str,
    candidate: dict[str, Any] | None,
) -> list[str]:
    if action_kind not in MUTATION_ACTIONS_REQUIRING_SAVE_CANDIDATE:
        return []
    if not isinstance(candidate, dict) or not candidate:
        return ["approved_save_candidate", "preview_identity", "operations"]
    missing: list[str] = []
    preview_identity = candidate.get("preview_identity")
    if not isinstance(preview_identity, dict) or not preview_identity:
        missing.append("preview_identity")
    else:
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
                missing.append(f"preview_identity.{key}")
    operations = candidate.get("operations")
    if not isinstance(operations, list) or not operations:
        missing.append("operations")
    if str(candidate.get("tool_name") or "").strip() != action_kind:
        missing.append("tool_name")
    return missing


def changes_from_preview_payload(
    preview_payload: dict[str, Any],
) -> tuple[FileChange, ...]:
    sections = preview_payload.get("sections")
    changes: list[FileChange] = []
    if isinstance(sections, list):
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            path = str(
                section.get("path")
                or preview_payload.get("resolved_path")
                or f"change-{index + 1}"
            )
            kind = str(section.get("change_kind") or "update")
            if kind not in {"add", "update", "delete", "move"}:
                kind = "update"
            changes.append(
                FileChange(
                    path=path,
                    kind=kind,  # type: ignore[arg-type]
                    diff=str(section.get("content") or ""),
                    move_path=(
                        str(section["move_path"])
                        if section.get("move_path") is not None
                        else None
                    ),
                )
            )
    if changes:
        return tuple(changes)
    diff = str(preview_payload.get("diff") or "")
    if diff:
        return (
            FileChange(
                path=str(preview_payload.get("resolved_path") or "workspace"),
                kind="update",
                diff=diff,
            ),
        )
    return ()


__all__ = [
    "LocalActionPreviewBinder",
    "LocalActionSaveCandidateBinder",
    "MUTATION_ACTIONS_REQUIRING_SAVE_CANDIDATE",
    "approved_save_candidate_from_preview_payload",
    "changes_from_preview_payload",
    "missing_required_save_candidate_fields",
    "preview_identity_from_candidate",
]
