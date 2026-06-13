"""Live stream batching for runtime-owned document drafts."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable

from reuleauxcoder.domain.agent.document_draft import DocumentDraft
from reuleauxcoder.domain.agent.document_draft_text import draft_text_units
from reuleauxcoder.domain.agent.events import AgentEvent


DEFAULT_PREVIEW_FLUSH_INTERVAL_SEC = 0.1
DEFAULT_PREVIEW_FLUSH_CHARS = 2048
DEFAULT_PROGRESS_FLUSH_INTERVAL_SEC = 1.0
DEFAULT_PROGRESS_FLUSH_CHARS = 2048
DEFAULT_SNAPSHOT_FLUSH_INTERVAL_SEC = 5.0
DEFAULT_SNAPSHOT_FLUSH_CHARS = 16_384


@dataclass(frozen=True, slots=True)
class DocumentDraftStreamPolicy:
    preview_flush_interval_sec: float = DEFAULT_PREVIEW_FLUSH_INTERVAL_SEC
    preview_flush_chars: int = DEFAULT_PREVIEW_FLUSH_CHARS
    progress_flush_interval_sec: float = DEFAULT_PROGRESS_FLUSH_INTERVAL_SEC
    progress_flush_chars: int = DEFAULT_PROGRESS_FLUSH_CHARS
    snapshot_flush_interval_sec: float = DEFAULT_SNAPSHOT_FLUSH_INTERVAL_SEC
    snapshot_flush_chars: int = DEFAULT_SNAPSHOT_FLUSH_CHARS


class DocumentDraftLiveStream:
    """Derives preview/progress/snapshot events from the runtime draft content."""

    def __init__(
        self,
        *,
        policy: DocumentDraftStreamPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy or DocumentDraftStreamPolicy()
        self._clock = clock or time.monotonic
        self._draft_id = ""
        self._pending_preview_parts: list[str] = []
        self._pending_preview_start_offset: int | None = None
        self._pending_preview_started_at: float | None = None
        self._pending_preview_length = 0
        self._last_preview_flush_at: float | None = None
        self._pending_progress_started_at: float | None = None
        self._last_progress_flush_at: float | None = None
        self._last_progress_length = 0
        self._pending_snapshot_started_at: float | None = None
        self._last_snapshot_flush_at: float | None = None
        self._last_snapshot_length = 0
        self._last_chunk_seq = 0

    @property
    def last_chunk_seq(self) -> int:
        return self._last_chunk_seq

    def append(
        self,
        draft: DocumentDraft,
        content_delta: str,
        *,
        now: float | None = None,
    ) -> list[AgentEvent]:
        if not content_delta:
            return []
        now = self._now(now)
        self._ensure_draft(draft)
        content_length = _draft_content_length(draft)
        content_delta_length = draft_text_units(content_delta)
        start_offset = max(0, content_length - content_delta_length)
        if self._pending_preview_start_offset is None:
            self._pending_preview_start_offset = start_offset
            self._pending_preview_started_at = now
        self._pending_preview_parts.append(content_delta)
        self._pending_preview_length += content_delta_length

        events: list[AgentEvent] = []
        if self._should_flush_preview(now):
            events.append(self._flush_preview(draft, now))
        if self._should_emit_progress(content_length, now):
            events.append(self._progress_event(draft, now))
        if self._should_emit_snapshot(content_length, now):
            events.append(
                self._snapshot_event(
                    draft,
                    now,
                    snapshot_kind="periodic",
                    final=False,
                )
            )
        return events

    def flush(
        self,
        draft: DocumentDraft | None,
        *,
        now: float | None = None,
        snapshot_kind: str,
        final: bool,
    ) -> list[AgentEvent]:
        if draft is None:
            return []
        now = self._now(now)
        self._ensure_draft(draft)
        events: list[AgentEvent] = []
        if self._pending_preview_parts:
            events.append(self._flush_preview(draft, now))
        content_length = _draft_content_length(draft)
        if content_length != self._last_progress_length:
            events.append(self._progress_event(draft, now))
        if content_length > 0 or final:
            events.append(
                self._snapshot_event(
                    draft,
                    now,
                    snapshot_kind=snapshot_kind,
                    final=final,
                )
            )
        return events

    def _ensure_draft(self, draft: DocumentDraft) -> None:
        if self._draft_id == draft.draft_id:
            return
        self._draft_id = draft.draft_id
        self._pending_preview_parts = []
        self._pending_preview_start_offset = None
        self._pending_preview_started_at = None
        self._pending_preview_length = 0
        self._last_preview_flush_at = None
        self._pending_progress_started_at = None
        self._last_progress_flush_at = None
        self._last_progress_length = 0
        self._pending_snapshot_started_at = None
        self._last_snapshot_flush_at = None
        self._last_snapshot_length = 0
        self._last_chunk_seq = 0

    def _should_flush_preview(self, now: float) -> bool:
        if self._pending_preview_length <= 0:
            return False
        if self._pending_preview_length >= max(1, self.policy.preview_flush_chars):
            return True
        return self._elapsed(self._pending_preview_started_at, now) >= max(
            0.0,
            self.policy.preview_flush_interval_sec,
        )

    def _should_emit_progress(self, content_length: int, now: float) -> bool:
        if content_length <= 0 or content_length == self._last_progress_length:
            self._pending_progress_started_at = None
            return False
        if self._pending_progress_started_at is None:
            self._pending_progress_started_at = now
        if content_length - self._last_progress_length >= max(1, self.policy.progress_flush_chars):
            return True
        return self._elapsed(self._pending_progress_started_at, now) >= max(
            0.0,
            self.policy.progress_flush_interval_sec,
        )

    def _should_emit_snapshot(self, content_length: int, now: float) -> bool:
        if content_length <= 0 or content_length == self._last_snapshot_length:
            self._pending_snapshot_started_at = None
            return False
        if self._pending_snapshot_started_at is None:
            self._pending_snapshot_started_at = now
        if content_length - self._last_snapshot_length >= max(1, self.policy.snapshot_flush_chars):
            return True
        return self._elapsed(self._pending_snapshot_started_at, now) >= max(
            0.0,
            self.policy.snapshot_flush_interval_sec,
        )

    def _flush_preview(self, draft: DocumentDraft, now: float) -> AgentEvent:
        content = "".join(self._pending_preview_parts)
        start_offset = self._pending_preview_start_offset
        if start_offset is None:
            start_offset = max(0, _draft_content_length(draft) - draft_text_units(content))
        flush_latency_ms = int(self._elapsed(self._pending_preview_started_at, now) * 1000)
        self._last_chunk_seq += 1
        self._pending_preview_parts = []
        self._pending_preview_start_offset = None
        self._pending_preview_started_at = None
        self._pending_preview_length = 0
        self._last_preview_flush_at = now
        return AgentEvent.document_draft_preview_chunk(
            draft_id=draft.draft_id,
            target_path=draft.target_path,
            chunk_seq=self._last_chunk_seq,
            start_offset=start_offset,
            content=content,
            flush_latency_ms=flush_latency_ms,
        )

    def _progress_event(self, draft: DocumentDraft, now: float) -> AgentEvent:
        content_length = _draft_content_length(draft)
        self._last_progress_flush_at = now
        self._last_progress_length = content_length
        self._pending_progress_started_at = None
        return AgentEvent.document_draft_progress(
            draft_id=draft.draft_id,
            target_path=draft.target_path,
            content_length=content_length,
            content_sha256=_draft_content_sha256(draft),
            last_chunk_seq=self._last_chunk_seq,
        )

    def _snapshot_event(
        self,
        draft: DocumentDraft,
        now: float,
        *,
        snapshot_kind: str,
        final: bool,
    ) -> AgentEvent:
        content = draft.content
        self._last_snapshot_flush_at = now
        self._last_snapshot_length = draft_text_units(content)
        self._pending_snapshot_started_at = None
        return AgentEvent.document_draft_snapshot(
            draft_id=draft.draft_id,
            target_path=draft.target_path,
            content=content,
            snapshot_kind=snapshot_kind,
            final=final,
            last_chunk_seq=self._last_chunk_seq,
        )

    def _now(self, value: float | None) -> float:
        return self._clock() if value is None else float(value)

    @staticmethod
    def _elapsed(previous: float | None, now: float) -> float:
        if previous is None:
            return 0.0
        return max(0.0, now - previous)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _draft_content_length(draft: DocumentDraft) -> int:
    value = getattr(draft, "content_length", None)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            pass
    return draft_text_units(draft.content)


def _draft_content_sha256(draft: DocumentDraft) -> str:
    value = getattr(draft, "content_sha256", None)
    if isinstance(value, str) and value:
        return value
    return _sha256(draft.content)
