"""Runtime-owned long document draft lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Callable

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.document_draft_text import draft_text_units
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.files import (
    LocalWorkspaceMutationBackend,
    WorkspaceMutationBackend,
)


@dataclass(slots=True)
class DocumentDraft:
    draft_id: str
    target_path: str
    title: str
    format: str = "markdown"
    body_parts: list[str] = field(default_factory=list)
    status: str = "declared"
    _content_length: int = field(default=0, init=False, repr=False)
    _content_hasher: Any = field(
        default_factory=hashlib.sha256,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        parts = list(self.body_parts)
        self.body_parts = []
        for part in parts:
            self.append_body_delta(part)

    @property
    def content(self) -> str:
        return "".join(self.body_parts)

    @property
    def content_length(self) -> int:
        return self._content_length

    @property
    def content_sha256(self) -> str:
        return self._content_hasher.hexdigest()

    def append_body_delta(self, text: str) -> None:
        if not text:
            return
        self.body_parts.append(text)
        self._content_length += draft_text_units(text)
        self._content_hasher.update(text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class DocumentDraftSnapshot:
    draft_id: str
    target_path: str
    title: str
    format: str
    status: str
    content: str
    content_length: int
    content_sha256: str

    @classmethod
    def from_draft(cls, draft: DocumentDraft) -> "DocumentDraftSnapshot":
        content = draft.content
        return cls(
            draft_id=draft.draft_id,
            target_path=draft.target_path,
            title=draft.title,
            format=draft.format,
            status=draft.status,
            content=content,
            content_length=draft_text_units(content),
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )


class DocumentDraftRuntime:
    """Owns draft declaration, body buffering, approval, and commit."""

    def __init__(
        self,
        *,
        workspace_root: str,
        mutation_backend: WorkspaceMutationBackend | None = None,
        approval_provider: object | None,
        emit: Callable[[AgentEvent], None],
    ) -> None:
        self.workspace_root = workspace_root
        self.mutation_backend = mutation_backend or LocalWorkspaceMutationBackend(
            workspace_root
        )
        self.approval_provider = approval_provider
        self.emit = emit
        self.active: DocumentDraft | None = None

    def begin_from_tool_result(self, result: str) -> DocumentDraft | None:
        parsed = _parse_draft_declaration(result)
        if parsed is None:
            return None
        draft = DocumentDraft(**parsed)
        draft.status = "streaming"
        self.active = draft
        self.emit(
            AgentEvent.document_draft_started(
                draft_id=draft.draft_id,
                target_path=draft.target_path,
                title=draft.title,
                format=draft.format,
            )
        )
        return draft

    def append_stream_delta(self, text: str) -> None:
        if self.active is None or not text:
            return
        if self.active.status == "streaming":
            self.active.append_body_delta(text)

    def snapshot_active(self) -> DocumentDraftSnapshot | None:
        if self.active is None:
            return None
        return DocumentDraftSnapshot.from_draft(self.active)

    def commit_active(self) -> None:
        draft = self.active
        if draft is None or draft.status != "streaming":
            return
        content = draft.content
        if not content.strip():
            self._fail(draft, "draft document body is empty")
            return
        item_id = f"file-change:draft:{draft.draft_id}"
        approval_id = f"approval:draft:{draft.draft_id}"
        mutation_backend = self.mutation_backend
        preview = mutation_backend.preview_document_commit(draft.target_path, content)
        changes = [change.to_dict() for change in preview.changes]
        if preview.status == "failed":
            self._fail(draft, preview.error or preview.message)
            self.emit(
                AgentEvent.file_change_completed(
                    item_id=item_id,
                    changes=changes,
                    status="failed",
                    error=preview.error or preview.message,
                )
            )
            return
        self.emit(
            AgentEvent.file_change_started(
                item_id=item_id,
                changes=changes,
                status="in_progress",
            )
        )
        self.emit(
            AgentEvent.document_draft_commit_requested(
                draft_id=draft.draft_id,
                target_path=draft.target_path,
                item_id=item_id,
                approval_id=approval_id,
            )
        )
        self.emit(
            AgentEvent.file_change_approval_requested(
                item_id=item_id,
                approval_id=approval_id,
                reason=f"Commit draft document to {draft.target_path}",
            )
        )
        decision = self._request_commit_approval(draft, preview.diff)
        if decision != "allow_once":
            reason = "draft document commit was not approved"
            self.emit(
                AgentEvent.file_change_approval_resolved(
                    item_id=item_id,
                    approval_id=approval_id,
                    decision="deny_once",
                    reason=reason,
                )
            )
            self.emit(
                AgentEvent.document_draft_cancelled(
                    draft_id=draft.draft_id,
                    target_path=draft.target_path,
                    reason=reason,
                )
            )
            self.emit(
                AgentEvent.file_change_completed(
                    item_id=item_id,
                    changes=changes,
                    status="declined",
                    error=reason,
                )
            )
            draft.status = "cancelled"
            self.active = None
            return
        self.emit(
            AgentEvent.file_change_approval_resolved(
                item_id=item_id,
                approval_id=approval_id,
                decision="allow_once",
            )
        )
        result = mutation_backend.commit_document(draft.target_path, content)
        result_changes = [change.to_dict() for change in result.changes] or changes
        if result.status == "completed":
            self.emit(
                AgentEvent.document_draft_committed(
                    draft_id=draft.draft_id,
                    target_path=draft.target_path,
                    item_id=item_id,
                )
            )
            self.emit(
                AgentEvent.file_change_completed(
                    item_id=item_id,
                    changes=result_changes,
                    status="completed",
                    duration_ms=result.duration_ms,
                )
            )
            draft.status = "committed"
            self.active = None
            return
        self._fail(draft, result.error or result.message)
        self.emit(
            AgentEvent.file_change_completed(
                item_id=item_id,
                changes=result_changes,
                status="failed",
                error=result.error or result.message,
                duration_ms=result.duration_ms,
            )
        )

    def cancel_active(self, reason: str) -> None:
        draft = self.active
        if draft is None:
            return
        self.emit(
            AgentEvent.document_draft_cancelled(
                draft_id=draft.draft_id,
                target_path=draft.target_path,
                reason=reason,
            )
        )
        draft.status = "cancelled"
        self.active = None

    def _fail(self, draft: DocumentDraft, error: str) -> None:
        self.emit(
            AgentEvent.document_draft_failed(
                draft_id=draft.draft_id,
                target_path=draft.target_path,
                error=error,
            )
        )
        draft.status = "failed"
        self.active = None

    def _request_commit_approval(self, draft: DocumentDraft, diff: str) -> str:
        provider = self.approval_provider
        request_approval = getattr(provider, "request_approval", None)
        if not callable(request_approval):
            return "deny_once"
        decision = request_approval(
            ApprovalRequest(
                tool_name="draft_document_commit",
                tool_args={
                    "target_path": draft.target_path,
                    "title": draft.title,
                    "diff": diff,
                },
                tool_source="runtime",
                reason=f"Commit draft document to {draft.target_path}",
                intent="Review the generated document diff before it is written.",
                metadata={
                    "draft_id": draft.draft_id,
                    "runtime_workspace_root": self.workspace_root,
                    "workspace_mutation_owner": {
                        "workspace_id": str(
                            getattr(self.mutation_backend, "workspace_id", "")
                        ),
                        "execution_target": str(
                            getattr(self.mutation_backend, "execution_target", "")
                        ),
                        "path_space": str(
                            getattr(self.mutation_backend, "path_space", "")
                        ),
                    },
                },
            )
        )
        return str(getattr(decision, "mode", "deny_once") or "deny_once")


_DRAFT_FIELD_RE = re.compile(r"^(draft_id|target_path):\s*(.+?)\s*$")


def _parse_draft_declaration(result: str) -> dict[str, str] | None:
    if not isinstance(result, str) or "draft document declared" not in result.lower():
        return None
    values: dict[str, str] = {}
    title = ""
    for index, line in enumerate(result.splitlines()):
        if index == 0 and ":" in line:
            title = line.split(":", 1)[1].strip()
        match = _DRAFT_FIELD_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    draft_id = values.get("draft_id")
    target_path = values.get("target_path")
    if not draft_id or not target_path:
        return None
    return {
        "draft_id": draft_id,
        "target_path": target_path,
        "title": title or target_path,
        "format": "markdown",
    }
