"""Memory provider pipeline."""

from __future__ import annotations

from typing import Protocol

from reuleauxcoder.domain.memory.models import (
    MemoryBundle,
    MemoryCaptureEvent,
    MemoryCaptureReceipt,
    MemoryItem,
    MemoryProvideRequest,
    MemoryQuery,
    MemoryScope,
)


class MemoryRepository(Protocol):
    def scope_version(self, scope: MemoryScope) -> int: ...

    def search(self, scope: MemoryScope, query: MemoryQuery) -> list[MemoryItem]: ...

    def enqueue_capture_job(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt: ...


class MemoryProvider:
    """Scoped provide/capture facade.

    Provide is synchronous and never performs extraction. Capture enqueues an
    idempotent job for later extraction/dedupe/indexing work.
    """

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository
        self._bundle_cache: dict[tuple, MemoryBundle] = {}

    def provide(
        self, scope: MemoryScope, request: MemoryProvideRequest
    ) -> MemoryBundle:
        version = self.repository.scope_version(scope)
        key = (
            *scope.cache_key(),
            version,
            request.query,
            request.token_budget,
            request.limit,
            request.type_filter,
        )
        cached = self._bundle_cache.get(key)
        if cached is not None:
            return cached

        items = self.repository.search(
            scope,
            MemoryQuery(
                query=request.query,
                limit=request.limit,
                type_filter=request.type_filter,
            ),
        )
        selected = self._fit_budget(items, request.token_budget)
        bundle = MemoryBundle(
            scope=scope,
            items=selected,
            token_estimate=self._estimate_tokens(selected),
            provenance={"scope_version": version},
        )
        self._bundle_cache[key] = bundle
        return bundle

    def capture(
        self, scope: MemoryScope, event: MemoryCaptureEvent
    ) -> MemoryCaptureReceipt:
        return self.repository.enqueue_capture_job(scope, event)

    @staticmethod
    def _estimate_tokens(items: list[MemoryItem]) -> int:
        chars = sum(len(item.abstract) + len(item.content) for item in items)
        return max(0, chars // 4)

    def _fit_budget(self, items: list[MemoryItem], token_budget: int) -> list[MemoryItem]:
        budget = max(0, int(token_budget or 0))
        if budget <= 0:
            return []
        selected: list[MemoryItem] = []
        used = 0
        for item in items:
            estimate = self._estimate_tokens([item])
            if selected and used + estimate > budget:
                break
            selected.append(item)
            used += estimate
        return selected
