from __future__ import annotations

import hashlib

from reuleauxcoder.domain.agent.document_draft import DocumentDraft
from reuleauxcoder.domain.agent.document_draft_stream import (
    DocumentDraftLiveStream,
    DocumentDraftStreamPolicy,
)


def _draft(content: str = "") -> DocumentDraft:
    draft = DocumentDraft(
        draft_id="draft-1",
        target_path="docs/architecture.md",
        title="Architecture",
    )
    draft.status = "streaming"
    if content:
        draft.append_body_delta(content)
    return draft


class _CountingDraft:
    def __init__(self) -> None:
        self.draft_id = "draft-1"
        self.target_path = "docs/architecture.md"
        self.title = "Architecture"
        self.status = "streaming"
        self.content_length = 0
        self.content_reads = 0
        self._parts: list[str] = []

    def append(self, text: str) -> None:
        self._parts.append(text)
        self.content_length += len(text)

    @property
    def content(self) -> str:
        self.content_reads += 1
        return "".join(self._parts)


def test_document_draft_live_stream_batches_preview_and_progress_by_chars() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=4,
            preview_flush_interval_sec=999,
            progress_flush_chars=4,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _draft()

    draft.append_body_delta("ab")
    assert stream.append(draft, "ab", now=0) == []

    draft.append_body_delta("cd")
    events = stream.append(draft, "cd", now=0)

    assert [event.event_type.value for event in events] == [
        "document_draft_preview_chunk",
        "document_draft_progress",
    ]
    preview = events[0].data
    assert preview["chunk_seq"] == 1
    assert preview["start_offset"] == 0
    assert preview["end_offset"] == 4
    assert preview["content"] == "abcd"
    assert preview["content_sha256"] == hashlib.sha256(b"abcd").hexdigest()
    progress = events[1].data
    assert progress["content_length"] == 4
    assert progress["content_sha256"] == hashlib.sha256(b"abcd").hexdigest()
    assert progress["last_chunk_seq"] == 1
    assert "content" not in progress


def test_document_draft_live_stream_append_does_not_read_full_content_before_flush() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _CountingDraft()

    draft.append("a")
    events = stream.append(draft, "a", now=0)

    assert events == []
    assert draft.content_reads == 0


def test_document_draft_live_stream_preview_offsets_use_incremental_lengths() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=3,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _CountingDraft()

    draft.append("ab")
    assert stream.append(draft, "ab", now=0) == []

    draft.append("c")
    events = stream.append(draft, "c", now=0)

    assert [event.event_type.value for event in events] == [
        "document_draft_preview_chunk"
    ]
    assert events[0].data["start_offset"] == 0
    assert events[0].data["end_offset"] == 3
    assert draft.content_reads == 0


def test_document_draft_live_stream_flushes_first_preview_by_interval_before_char_threshold() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=0.1,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _draft()

    draft.append_body_delta("a")
    assert stream.append(draft, "a", now=0) == []

    draft.append_body_delta("b")
    events = stream.append(draft, "b", now=0.11)

    assert [event.event_type.value for event in events] == [
        "document_draft_preview_chunk"
    ]
    preview = events[0].data
    assert preview["content"] == "ab"
    assert preview["start_offset"] == 0
    assert preview["end_offset"] == 2


def test_document_draft_live_stream_emits_first_progress_by_interval_before_char_threshold() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=0.1,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _draft()

    draft.append_body_delta("a")
    assert stream.append(draft, "a", now=0) == []

    draft.append_body_delta("b")
    events = stream.append(draft, "b", now=0.11)

    assert [event.event_type.value for event in events] == [
        "document_draft_progress"
    ]
    progress = events[0].data
    assert progress["content_length"] == 2
    assert progress["content_sha256"] == hashlib.sha256(b"ab").hexdigest()


def test_document_draft_live_stream_emits_first_snapshot_by_interval_before_char_threshold() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=0.1,
        )
    )
    draft = _draft()

    draft.append_body_delta("a")
    assert stream.append(draft, "a", now=0) == []

    draft.append_body_delta("b")
    events = stream.append(draft, "b", now=0.11)

    assert [event.event_type.value for event in events] == [
        "document_draft_snapshot"
    ]
    snapshot = events[0].data
    assert snapshot["content"] == "ab"
    assert snapshot["content_length"] == 2
    assert snapshot["content_sha256"] == hashlib.sha256(b"ab").hexdigest()
    assert snapshot["snapshot_kind"] == "periodic"
    assert snapshot["final"] is False


def test_document_draft_live_stream_force_flush_emits_final_snapshot() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _draft("abc")

    assert stream.append(draft, "abc", now=0) == []
    events = stream.flush(
        draft,
        now=1,
        snapshot_kind="interrupted",
        final=True,
    )

    assert [event.event_type.value for event in events] == [
        "document_draft_preview_chunk",
        "document_draft_progress",
        "document_draft_snapshot",
    ]
    assert events[0].data["content"] == "abc"
    assert events[0].data["start_offset"] == 0
    assert events[0].data["end_offset"] == 3
    assert events[1].data["content_length"] == 3
    snapshot = events[2].data
    assert snapshot["content"] == "abc"
    assert snapshot["content_length"] == 3
    assert snapshot["content_sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert snapshot["snapshot_kind"] == "interrupted"
    assert snapshot["final"] is True
    assert snapshot["last_chunk_seq"] == 1


def test_document_draft_live_stream_uses_utf16_units_for_offsets_and_lengths() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=999,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _draft("A😀B")

    assert stream.append(draft, "A😀B", now=0) == []
    events = stream.flush(
        draft,
        now=1,
        snapshot_kind="final",
        final=True,
    )

    assert [event.event_type.value for event in events] == [
        "document_draft_preview_chunk",
        "document_draft_progress",
        "document_draft_snapshot",
    ]
    assert events[0].data["start_offset"] == 0
    assert events[0].data["end_offset"] == 4
    assert events[1].data["content_length"] == 4
    assert events[2].data["content_length"] == 4


def test_document_draft_live_stream_emits_snapshot_by_threshold() -> None:
    stream = DocumentDraftLiveStream(
        policy=DocumentDraftStreamPolicy(
            preview_flush_chars=999,
            preview_flush_interval_sec=999,
            progress_flush_chars=999,
            progress_flush_interval_sec=999,
            snapshot_flush_chars=3,
            snapshot_flush_interval_sec=999,
        )
    )
    draft = _draft("abc")

    events = stream.append(draft, "abc", now=0)

    assert [event.event_type.value for event in events] == ["document_draft_snapshot"]
    snapshot = events[0].data
    assert snapshot["content"] == "abc"
    assert snapshot["snapshot_kind"] == "periodic"
    assert snapshot["final"] is False
    assert snapshot["last_chunk_seq"] == 0
