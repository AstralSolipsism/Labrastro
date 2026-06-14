"""Apply structured text patches through the runtime file mutation service."""

from __future__ import annotations

import os

from reuleauxcoder.domain.files import (
    APPLY_PATCH_PARAMETER_DESCRIPTION,
    APPLY_PATCH_TOOL_DESCRIPTION,
    FileMutationError,
    LocalWorkspaceMutationBackend,
    apply_patch_contract_error_message,
    validate_apply_patch_contract,
)
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import ToolOutputStrategy, ToolRisk


PATCH_ARGUMENT_CHARS_LIMIT = 64 * 1024


@register_tool
class ApplyPatchTool(Tool):
    name = "apply_patch"
    uses_workspace_mutation_candidate = True
    risk = ToolRisk.FILE_MUTATION
    output_strategy = ToolOutputStrategy.MUTATION_RESULT
    permission_policy = "file_mutation"
    mutates_files = True
    preview_required = True
    approved_save_candidate_required = True
    description = APPLY_PATCH_TOOL_DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": APPLY_PATCH_PARAMETER_DESCRIPTION,
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def preflight_validate(self, **kwargs) -> str | None:
        patch = kwargs.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return "Error: apply_patch requires a non-empty string patch"
        if len(patch) > PATCH_ARGUMENT_CHARS_LIMIT:
            return (
                "Error: apply_patch patch exceeds 64 KiB; split the patch or use "
                "draft_document_begin for long markdown documents"
            )
        if "\x00" in patch:
            return "Error: apply_patch does not accept binary patch content"
        try:
            validate_apply_patch_contract(patch)
        except FileMutationError as exc:
            return apply_patch_contract_error_message(str(exc))
        return None

    def execute(self, patch: str) -> str:
        validation_error = self.preflight_validate(patch=patch)
        if validation_error:
            return validation_error
        return self.run_backend(patch=patch)

    @backend_handler("remote_relay")
    def _execute_remote(self, patch: str) -> str:
        validation_error = self.preflight_validate(patch=patch)
        if validation_error:
            return validation_error
        preview_text_patch = getattr(self.backend, "preview_text_patch", None)
        save_candidate = getattr(self.backend, "save_candidate", None)
        if callable(preview_text_patch) and callable(save_candidate):
            preview = preview_text_patch(patch)
            if preview.status == "failed" or preview.error:
                return preview.message
            result = save_candidate(preview.approved_save_candidate)
            return _format_mutation_result(result)
        return self.backend.exec_tool("apply_patch", {"patch": patch})

    @backend_handler("local")
    def _execute_local(self, patch: str) -> str:
        workspace_root = getattr(getattr(self.backend, "context", None), "workspace_root", None)
        cwd = getattr(getattr(self.backend, "context", None), "cwd", None)
        mutation_backend = LocalWorkspaceMutationBackend(workspace_root or cwd or os.getcwd())
        preview = mutation_backend.preview_text_patch(patch)
        if preview.status == "failed" or preview.error:
            return preview.message
        result = mutation_backend.save_candidate(preview.approved_save_candidate)
        return _format_mutation_result(result)


def _format_mutation_result(result) -> str:
    if not result.ok:
        return result.message
    if result.diff:
        return f"{result.message}\n{result.diff}"
    return result.message
