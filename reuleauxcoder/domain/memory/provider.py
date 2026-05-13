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
from reuleauxcoder.domain.memory.runtime import (
    ACCOUNT_MEMORY_OWNER_PREFIX,
    GLOBAL_MEMORY_PROJECT_ID,
    MAIN_CHAT_MEMORY_NAMESPACE,
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

        query = MemoryQuery(
            query=request.query,
            limit=request.limit,
            type_filter=request.type_filter,
        )
        if self._is_account_main_chat(scope):
            items = self._search_account_main_chat(scope, query)
        else:
            items = self.repository.search(scope, query)
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

    @staticmethod
    def _is_account_main_chat(scope: MemoryScope) -> bool:
        return (
            scope.owner_agent_id.startswith(ACCOUNT_MEMORY_OWNER_PREFIX)
            and scope.memory_namespace == MAIN_CHAT_MEMORY_NAMESPACE
        )

    @staticmethod
    def _main_chat_scope(scope: MemoryScope, project_id: str) -> MemoryScope:
        return MemoryScope(
            owner_agent_id=scope.owner_agent_id,
            memory_namespace=scope.memory_namespace,
            project_id=project_id,
            workspace_id="",
            repo_id="",
            goal_id="",
            task_id="",
            session_id=scope.session_id,
            sensitivity=scope.sensitivity,
        )

    def _search_account_main_chat(
        self, scope: MemoryScope, query: MemoryQuery
    ) -> list[MemoryItem]:
        remaining = max(1, int(query.limit or 1))
        seen: set[str] = set()
        items: list[MemoryItem] = []

        def append_from(search_scope: MemoryScope, limit: int) -> None:
            if limit <= 0:
                return
            for item in self.repository.search(
                search_scope,
                MemoryQuery(
                    query=query.query,
                    limit=limit,
                    type_filter=query.type_filter,
                ),
            ):
                if item.id in seen:
                    continue
                seen.add(item.id)
                items.append(item)

        global_scope = self._main_chat_scope(scope, GLOBAL_MEMORY_PROJECT_ID)
        append_from(global_scope, remaining)
        remaining = max(0, remaining - len(items))

        current_project = scope.project_id.strip()
        if current_project and current_project != GLOBAL_MEMORY_PROJECT_ID:
            append_from(self._main_chat_scope(scope, current_project), remaining)
            remaining = max(0, int(query.limit or 1) - len(items))

        related_search = getattr(self.repository, "search_related_projects", None)
        if (
            remaining > 0
            and current_project
            and current_project != GLOBAL_MEMORY_PROJECT_ID
            and str(query.query or "").strip()
            and callable(related_search)
        ):
            excluded = {GLOBAL_MEMORY_PROJECT_ID}
            if current_project:
                excluded.add(current_project)
            for item in related_search(scope, query, excluded, remaining):
                if item.id in seen:
                    continue
                seen.add(item.id)
                items.append(item)
                if len(items) >= int(query.limit or 1):
                    break
        return items
