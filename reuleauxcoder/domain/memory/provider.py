"""Provider contract for runtime memory adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from reuleauxcoder.domain.memory.models import (
    MemoryBundle,
    MemoryCaptureEvent,
    MemoryCaptureReceipt,
    MemoryForgetSelector,
    MemoryMutationResult,
    MemoryProviderCapabilities,
    MemoryProviderStatus,
    MemoryProvideRequest,
    MemoryRememberItem,
    MemoryScope,
)


@runtime_checkable
class MemoryProvider(Protocol):
    """Contract implemented by installed memory provider adapters."""

    @property
    def capabilities(self) -> MemoryProviderCapabilities: ...

    def health(self, scope: MemoryScope) -> MemoryProviderStatus: ...

    def provide(
        self, scope: MemoryScope, request: MemoryProvideRequest
    ) -> MemoryBundle: ...

    def capture(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt: ...

    def remember(
        self, scope: MemoryScope, item: MemoryRememberItem
    ) -> MemoryMutationResult: ...

    def forget(
        self, scope: MemoryScope, selector: MemoryForgetSelector
    ) -> MemoryMutationResult: ...
