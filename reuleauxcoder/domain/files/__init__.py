"""Runtime-owned text file mutation services."""

from reuleauxcoder.domain.files.file_mutation_service import (
    FileChange,
    FileMutationError,
    FileMutationResult,
    FileMutationService,
    MutationOperationState,
    MutationPlan,
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
    "LocalWorkspaceMutationBackend",
    "MutationOperationState",
    "MutationPlan",
    "PatchArgumentStreamDecoder",
    "PatchArgumentStreamError",
    "WorkspaceMutationBackend",
]
