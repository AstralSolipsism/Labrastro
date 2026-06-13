from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any

from reuleauxcoder.domain.agent.document_draft import DocumentDraftRuntime
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.files import FileChange, FileMutationResult


class ApprovalProvider:
    def __init__(self, decision: ApprovalDecision):
        self.decision = decision
        self.requests = []

    def request_approval(self, request):
        self.requests.append(request)
        return self.decision


class RecordingMutationBackend:
    workspace_id = "owner://workspace"
    execution_target = "remote_peer"
    path_space = "remote_peer_workspace"

    def __init__(self):
        self.preview_calls = []
        self.commit_calls = []
        self.save_calls = []

    def preview_text_patch(self, patch: str) -> FileMutationResult:
        raise AssertionError("draft runtime must not preview text patches")

    def apply_text_patch(self, patch: str) -> FileMutationResult:
        raise AssertionError("draft runtime must not apply text patches")

    def preview_document_commit(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        self.preview_calls.append((target_path, content))
        candidate = _approved_candidate(
            target_path,
            content,
            workspace_id=self.workspace_id,
            execution_target=self.execution_target,
            path_space=self.path_space,
        )
        return FileMutationResult(
            status="in_progress",
            changes=(
                FileChange(
                    path=target_path,
                    kind="add",
                    diff="--- a/docs/architecture.md\n+++ b/docs/architecture.md\n",
                ),
            ),
            diff="--- a/docs/architecture.md\n+++ b/docs/architecture.md\n",
            preview_identity=candidate["preview_identity"],
            approved_save_candidate=candidate,
        )

    def commit_document(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        self.commit_calls.append((target_path, content))
        return FileMutationResult(status="completed", message="committed")

    def save_candidate(self, candidate: dict[str, Any]) -> FileMutationResult:
        self.save_calls.append(deepcopy(candidate))
        return FileMutationResult(status="completed", message="saved")


def _declaration() -> str:
    return "\n".join(
        [
            "Draft document declared: Architecture",
            "draft_id: draft-test",
            "target_path: docs/architecture.md",
            "Continue the document body in assistant markdown stream.",
        ]
    )


def _approved_candidate(
    target_path: str,
    content: str,
    *,
    workspace_id: str = "owner://workspace",
    execution_target: str = "remote_peer",
    path_space: str = "remote_peer_workspace",
) -> dict[str, Any]:
    preview_identity = {
        "plan_id": "draft-plan",
        "candidate_hash": "draft-candidate",
        "tool_name": "draft_document_commit",
        "workspace_id": workspace_id,
        "execution_target": execution_target,
        "path_space": path_space,
        "args_hash": "draft-args",
    }
    diff = "--- a/docs/architecture.md\n+++ b/docs/architecture.md\n"
    return {
        "plan_id": preview_identity["plan_id"],
        "candidate_hash": preview_identity["candidate_hash"],
        "tool_name": preview_identity["tool_name"],
        "workspace_id": workspace_id,
        "execution_target": execution_target,
        "path_space": path_space,
        "args_hash": preview_identity["args_hash"],
        "preview_identity": preview_identity,
        "operations": [
            {
                "kind": "add",
                "path": target_path,
                "new_content": content,
            }
        ],
        "changes": [
            {
                "path": target_path,
                "kind": "add",
                "diff": diff,
            }
        ],
        "diff": diff,
    }


def test_document_draft_runtime_commits_after_file_change_approval(tmp_path: Path) -> None:
    events = []
    provider = ApprovalProvider(ApprovalDecision.allow_once("ok"))
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=provider,
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Architecture\n")
    runtime.append_stream_delta("\nBody\n")
    runtime.commit_active()

    assert (tmp_path / "docs" / "architecture.md").read_text() == "# Architecture\n\nBody\n"
    assert provider.requests[0].tool_name == "draft_document_commit"
    assert [event.event_type for event in events] == [
        AgentEventType.DOCUMENT_DRAFT_STARTED,
        AgentEventType.FILE_CHANGE_STARTED,
        AgentEventType.DOCUMENT_DRAFT_COMMIT_REQUESTED,
        AgentEventType.FILE_CHANGE_APPROVAL_REQUESTED,
        AgentEventType.FILE_CHANGE_APPROVAL_RESOLVED,
        AgentEventType.DOCUMENT_DRAFT_COMMITTED,
        AgentEventType.FILE_CHANGE_COMPLETED,
    ]
    assert events[-1].data["status"] == "completed"


def test_document_draft_runtime_decline_does_not_apply_patch(tmp_path: Path) -> None:
    events = []
    provider = ApprovalProvider(ApprovalDecision.deny_once("no"))
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=provider,
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Architecture\n")
    runtime.commit_active()

    assert not (tmp_path / "docs" / "architecture.md").exists()
    assert events[-2].event_type is AgentEventType.DOCUMENT_DRAFT_CANCELLED
    assert events[-1].event_type is AgentEventType.FILE_CHANGE_COMPLETED
    assert events[-1].data["status"] == "declined"


def test_document_draft_runtime_rejects_existing_target_before_approval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "docs" / "architecture.md"
    target.parent.mkdir()
    target.write_text("existing\n", encoding="utf-8")
    events = []
    provider = ApprovalProvider(ApprovalDecision.allow_once("ok"))
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=provider,
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Replacement\n")
    runtime.commit_active()

    assert provider.requests == []
    assert target.read_text(encoding="utf-8") == "existing\n"
    assert [event.event_type for event in events] == [
        AgentEventType.DOCUMENT_DRAFT_STARTED,
        AgentEventType.DOCUMENT_DRAFT_FAILED,
        AgentEventType.FILE_CHANGE_COMPLETED,
    ]
    assert events[-1].data["status"] == "failed"
    assert "already exists" in events[-1].data["error"]
    assert "apply_patch" in events[-1].data["error"]


def test_document_draft_runtime_commits_through_workspace_owner(tmp_path: Path) -> None:
    events = []
    owner = RecordingMutationBackend()
    provider = ApprovalProvider(ApprovalDecision.allow_once("ok"))
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        mutation_backend=owner,
        approval_provider=provider,
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Architecture\n")
    runtime.commit_active()

    assert owner.preview_calls == [
        ("docs/architecture.md", "# Architecture\n"),
    ]
    assert owner.commit_calls == []
    assert owner.save_calls[0]["operations"] == [
        {
            "kind": "add",
            "path": "docs/architecture.md",
            "new_content": "# Architecture\n",
        }
    ]
    assert not (tmp_path / "docs" / "architecture.md").exists()
    assert provider.requests[0].metadata["workspace_mutation_owner"] == {
        "workspace_id": "owner://workspace",
        "execution_target": "remote_peer",
        "path_space": "remote_peer_workspace",
    }
    assert "draft_document_content" not in provider.requests[0].metadata


def test_document_draft_runtime_saves_confirmed_candidate_after_manual_file_edit(
    tmp_path: Path,
) -> None:
    class EditingApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            target = tmp_path / "docs" / "architecture.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("manual edit during approval\n", encoding="utf-8")
            candidate = deepcopy(request.metadata["approved_save_candidate"])
            candidate["operations"][0]["new_content"] = "# Confirmed\n"
            return ApprovalDecision.allow_once(
                "save confirmed candidate",
                meta={"approved_save_candidate": candidate},
            )

    events = []
    provider = EditingApprovalProvider()
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=provider,
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Architecture\n")
    runtime.commit_active()

    assert (tmp_path / "docs" / "architecture.md").read_text(
        encoding="utf-8"
    ) == "# Confirmed\n"
    assert provider.requests[0].metadata["approved_save_candidate"]["operations"] == [
        {
            "kind": "add",
            "path": "docs/architecture.md",
            "new_content": "# Architecture\n",
        }
    ]
    assert events[-1].event_type is AgentEventType.FILE_CHANGE_COMPLETED
    assert events[-1].data["status"] == "completed"


def test_document_draft_runtime_snapshot_reads_active_draft_source(
    tmp_path: Path,
) -> None:
    events = []
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=ApprovalProvider(ApprovalDecision.deny_once("no")),
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Architecture\n")
    runtime.append_stream_delta("\nBody\n")

    snapshot = runtime.snapshot_active()

    assert snapshot is not None
    assert snapshot.draft_id == "draft-test"
    assert snapshot.target_path == "docs/architecture.md"
    assert snapshot.content == "# Architecture\n\nBody\n"
    assert snapshot.content_length == len("# Architecture\n\nBody\n")
    assert snapshot.content_sha256 == hashlib.sha256(
        "# Architecture\n\nBody\n".encode("utf-8")
    ).hexdigest()


def test_document_draft_runtime_snapshot_uses_utf16_content_length(
    tmp_path: Path,
) -> None:
    events = []
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=ApprovalProvider(ApprovalDecision.deny_once("no")),
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("A😀B")

    snapshot = runtime.snapshot_active()

    assert snapshot is not None
    assert snapshot.content == "A😀B"
    assert snapshot.content_length == 4


def test_document_draft_runtime_marks_interruption_recoverable_with_checkpoint(
    tmp_path: Path,
) -> None:
    events = []
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=ApprovalProvider(ApprovalDecision.deny_once("no")),
        emit=events.append,
    )

    runtime.begin_from_tool_result(_declaration())
    runtime.append_stream_delta("# Architecture\n")

    checkpoint = runtime.interrupt_active("provider stream interrupted", last_chunk_seq=2)

    assert checkpoint is not None
    assert checkpoint.content == "# Architecture\n"
    assert checkpoint.to_dict()["content"] == "# Architecture\n"
    assert getattr(runtime, "active") is not None
    assert runtime.active.status == "interrupted"
    assert [event.event_type for event in events[-2:]] == [
        AgentEventType.DRAFT_BODY_STALLED,
        AgentEventType.DRAFT_INTERRUPTED_RECOVERABLE,
    ]
    assert "content" not in events[-1].data
    assert events[-1].data["content_length"] == len("# Architecture\n")


def test_document_draft_runtime_restores_checkpoint_for_continuation(
    tmp_path: Path,
) -> None:
    events = []
    runtime = DocumentDraftRuntime(
        workspace_root=str(tmp_path),
        approval_provider=ApprovalProvider(ApprovalDecision.deny_once("no")),
        emit=events.append,
    )

    restored = runtime.restore_checkpoint(
        {
            "draft_id": "draft-test",
            "target_path": "docs/architecture.md",
            "title": "Architecture",
            "format": "markdown",
            "status": "interrupted",
            "content": "# Architecture\n",
            "content_length": len("# Architecture\n"),
            "content_sha256": hashlib.sha256("# Architecture\n".encode("utf-8")).hexdigest(),
        }
    )
    runtime.append_stream_delta("\nContinued\n")

    assert restored is not None
    assert runtime.active is not None
    assert runtime.active.status == "streaming"
    assert runtime.active.content == "# Architecture\n\nContinued\n"
