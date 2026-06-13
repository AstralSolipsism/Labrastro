"""Workspace-owner boundary for file mutation tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from reuleauxcoder.domain.files.file_mutation_service import (
    FileMutationResult,
    FileMutationService,
)


class WorkspaceMutationBackend(Protocol):
    """The only boundary allowed to preview or commit workspace file mutations."""

    workspace_id: str
    execution_target: str
    path_space: str

    def preview_text_patch(self, patch: str) -> FileMutationResult: ...

    def apply_text_patch(self, patch: str) -> FileMutationResult: ...

    def preview_document_commit(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult: ...

    def commit_document(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult: ...

    def save_candidate(self, candidate: dict[str, Any]) -> FileMutationResult: ...


class LocalWorkspaceMutationBackend:
    """Workspace mutation owner for an in-process local workspace."""

    execution_target = "local_workspace"
    path_space = "local_workspace"

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.service = FileMutationService(
            workspace_root,
            execution_target=self.execution_target,
            path_space=self.path_space,
        )
        self.workspace_id = str(self.service.workspace_root)

    def preview_text_patch(self, patch: str) -> FileMutationResult:
        return self.service.preview_text_patch(patch)

    def apply_text_patch(self, patch: str) -> FileMutationResult:
        return self.service.apply_text_patch(patch)

    def preview_document_commit(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        return self.service.preview_document_commit(target_path, content)

    def commit_document(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        return self.service.commit_document(target_path, content)

    def save_candidate(self, candidate: dict[str, Any]) -> FileMutationResult:
        return self.service.save_candidate(candidate)
