from __future__ import annotations

import importlib


def _lifecycle():
    return importlib.import_module("labrastro_server.services.agent_runtime.lifecycle")


def test_completing_task_does_not_mark_pull_request_as_merged() -> None:
    lifecycle = _lifecycle()

    state = lifecycle.TaskLifecycleState.create(
        task_id="task-1",
        issue_id="issue-1",
        agent_id="code_reviewer",
    )
    state.attach_artifact(
        artifact_id="artifact-1",
        type="pull_request",
        branch_name="agent/code-reviewer/task-1",
        pr_url="https://example.test/pr/1",
        status="pr_created",
    )

    state.complete_agent_run(output="PR 已创建，等待审核")

    assert state.task.status.value == "completed"
    assert state.artifacts["artifact-1"].status.value == "pr_created"
    assert state.artifacts["artifact-1"].merge_status.value == "pending_user"
    assert state.issue_status.value == "in_review"


def test_user_merge_updates_artifact_without_rewriting_task_result() -> None:
    lifecycle = _lifecycle()

    state = lifecycle.TaskLifecycleState.create(
        task_id="task-1",
        issue_id="issue-1",
        agent_id="code_reviewer",
    )
    state.attach_artifact(
        artifact_id="artifact-1",
        type="pull_request",
        branch_name="agent/code-reviewer/task-1",
        pr_url="https://example.test/pr/1",
        status="pr_created",
    )
    state.complete_agent_run(output="PR 已创建，等待审核")
    state.mark_artifact_merged("artifact-1", actor_user_id="user-1")

    assert state.task.status.value == "completed"
    assert state.task.output == "PR 已创建，等待审核"
    assert state.artifacts["artifact-1"].status.value == "merged"
    assert state.artifacts["artifact-1"].merge_status.value == "merged_by_user"
    assert state.artifacts["artifact-1"].merged_by == "user-1"
    assert state.issue_status.value == "done"


def test_review_comment_creates_followup_task_reusing_branch_and_pr() -> None:
    lifecycle = _lifecycle()

    state = lifecycle.TaskLifecycleState.create(
        task_id="task-1",
        issue_id="issue-1",
        agent_id="code_reviewer",
    )
    state.attach_artifact(
        artifact_id="artifact-1",
        type="pull_request",
        branch_name="agent/code-reviewer/task-1",
        pr_url="https://example.test/pr/1",
        status="pr_changes_requested",
    )

    followup = state.create_followup_task_from_comment(
        comment_id="comment-2",
        agent_id="code_reviewer",
    )

    assert followup.parent_task_id == "task-1"
    assert followup.trigger_comment_id == "comment-2"
    assert followup.branch_name == "agent/code-reviewer/task-1"
    assert followup.pr_url == "https://example.test/pr/1"
    assert followup.status.value == "queued"


def test_non_code_task_can_complete_with_report_artifact_only() -> None:
    lifecycle = _lifecycle()

    state = lifecycle.TaskLifecycleState.create(
        task_id="task-2",
        issue_id="issue-2",
        agent_id="researcher",
    )
    state.attach_artifact(
        artifact_id="artifact-2",
        type="report",
        content="调研结论",
        status="generated",
    )
    state.complete_agent_run(output="已完成调研")

    assert state.task.status.value == "completed"
    assert state.artifacts["artifact-2"].type.value == "report"
    assert state.artifacts["artifact-2"].branch_name is None
    assert state.artifacts["artifact-2"].pr_url is None
    assert state.artifacts["artifact-2"].merge_status is None


def test_publish_failure_artifact_is_preserved_without_blocking_completion() -> None:
    lifecycle = _lifecycle()

    state = lifecycle.TaskLifecycleState.create(
        task_id="task-3",
        issue_id="issue-3",
        agent_id="coder",
    )
    state.attach_artifact(
        artifact_id="artifact-failed",
        type="log",
        status="failed",
        content="gh pr create failed",
        metadata={"stage": "pr_create"},
    )
    state.complete_agent_run(output="executor completed")

    assert state.task.status.value == "completed"
    assert state.artifacts["artifact-failed"].status.value == "failed"
    assert state.artifacts["artifact-failed"].metadata["stage"] == "pr_create"
    assert state.issue_status.value == "done"
