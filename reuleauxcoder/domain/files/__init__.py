"""Runtime-owned text file mutation services."""

from reuleauxcoder.domain.files.file_mutation_service import (
    FileChange,
    FileMutationError,
    FileMutationResult,
    FileMutationService,
    MutationOperationDescriptor,
    MutationPlan,
)
from reuleauxcoder.domain.files.apply_patch_contract import (
    APPLY_PATCH_CONTRACT_TEXT,
    APPLY_PATCH_PARAMETER_DESCRIPTION,
    APPLY_PATCH_TOOL_DESCRIPTION,
    apply_patch_contract_error_message,
    validate_apply_patch_contract,
)
from reuleauxcoder.domain.files.patch_argument_stream_decoder import (
    PatchArgumentStreamDecoder,
    PatchArgumentStreamError,
)
from reuleauxcoder.domain.files.workspace_mutation_backend import (
    LocalWorkspaceMutationBackend,
    WorkspaceMutationBackend,
)

__all__ = [
    "FileChange",
    "FileMutationError",
    "FileMutationResult",
    "FileMutationService",
    "APPLY_PATCH_CONTRACT_TEXT",
    "APPLY_PATCH_PARAMETER_DESCRIPTION",
    "APPLY_PATCH_TOOL_DESCRIPTION",
    "LocalWorkspaceMutationBackend",
    "MutationOperationDescriptor",
    "MutationPlan",
    "PatchArgumentStreamDecoder",
    "PatchArgumentStreamError",
    "WorkspaceMutationBackend",
    "apply_patch_contract_error_message",
    "validate_apply_patch_contract",
]
