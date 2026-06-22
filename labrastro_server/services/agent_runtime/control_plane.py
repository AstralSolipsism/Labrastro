"""Server-side control plane for queued AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
import json
import threading
import time
import uuid

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AGENT_RUN_METADATA_ACTIVATION_KEYS,
    AGENT_RUN_METADATA_FORBIDDEN_KEYS,
    AgentRunActivation,
    AgentRunActivationInputKind,
    AgentRunActivationStatus,
    ActivationSteer,
    ActivationSteerSource,
    ActivationSteerStatus,
    AgentCallGrant,
    AgentRunFeedback,
    AgentRunFeedbackKind,
    AgentRunFeedbackSource,
    AgentRunFeedbackVisibility,
    AgentRunRelation,
    AgentRunRelationType,
    AgentRunSource,
    AgentRunResumePolicy,
    AgentThreadBinding,
    AgentThreadBindingLifetime,
    AgentThreadBindingStatus,
    SessionRunBinding,
    SessionRunBindingStatus,
    ArtifactStatus,
    ArtifactType,
    ExecutionLocation,
    ExecutorType,
    PublishPolicy,
    TaskArtifact,
    AgentRun,
    TaskSessionRef,
    AgentRunStatus,
    AgentRunWaitingReason,
    TriggerMode,
    ModelRequestOrigin,
    WorkerKind,
    WorktreeRole,
)
from reuleauxcoder.domain.agent.runtime_budget import RUNTIME_BUDGET_FIELDS
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunRequest,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.environment_events import (
    environment_summary_event,
    expand_environment_executor_event,
)
from labrastro_server.services.agent_runtime.lifecycle import IssueStatus, TaskLifecycleState
from labrastro_server.services.agent_runtime.permission_events import (
    blocked_review_event_payload,
    should_block_waiting_approval,
)
from labrastro_server.services.agent_runtime.prompt_renderer import (
    CanonicalAgentContext,
    ExecutorPromptRenderer,
)
from labrastro_server.services.agent_runtime.runtime_store import (
    DEFAULT_RUNTIME_EVENT_LIMIT,
    AgentRunStore,
    artifact_attached_event_payload,
    clamp_event_limit,
    agent_relation_completed_payload,
    agent_relation_terminal_lifecycle_events,
    executor_result_artifacts,
    runtime_slots_allow_agent_run_claim,
    worktree_lifecycle_events,
)
from labrastro_server.services.agent_runtime.runtime_policy import (
    model_request_origin_for_runtime,
    optional_model_request_origin,
    optional_publish_policy,
    optional_worker_kind,
    optional_worktree_role,
    system_flow_for_source,
    validate_agent_run_runtime_policy,
    worker_kind_for_runtime,
    worker_matches_agent_run,
    workspace_key,
)
from labrastro_server.services.agent_runtime.worktree import WorktreeManager
from labrastro_server.services.sandbox.provider import SandboxProfile, SandboxProvider
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    RuntimeProfilesConfig,
    RunLimitsConfig,
    build_agent_run_snapshot,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


_ACTIVATION_STEER_ITEM_TYPES = {
    "text",
    "image_ref",
    "file_ref",
    "artifact_ref",
    "session_item_ref",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _validate_activation_steer_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("activation steer payload must be an object")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("activation steer payload requires non-empty items")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("activation steer item must be an object")
        item_type = str(item.get("type") or "").strip()
        if item_type not in _ACTIVATION_STEER_ITEM_TYPES:
            raise ValueError("unsupported activation steer item type")


def _normalize_activation_steer_metadata(
    metadata: dict[str, Any] | None,
    *,
    source: ActivationSteerSource,
    fallback_key: str,
) -> dict[str, Any]:
    value = dict(metadata or {})
    idempotency_key = str(
        value.get("idempotency_key") or value.get("client_steer_id") or fallback_key
    ).strip()
    if not idempotency_key:
        raise ValueError("activation steer idempotency_key is required")
    value["idempotency_key"] = idempotency_key
    value.setdefault("sender", source.value)
    value["sender"] = str(value.get("sender") or source.value).strip() or source.value
    return value


def _activation_steer_idempotency_scope(
    metadata: dict[str, Any],
) -> tuple[str, str]:
    return (
        str(metadata.get("sender") or "").strip(),
        str(metadata.get("idempotency_key") or "").strip(),
    )


def _default_executor_session_id(task_id: str) -> str:
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "-"
        for ch in str(task_id or "").strip()
    ).strip("-")
    return f"labrastro-agent-run-{safe or uuid.uuid4().hex}"


def _ensure_reuleauxcoder_executor_session(
    request: "AgentRunRequest",
    task_id: str,
) -> None:
    if request.executor == ExecutorType.REULEAUXCODER and not request.executor_session_id:
        request.executor_session_id = _default_executor_session_id(task_id)


def _coerce_executor(value: ExecutorType | str | None) -> ExecutorType:
    if isinstance(value, ExecutorType):
        return value
    if value is None or str(value).strip() == "":
        return ExecutorType.REULEAUXCODER
    return ExecutorType(str(value))


def _coerce_location(value: ExecutionLocation | str | None) -> ExecutionLocation:
    if isinstance(value, ExecutionLocation):
        return value
    if value is None or str(value).strip() == "":
        return ExecutionLocation.LOCAL_WORKSPACE
    return ExecutionLocation(str(value))


def _optional_executor(value: ExecutorType | str | None) -> ExecutorType | None:
    if isinstance(value, ExecutorType):
        return value
    if value is None or str(value).strip() == "":
        return None
    return ExecutorType(str(value))


def _optional_location(
    value: ExecutionLocation | str | None,
) -> ExecutionLocation | None:
    if isinstance(value, ExecutionLocation):
        return value
    if value is None or str(value).strip() == "":
        return None
    return ExecutionLocation(str(value))


def _agent_run_to_dict(task: AgentRun) -> dict[str, Any]:
    metadata = dict(task.metadata)
    public_metadata = _public_agent_run_metadata(metadata)
    return {
        "id": task.id,
        "agent_run_id": task.id,
        "agent_id": task.agent_id,
        "kind": task.kind,
        "owner_session_run_id": task.owner_session_run_id,
        "source": task.source.value,
        "trigger_mode": task.trigger_mode.value,
        "status": task.status.value,
        "waiting_reason": task.waiting_reason.value if task.waiting_reason else None,
        "resume_policy": task.resume_policy.value if task.resume_policy else None,
        "runtime_profile_id": task.runtime_profile_id,
        "executor": task.executor.value if task.executor else None,
        "execution_location": (
            task.execution_location.value if task.execution_location else None
        ),
        "worktree_role": task.worktree_role.value if task.worktree_role else None,
        "publish_policy": task.publish_policy.value if task.publish_policy else None,
        "current_executor_session_id": task.executor_session_id,
        "current_activation_id": task.current_activation_id,
        "workdir": task.workdir,
        "sandbox_id": task.sandbox_id,
        "sandbox_session_id": task.sandbox_session_id,
        "workspace_ref": task.workspace_ref,
        "retention_scope": task.retention_scope,
        "cleanup_policy": task.cleanup_policy,
        "failure_reason": task.failure_reason,
        "cancel_reason": task.cancel_reason,
        "budget": _dict_from(metadata.get("budget")),
        "metadata": public_metadata,
    }


def _public_agent_run_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in AGENT_RUN_METADATA_ACTIVATION_KEYS
    }


def _agent_relation_completed_payload(
    task: AgentRun,
    *,
    owner_agent_run_id: str | None = None,
    task_prompt: str | None = None,
) -> dict[str, Any]:
    return agent_relation_completed_payload(
        task,
        owner_agent_run_id=owner_agent_run_id,
        task_prompt=task_prompt,
    )


def _activation_id_for_task(task_id: str, seq: int = 1) -> str:
    return f"{task_id}:activation:{seq}"


def _activation_seq_from_id(activation_id: str | None) -> int | None:
    text = str(activation_id or "").strip()
    if not text or ":activation:" not in text:
        return None
    try:
        return int(text.rsplit(":activation:", 1)[1])
    except (TypeError, ValueError):
        return None


def _activation_status_for_task(task: AgentRun) -> AgentRunActivationStatus:
    if task.status == AgentRunStatus.QUEUED:
        return AgentRunActivationStatus.QUEUED
    if task.status == AgentRunStatus.DISPATCHED:
        return AgentRunActivationStatus.DISPATCHED
    if task.status == AgentRunStatus.RUNNING:
        return AgentRunActivationStatus.RUNNING
    if task.status == AgentRunStatus.WAITING:
        return AgentRunActivationStatus.WAITING
    if task.status == AgentRunStatus.COMPLETED:
        return AgentRunActivationStatus.COMPLETED
    if task.status == AgentRunStatus.FAILED:
        return AgentRunActivationStatus.FAILED
    if task.status == AgentRunStatus.CANCELLED:
        return AgentRunActivationStatus.CANCELLED
    if task.status == AgentRunStatus.BLOCKED:
        return AgentRunActivationStatus.BLOCKED
    return AgentRunActivationStatus.QUEUED


def _activation_status_for_result(result: ExecutorRunResult) -> AgentRunActivationStatus:
    if result.succeeded:
        return AgentRunActivationStatus.COMPLETED
    if result.status == "cancelled":
        return AgentRunActivationStatus.CANCELLED
    if result.status == "blocked":
        return AgentRunActivationStatus.BLOCKED
    return AgentRunActivationStatus.FAILED


def _activation_from_task(
    task: AgentRun,
    *,
    activation_id: str | None = None,
    seq: int | None = None,
    input_kind: AgentRunActivationInputKind | str | None = None,
    input_payload: dict[str, Any] | None = None,
    prompt: str | None = None,
    status: AgentRunActivationStatus | None = None,
    request_id: str | None = None,
    worker_id: str | None = None,
    output: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> AgentRunActivation:
    resolved_seq = int(
        seq
        or _activation_seq_from_id(activation_id)
        or _activation_seq_from_id(task.current_activation_id)
        or 1
    )
    resolved_activation_id = str(
        activation_id
        or task.current_activation_id
        or _activation_id_for_task(task.id, resolved_seq)
    )
    resolved_input_kind = (
        getattr(input_kind, "value", input_kind)
        or AgentRunActivationInputKind.USER_REQUEST.value
    )
    resolved_input_payload = dict(input_payload or {})
    if not resolved_input_payload:
        resolved_input_payload = {"source": task.source.value}
    return AgentRunActivation(
        id=resolved_activation_id,
        agent_run_id=task.id,
        seq=resolved_seq,
        input_kind=resolved_input_kind,
        input_payload=resolved_input_payload,
        prompt=str(prompt or ""),
        status=status or _activation_status_for_task(task),
        output=output if output is not None else _terminal_output(task),
        result_payload=result_payload or {},
        worker_id=worker_id,
        request_id=request_id,
        metadata={
            "executor_session_id": task.executor_session_id,
            "runtime_profile_id": task.runtime_profile_id,
        },
    )


def _activation_with_runtime_state(
    activation: AgentRunActivation,
    *,
    status: AgentRunActivationStatus | None = None,
    request_id: str | None = None,
    worker_id: str | None = None,
    output: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> AgentRunActivation:
    return AgentRunActivation(
        id=activation.id,
        agent_run_id=activation.agent_run_id,
        seq=activation.seq,
        input_kind=activation.input_kind,
        input_payload=dict(activation.input_payload),
        prompt=activation.prompt,
        status=status or activation.status,
        output=output if output is not None else activation.output,
        result_payload=(
            dict(result_payload)
            if result_payload is not None
            else dict(activation.result_payload)
        ),
        worker_id=worker_id if worker_id is not None else activation.worker_id,
        request_id=request_id if request_id is not None else activation.request_id,
        started_at=activation.started_at,
        ended_at=activation.ended_at,
        metadata=dict(activation.metadata),
    )


def _activation_to_dict(activation: AgentRunActivation) -> dict[str, Any]:
    return {
        "id": activation.id,
        "activation_id": activation.id,
        "agent_run_id": activation.agent_run_id,
        "seq": activation.seq,
        "input_kind": activation.input_kind.value,
        "input_payload": dict(activation.input_payload),
        "prompt": activation.prompt,
        "status": activation.status.value,
        "output": activation.output,
        "result_payload": dict(activation.result_payload),
        "worker_id": activation.worker_id,
        "request_id": activation.request_id,
        "started_at": activation.started_at,
        "ended_at": activation.ended_at,
        "metadata": dict(activation.metadata),
    }


def _feedback_to_dict(feedback: AgentRunFeedback) -> dict[str, Any]:
    return {
        "id": feedback.id,
        "agent_run_id": feedback.agent_run_id,
        "source": feedback.source.value,
        "kind": feedback.kind.value,
        "payload": dict(feedback.payload),
        "created_at": feedback.created_at,
        "consumed_by_activation_id": feedback.consumed_by_activation_id,
        "visibility": feedback.visibility.value,
        "requires_activation": feedback.requires_activation,
        "metadata": dict(feedback.metadata),
    }


def _steer_to_dict(steer: ActivationSteer) -> dict[str, Any]:
    return {
        "id": steer.id,
        "activation_id": steer.activation_id,
        "source": steer.source.value,
        "payload": dict(steer.payload),
        "created_at": steer.created_at,
        "delivered_at": steer.delivered_at,
        "status": steer.status.value,
        "metadata": dict(steer.metadata),
    }


def _relation_to_dict(relation: AgentRunRelation) -> dict[str, Any]:
    return {
        "id": relation.id,
        "owner_agent_run_id": relation.owner_agent_run_id,
        "related_agent_run_id": relation.related_agent_run_id,
        "relation_type": relation.relation_type.value,
        "relation_scope": relation.relation_scope,
        "created_by_activation_id": relation.created_by_activation_id,
        "status": relation.status.value,
        "payload": dict(relation.payload),
        "metadata": dict(relation.metadata),
    }


def _relation_from_dict(data: dict[str, Any]) -> AgentRunRelation:
    return AgentRunRelation(
        id=str(data.get("id") or ""),
        owner_agent_run_id=str(data.get("owner_agent_run_id") or ""),
        related_agent_run_id=str(data.get("related_agent_run_id") or ""),
        relation_type=str(data.get("relation_type") or ""),
        relation_scope=str(data.get("relation_scope") or "session"),
        created_by_activation_id=(
            str(data["created_by_activation_id"])
            if data.get("created_by_activation_id") is not None
            else None
        ),
        status=str(data.get("status") or "active"),
        payload=_dict_from(data.get("payload")),
        metadata=_dict_from(data.get("metadata")),
    )


def _agent_thread_binding_to_dict(binding: AgentThreadBinding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "owner_session_run_id": binding.owner_session_run_id,
        "main_agent_run_id": binding.main_agent_run_id,
        "agent_id": binding.agent_id,
        "target_agent_run_id": binding.target_agent_run_id,
        "thread_key": binding.thread_key,
        "thread_summary": binding.thread_summary,
        "binding_lifetime": binding.binding_lifetime.value,
        "workdir_policy": binding.workdir_policy.value,
        "visibility": binding.visibility,
        "status": binding.status.value,
        "cleanup_policy": binding.cleanup_policy,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
        "metadata": dict(binding.metadata),
    }


def _session_run_binding_to_dict(binding: SessionRunBinding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "session_run_id": binding.session_run_id,
        "session_id": binding.session_id,
        "peer_id": binding.peer_id,
        "branch_binding_id": binding.branch_binding_id,
        "agent_run_id": binding.agent_run_id,
        "selected": binding.selected,
        "parent_branch_binding_id": binding.parent_branch_binding_id,
        "base_session_item_id": binding.base_session_item_id,
        "source_agent_run_id": binding.source_agent_run_id,
        "target_agent_run_id": binding.target_agent_run_id,
        "status": binding.status.value,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
        "metadata": dict(binding.metadata),
    }


def _initial_activation_for_request(
    metadata: dict[str, Any],
    *,
    task_id: str,
    request: "AgentRunRequest",
) -> tuple[dict[str, Any], AgentRunActivation]:
    result = dict(metadata)
    input_payload = {
        "source": request.source.value,
        "trigger_mode": request.trigger_mode.value,
    }
    worktree_branch = str(result.pop("worktree_branch", "") or "").strip()
    if worktree_branch:
        input_payload["worktree_branch"] = worktree_branch
    _validate_activation_input_payload(input_payload)
    activation = AgentRunActivation(
        id=_activation_id_for_task(task_id, 1),
        agent_run_id=task_id,
        seq=1,
        input_kind=AgentRunActivationInputKind.USER_REQUEST,
        input_payload=input_payload,
        prompt=request.prompt,
        status=AgentRunActivationStatus.QUEUED,
        metadata={
            "executor_session_id": request.executor_session_id,
            "runtime_profile_id": request.runtime_profile_id,
        },
    )
    return result, activation


def _artifact_to_dict(artifact: TaskArtifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "task_id": artifact.task_id,
        "type": artifact.type.value,
        "status": artifact.status.value,
        "branch_name": artifact.branch_name,
        "pr_url": artifact.pr_url,
        "content": artifact.content,
        "path": artifact.path,
        "metadata": dict(artifact.metadata),
        "merge_status": artifact.merge_status.value if artifact.merge_status else None,
        "merged_by": artifact.merged_by,
    }


def _dict_from(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _terminal_output(task: AgentRun) -> str:
    terminal_result = _dict_from(getattr(task, "terminal_result", {}))
    return str(terminal_result.get("output") or "")


def _set_terminal_result(task: AgentRun, *, output: str | None = None) -> None:
    task.terminal_result = {"output": str(output or "")}


def _worktree_branch_for_activation(activation: AgentRunActivation | None) -> str:
    input_payload = _dict_from(
        activation.input_payload if activation is not None else None
    )
    return str(input_payload.get("worktree_branch") or "").strip()


def _relation_from_submit_request(
    task_id: str,
    request: "AgentRunRequest",
) -> AgentRunRelation | None:
    relation = getattr(request, "relation", None)
    if relation is None:
        return None
    owner_agent_run_id = str(relation.owner_agent_run_id or "").strip()
    if not owner_agent_run_id or owner_agent_run_id == task_id:
        return None
    related_agent_run_id = str(relation.related_agent_run_id or "").strip()
    if related_agent_run_id and related_agent_run_id != task_id:
        raise ValueError(
            "AgentRunRequest.relation.related_agent_run_id must match submitted AgentRun id"
        )
    relation_type = relation.relation_type
    relation_id = (
        str(relation.id or "").strip()
        or f"relation:{owner_agent_run_id}:{task_id}:{relation_type.value}"
    )
    return AgentRunRelation(
        id=relation_id,
        owner_agent_run_id=owner_agent_run_id,
        related_agent_run_id=task_id,
        relation_type=relation_type,
        relation_scope=relation.relation_scope,
        created_by_activation_id=relation.created_by_activation_id,
        status=relation.status,
        payload=dict(relation.payload),
        metadata=dict(relation.metadata),
    )


_PERSISTENT_AGENT_CALL_METADATA_RESERVED = {
    "agent_run_source",
    "relation_type",
    "called_by_agent_run_id",
    "call_agent_mode",
    "purpose_key",
    "thread_key",
    "thread_summary",
    "wait",
    "parent_session_id",
    "parent_turn_id",
    "workspace_root",
}

_RELATION_PERMISSION_RECOMPUTE_POLICIES = {"recompute_or_reject"}
_RELATION_CLEANUP_POLICIES = {"delete_with_owner_session"}


def _external_persistent_agent_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(value or {})
    reserved = sorted(
        key for key in metadata if key in _PERSISTENT_AGENT_CALL_METADATA_RESERVED
    )
    if reserved:
        raise ValueError(
            "persistent Agent call metadata cannot override relation fields: "
            + ", ".join(reserved)
        )
    return metadata


def _normalize_permission_recompute_policy(value: str | None) -> str:
    policy = str(value or "recompute_or_reject").strip()
    if policy not in _RELATION_PERMISSION_RECOMPUTE_POLICIES:
        raise ValueError("permission_recompute_policy must be recompute_or_reject")
    return policy


def _normalize_relation_cleanup_policy(value: str | None) -> str:
    policy = str(value or "delete_with_owner_session").strip()
    if policy not in _RELATION_CLEANUP_POLICIES:
        raise ValueError("cleanup_policy must be delete_with_owner_session")
    return policy


def _require_relation_text(value: str | None, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _safe_binding_part(value: str) -> str:
    text = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text)
    return safe.strip("-") or "default"


def _agent_thread_binding_id(
    *,
    owner_session_run_id: str,
    main_agent_run_id: str,
    agent_id: str,
    thread_key: str,
    binding_lifetime: str,
) -> str:
    return "agent-thread-binding:" + ":".join(
        [
            _safe_binding_part(binding_lifetime),
            _safe_binding_part(owner_session_run_id or main_agent_run_id),
            _safe_binding_part(main_agent_run_id),
            _safe_binding_part(agent_id),
            _safe_binding_part(thread_key),
        ]
    )


def _session_run_binding_id(
    *,
    session_run_id: str,
    branch_binding_id: str = "",
) -> str:
    normalized_branch_binding_id = str(branch_binding_id or "").strip()
    if not normalized_branch_binding_id:
        raise ValueError("branch_binding_id is required")
    branch_part = _safe_binding_part(normalized_branch_binding_id)
    return "session-run-binding:" + ":".join(
        [
            _safe_binding_part(session_run_id),
            branch_part,
        ]
    )


def _agent_call_grant_key(
    *,
    user_id: str,
    grant_scope: str,
    main_agent_id: str,
    target_agent_id: str,
    conversation_scope: str,
    capability_scope: dict[str, Any] | None = None,
    target_config_version: str,
    **_extra: Any,
) -> tuple[str, str, str, str, str, str, str]:
    capability_scope_key = json.dumps(
        dict(capability_scope or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        str(user_id or "").strip(),
        str(grant_scope or "").strip(),
        str(main_agent_id or "").strip(),
        str(target_agent_id or "").strip(),
        str(conversation_scope or "").strip(),
        capability_scope_key,
        str(target_config_version or "").strip(),
    )


def _agent_call_grant_is_active(grant: AgentCallGrant | None) -> bool:
    if grant is None or str(grant.revoked_at or "").strip():
        return False
    expires_at = str(grant.expires_at or "").strip()
    if not expires_at:
        return True
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


_ACTIVE_STEER_AGENT_RUN_STATUSES = {
    AgentRunStatus.RUNNING,
}
_ACTIVE_AGENT_RUN_STATUSES = {
    AgentRunStatus.QUEUED,
    AgentRunStatus.DISPATCHED,
    AgentRunStatus.RUNNING,
    AgentRunStatus.WAITING,
}
_AGENT_CALL_FEEDBACK_KINDS = {
    AgentRunFeedbackKind.AGENT_CALL_RESULT,
    AgentRunFeedbackKind.AGENT_CALL_FAILED,
}


def _valid_agent_thread_key(value: str) -> bool:
    return all(ch.islower() or ch.isdigit() or ch in {"-", "_"} for ch in value)

_ACTIVATION_INPUT_FORBIDDEN_BUSINESS_KEYS = {
    "issue_id",
    "trigger_comment_id",
    "branch_name",
    "pr_url",
    "task_run_id",
    "taskflow_id",
    "workflow_id",
    "work_item_id",
    "goal_id",
}


def _validate_activation_input_payload(input_payload: dict[str, Any]) -> None:
    forbidden = sorted(
        key for key in input_payload if key in _ACTIVATION_INPUT_FORBIDDEN_BUSINESS_KEYS
    )
    if forbidden:
        raise ValueError(
            "AgentRunActivation.input_payload cannot store taskflow, GitHub, "
            "or external business identity fields: "
            + ", ".join(forbidden)
        )


def _waiting_reason_for_required_feedback(
    feedbacks: list[AgentRunFeedback],
) -> AgentRunWaitingReason | None:
    for feedback in feedbacks:
        if not feedback.requires_activation or feedback.consumed_by_activation_id:
            continue
        if feedback.kind == AgentRunFeedbackKind.USER_MESSAGE:
            return AgentRunWaitingReason.USER_INPUT
        if feedback.kind in {
            AgentRunFeedbackKind.AGENT_CALL_RESULT,
            AgentRunFeedbackKind.AGENT_CALL_FAILED,
        }:
            return AgentRunWaitingReason.AGENT_CALL
        return AgentRunWaitingReason.SERVER_PROCESSING
    return None


def _agent_call_feedback_kind(task: AgentRun) -> AgentRunFeedbackKind:
    if task.status == AgentRunStatus.COMPLETED:
        return AgentRunFeedbackKind.AGENT_CALL_RESULT
    return AgentRunFeedbackKind.AGENT_CALL_FAILED


def _agent_call_feedback_payload(
    task: AgentRun,
    *,
    relation: AgentRunRelation,
) -> dict[str, Any]:
    payload_data = dict(relation.payload)
    conversation_scope = str(payload_data.get("conversation_scope") or "").strip()
    if not conversation_scope:
        conversation_scope = (
            "persistent"
            if relation.relation_type == AgentRunRelationType.AGENT_CALL_PERSISTENT
            else "ephemeral"
        )
    status = task.status.value
    output = _terminal_output(task)
    payload = {
        "agent_id": task.agent_id,
        "target_agent_run_id": task.id,
        "conversation_scope": conversation_scope,
        "wait": bool(payload_data.get("wait") is True),
        "thread_key": str(payload_data.get("thread_key") or ""),
        "status": status,
        "summary": output if status == AgentRunStatus.COMPLETED.value else "",
        "evidence_refs": [],
        "artifact_refs": [],
        "metrics": {},
        "error_code": "",
        "message": "",
    }
    if status != AgentRunStatus.COMPLETED.value:
        payload["error_code"] = task.failure_reason or status
        payload["message"] = task.failure_reason or output or status
    return payload


def _agent_call_feedback_prompt(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    summary = str(payload.get("summary") or payload.get("message") or "").strip()
    if summary:
        return f"Agent {agent_id} finished with status {status}: {summary}"
    return f"Agent {agent_id} finished with status {status}"


def _activation_can_resume_from_feedback(
    activation: AgentRunActivation | None,
) -> bool:
    if activation is None:
        return True
    return activation.status in {
        AgentRunActivationStatus.COMPLETED,
        AgentRunActivationStatus.FAILED,
        AgentRunActivationStatus.CANCELLED,
        AgentRunActivationStatus.BLOCKED,
        AgentRunActivationStatus.WAITING,
    }


def _normalize_agent_run_budget(value: Any) -> dict[str, int]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("AgentRun budget must be an object")
    result: dict[str, int] = {}
    for key, raw in value.items():
        field_name = str(key or "").strip()
        if not field_name:
            continue
        if field_name not in RUNTIME_BUDGET_FIELDS:
            allowed = ", ".join(sorted(RUNTIME_BUDGET_FIELDS))
            raise ValueError(
                f"unsupported AgentRun budget field '{field_name}'; allowed: {allowed}"
            )
        if raw in (None, ""):
            continue
        try:
            amount = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"AgentRun budget field '{field_name}' must be a positive integer"
            ) from exc
        if amount <= 0:
            raise ValueError(
                f"AgentRun budget field '{field_name}' must be a positive integer"
            )
        result[field_name] = amount
    return result


def _agent_model_binding(raw_agent: dict[str, Any]) -> dict[str, Any]:
    raw_model = _dict_from(raw_agent.get("model"))
    provider = str(raw_model.get("provider") or "").strip()
    model = str(raw_model.get("model") or "").strip()
    if not provider or not model:
        return {}
    binding: dict[str, Any] = {"provider": provider, "model": model}
    if raw_model.get("display_name"):
        binding["display_name"] = str(raw_model["display_name"])
    parameters = _dict_from(raw_model.get("parameters"))
    if parameters:
        binding["parameters"] = parameters
    return binding


def _string_list_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _can_resume_from_parent(request: "AgentRunRequest", parent: AgentRun) -> bool:
    if not parent.executor_session_id:
        return False
    return (
        request.agent_id == parent.agent_id
        and request.runtime_profile_id == parent.runtime_profile_id
        and request.executor == parent.executor
        and request.execution_location == parent.execution_location
        and workspace_key(request.workdir) == workspace_key(parent.workdir)
    )


def _coerce_source(value: AgentRunSource | str | None) -> AgentRunSource:
    if isinstance(value, AgentRunSource):
        return value
    if value is None or str(value).strip() == "":
        return AgentRunSource.MANUAL
    return AgentRunSource(str(value))


@dataclass
class AgentRunRequest:
    """Request accepted by the AgentRun control plane."""

    agent_id: str
    prompt: str
    owner_session_run_id: str = ""
    source: AgentRunSource | str = AgentRunSource.MANUAL
    executor: ExecutorType | str | None = None
    execution_location: ExecutionLocation | str | None = None
    worker_kind: WorkerKind | str | None = None
    model_request_origin: ModelRequestOrigin | str | None = None
    worktree_role: WorktreeRole | str | None = None
    publish_policy: PublishPolicy | str | None = None
    trigger_mode: TriggerMode | str = TriggerMode.ISSUE_TASK
    runtime_profile_id: str | None = None
    workdir: str | None = None
    executor_session_id: str | None = None
    model: str | None = None
    sandbox_id: str | None = None
    sandbox_session_id: str | None = None
    workspace_ref: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    relation: AgentRunRelation | None = None

    def __post_init__(self) -> None:
        self.source = _coerce_source(self.source)
        self.executor = _optional_executor(self.executor)
        self.execution_location = _optional_location(self.execution_location)
        self.worker_kind = optional_worker_kind(self.worker_kind)
        self.model_request_origin = optional_model_request_origin(
            self.model_request_origin
        )
        self.worktree_role = optional_worktree_role(self.worktree_role)
        self.publish_policy = optional_publish_policy(self.publish_policy)
        self.budget = _normalize_agent_run_budget(self.budget)
        if not isinstance(self.trigger_mode, TriggerMode):
            self.trigger_mode = TriggerMode(str(self.trigger_mode))
        self.metadata = _dict_from(self.metadata)
        forbidden_metadata = sorted(
            key for key in self.metadata if key in AGENT_RUN_METADATA_FORBIDDEN_KEYS
        )
        if forbidden_metadata:
            raise ValueError(
                "AgentRunRequest.metadata cannot store AgentRun relation or "
                "external business identity fields: "
                + ", ".join(forbidden_metadata)
            )


class AgentCallDispatchError(ValueError):
    """Structured Agent call dispatch failure surfaced as tool feedback."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "").strip() or "agent_call_failed"
        self.message = str(message or "").strip() or self.code
        super().__init__(self.message)


@dataclass
class AgentRunEvent:
    """Ordered AgentRun event stored by the control plane."""

    task_id: str
    seq: int
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.task_id,
            "seq": self.seq,
            "type": self.type,
            "payload": dict(self.payload),
        }

@dataclass
class AgentRunActivationClaim:
    """Activation payload returned to a worker after a successful claim."""

    request_id: str
    worker_id: str
    task: AgentRun
    executor_request: ExecutorRunRequest
    runtime_snapshot: dict[str, Any] = field(default_factory=dict)
    activation: AgentRunActivation | None = None

    def __post_init__(self) -> None:
        if self.activation is None:
            self.activation = _activation_from_task(
                self.task,
                status=AgentRunActivationStatus.DISPATCHED,
                request_id=self.request_id,
                worker_id=self.worker_id,
            )
        metadata = dict(self.executor_request.metadata)
        metadata.setdefault("activation_id", self.activation_id)
        metadata.setdefault("agent_run_id", self.task.id)
        self.executor_request.metadata = metadata

    @property
    def activation_id(self) -> str:
        return self.activation.id if self.activation is not None else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "activation_id": self.activation_id,
            "activation": (
                _activation_to_dict(self.activation)
                if self.activation is not None
                else None
            ),
            "agent_run": _agent_run_to_dict(self.task),
            "executor_request": self.executor_request.to_dict(),
            "runtime_snapshot": dict(self.runtime_snapshot),
        }

@dataclass
class PRArtifactResult:
    """Result returned by a PR flow implementation."""

    branch_name: str
    pr_url: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PRFlow(Protocol):
    """Protocol for creating or updating a task pull request artifact."""

    def create_or_update(
        self,
        task: AgentRun,
        *,
        diff: str = "",
        branch_name: str | None = None,
    ) -> PRArtifactResult:
        """Create or update a pull request for task output."""


class InMemoryPRFlow:
    """Deterministic PR flow used by tests and local dry runs."""

    def __init__(self, base_url: str = "https://example.invalid/pr") -> None:
        self.base_url = base_url.rstrip("/")

    def create_or_update(
        self,
        task: AgentRun,
        *,
        diff: str = "",
        branch_name: str | None = None,
    ) -> PRArtifactResult:
        branch = branch_name or f"agent/{task.agent_id}/{task.id[:12]}"
        return PRArtifactResult(
            branch_name=branch,
            pr_url=f"{self.base_url}/{task.id}",
            metadata={"diff_bytes": len(diff.encode("utf-8"))},
        )


class AgentRunControlPlane:
    """In-memory control plane for scheduling executors and recording run lifecycle.

    The service is deliberately storage-agnostic. The public methods are the
    contract that a persistent implementation and HTTP relay endpoints can keep.
    Executor tool systems and provider sessions remain owned by each executor.
    """

    def __init__(
        self,
        *,
        max_running_tasks: int = 4,
        runtime_snapshot: dict[str, Any] | None = None,
        pr_flow: PRFlow | None = None,
        store: AgentRunStore | None = None,
        sandbox_provider: SandboxProvider | None = None,
        sandbox_profile: SandboxProfile | None = None,
    ) -> None:
        self.max_running_tasks = max(1, int(max_running_tasks or 1))
        self.runtime_snapshot = (
            dict(runtime_snapshot)
            if runtime_snapshot is not None
            else build_agent_run_snapshot(
                agent_registry=AgentRegistryConfig(),
                runtime_profiles=RuntimeProfilesConfig(),
                run_limits=RunLimitsConfig(),
            )
        )
        self.pr_flow = pr_flow or InMemoryPRFlow()
        self._store = store
        if self._store is not None:
            if runtime_snapshot is not None:
                self._store.configure(
                    max_running_tasks=self.max_running_tasks,
                    runtime_snapshot=self.runtime_snapshot,
                )
            else:
                self.max_running_tasks = max(
                    1,
                    int(
                        getattr(
                            self._store,
                            "max_running_tasks",
                            self.max_running_tasks,
                        )
                        or 1
                    ),
                )
                store_runtime_snapshot = dict(
                    getattr(self._store, "runtime_snapshot", {}) or {}
                )
                if store_runtime_snapshot:
                    self.runtime_snapshot = store_runtime_snapshot
                else:
                    self._store.configure(
                        max_running_tasks=self.max_running_tasks,
                        runtime_snapshot=self.runtime_snapshot,
                    )
                    self.runtime_snapshot = dict(
                        getattr(self._store, "runtime_snapshot", {}) or self.runtime_snapshot
                    )
        self._sandbox_provider = sandbox_provider
        self._sandbox_profile = sandbox_profile
        self._lock = threading.RLock()
        self._states: dict[str, TaskLifecycleState] = {}
        self._sessions: dict[str, TaskSessionRef] = {}
        self._events: dict[str, list[AgentRunEvent]] = {}
        self._activations: dict[str, AgentRunActivation] = {}
        self._feedbacks: dict[str, list[AgentRunFeedback]] = {}
        self._steers: dict[str, list[ActivationSteer]] = {}
        self._relations: dict[str, AgentRunRelation] = {}
        self._agent_thread_bindings: dict[str, AgentThreadBinding] = {}
        self._session_run_bindings: dict[str, SessionRunBinding] = {}
        self._agent_call_grants: dict[
            tuple[str, str, str, str, str, str, str],
            AgentCallGrant,
        ] = {}
        self._claims: dict[str, AgentRunActivationClaim] = {}
        self._claim_leases: dict[str, dict[str, Any]] = {}
        self._cancel_requests: dict[str, str] = {}
        self._wakeup = threading.Condition()

    def configure_sandbox_provider(
        self,
        provider: SandboxProvider | None,
        profile: SandboxProfile | None = None,
    ) -> None:
        """Attach or replace the execution-room provider for new AgentRuns."""

        with self._lock:
            self._sandbox_provider = provider
            self._sandbox_profile = profile

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Refresh runtime config without dropping queued/running task state."""

        with self._lock:
            if self._store is not None:
                self._store.configure(
                    max_running_tasks=max_running_tasks,
                    runtime_snapshot=runtime_snapshot,
                )
                self.max_running_tasks = self._store.max_running_tasks
                self.runtime_snapshot = dict(self._store.runtime_snapshot)
                return
            if max_running_tasks is not None:
                self.max_running_tasks = max(1, int(max_running_tasks or 1))
            if runtime_snapshot is not None:
                self.runtime_snapshot = dict(runtime_snapshot)

    def submit_agent_run(
        self, request: AgentRunRequest, *, task_id: str | None = None
    ) -> AgentRun:
        task_id = task_id or _new_id("task")
        if self._store is not None:
            request = self._resolve_request_against_snapshot(request)
            _ensure_reuleauxcoder_executor_session(request, task_id)
            metadata = dict(request.metadata)
            if request.worktree_role is not None:
                metadata.setdefault("worktree_role", request.worktree_role.value)
            if request.publish_policy is not None:
                metadata.setdefault("publish_policy", request.publish_policy.value)
            metadata, activation = _initial_activation_for_request(
                metadata,
                task_id=task_id,
                request=request,
            )
            request.metadata = self._metadata_with_snapshot_capabilities(
                metadata,
                agent_id=request.agent_id,
                source=request.source,
                runtime_profile_id=request.runtime_profile_id,
            )
            sandbox_error = self._prepare_sandbox_session(request, task_id)
            task = self._store.submit_agent_run(request, task_id=task_id)
            task.current_activation_id = activation.id
            if sandbox_error:
                task = self._store.fail_agent_run(task.id, error=sandbox_error)
            self.notify_task_available()
            return task
        with self._lock:
            request = self._resolve_request_locked(request)
            _ensure_reuleauxcoder_executor_session(request, task_id)
        sandbox_error = self._prepare_sandbox_session(request, task_id)
        with self._lock:
            metadata = dict(request.metadata)
            if request.sandbox_id:
                metadata.setdefault("sandbox_id", request.sandbox_id)
            if request.sandbox_session_id:
                metadata.setdefault("sandbox_session_id", request.sandbox_session_id)
            if request.workspace_ref:
                metadata.setdefault("workspace_ref", request.workspace_ref)
            if request.model is not None:
                metadata.setdefault("model", request.model)
            if request.budget:
                metadata.setdefault("budget", dict(request.budget))
            if request.worker_kind is not None:
                metadata.setdefault("worker_kind", request.worker_kind.value)
            if request.model_request_origin is not None:
                metadata.setdefault(
                    "model_request_origin",
                    request.model_request_origin.value,
                )
            if request.worktree_role is not None:
                metadata.setdefault("worktree_role", request.worktree_role.value)
            if request.publish_policy is not None:
                metadata.setdefault("publish_policy", request.publish_policy.value)
            metadata, activation = _initial_activation_for_request(
                metadata,
                task_id=task_id,
                request=request,
            )
            metadata = self._metadata_with_snapshot_capabilities(
                metadata,
                agent_id=request.agent_id,
                source=request.source,
                runtime_profile_id=request.runtime_profile_id,
            )
            relation = _relation_from_submit_request(task_id, request)
            task = AgentRun(
                id=task_id,
                agent_id=request.agent_id,
                owner_session_run_id=str(request.owner_session_run_id or ""),
                source=request.source,
                trigger_mode=request.trigger_mode,
                status=AgentRunStatus.QUEUED,
                runtime_profile_id=request.runtime_profile_id,
                executor=request.executor,
                execution_location=request.execution_location,
                worktree_role=request.worktree_role,
                publish_policy=request.publish_policy,
                executor_session_id=request.executor_session_id,
                current_activation_id=activation.id,
                workdir=request.workdir,
                sandbox_id=request.sandbox_id,
                sandbox_session_id=request.sandbox_session_id,
                workspace_ref=request.workspace_ref,
                metadata=metadata,
            )
            self._states[task.id] = TaskLifecycleState(task=task)
            self._events[task.id] = []
            self._activations[activation.id] = activation
            if relation is not None:
                self._relations[relation.id] = relation
            self._append_event_locked(task.id, "queued", {"agent_run": _agent_run_to_dict(task)})
            if relation is not None:
                self._append_event_locked(
                    task.id,
                    "agent_run_relation_created",
                    {"relation": _relation_to_dict(relation)},
                )
                if relation.owner_agent_run_id in self._events:
                    self._append_event_locked(
                        relation.owner_agent_run_id,
                        "agent_run_relation_created",
                        {"relation": _relation_to_dict(relation)},
                    )
            self._append_event_locked(
                task.id,
                "activation_queued",
                {"activation": _activation_to_dict(activation)},
            )
            if task.sandbox_session_id:
                self._append_event_locked(
                    task.id,
                    "sandbox_session_started",
                    {
                        "sandbox_id": task.sandbox_id,
                        "sandbox_session_id": task.sandbox_session_id,
                        "workspace_ref": task.workspace_ref,
                        "workdir": task.workdir,
                    },
                )
            if sandbox_error:
                task.status = AgentRunStatus.FAILED
                _set_terminal_result(task, output=sandbox_error)
                task.failure_reason = sandbox_error
                task.cancel_reason = None
                self._append_event_locked(task.id, "failed", {"error": sandbox_error})
        self.notify_task_available()
        return task

    def call_persistent_agent(
        self,
        *,
        owner_agent_run_id: str,
        owner_session_run_id: str,
        agent_id: str,
        prompt: str,
        thread_key: str = "",
        thread_summary: str = "",
        wait: bool = True,
        workdir: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        """Create or reuse a persistent Agent thread binding for one mainline."""

        main_agent_run_id = str(owner_agent_run_id or "").strip()
        target_agent_id = str(agent_id or "").strip()
        stable_thread_key = str(thread_key or "").strip()
        session_run_id = str(owner_session_run_id or "").strip()
        stable_thread_summary = str(thread_summary or "").strip()
        if not main_agent_run_id:
            raise ValueError("owner_agent_run_id is required for persistent Agent call")
        if not target_agent_id:
            raise ValueError("agent_id is required for persistent Agent call")
        if stable_thread_key and not _valid_agent_thread_key(stable_thread_key):
            raise AgentCallDispatchError(
                "invalid_agent_call_arguments",
                "thread_key must contain only lowercase letters, digits, '-' or '_'",
            )
        existing = self.find_agent_thread_binding(
            owner_session_run_id=session_run_id,
            main_agent_run_id=main_agent_run_id,
            agent_id=target_agent_id,
            thread_key=stable_thread_key,
            binding_lifetime=AgentThreadBindingLifetime.SESSION.value,
            include_inactive=True,
        )
        if existing is not None and existing.status == AgentThreadBindingStatus.UNAVAILABLE:
            raise AgentCallDispatchError(
                "agent_thread_unavailable",
                "persistent Agent thread is unavailable",
            )
        if existing is not None and existing.status != AgentThreadBindingStatus.ACTIVE:
            existing = None
        if existing is not None:
            if (
                stable_thread_summary
                and existing.thread_summary
                and stable_thread_summary != existing.thread_summary
            ):
                raise AgentCallDispatchError(
                    "agent_thread_summary_mismatch",
                    "persistent Agent thread summary does not match existing binding",
                )
            target = self.get_agent_run(existing.target_agent_run_id)
            if target.status in _ACTIVE_AGENT_RUN_STATUSES:
                raise AgentCallDispatchError(
                    "agent_thread_busy",
                    "persistent Agent thread already has an active activation",
                )
            return self.continue_agent_run(
                existing.target_agent_run_id,
                input_kind=AgentRunActivationInputKind.USER_REQUEST,
                input_payload={
                    "conversation_scope": "persistent",
                    "owner_agent_run_id": main_agent_run_id,
                    "binding_id": existing.id,
                    "thread_key": stable_thread_key,
                    "wait": bool(wait),
                },
                resume_session=True,
                prompt=str(prompt or "").strip(),
            )

        if not stable_thread_summary:
            raise AgentCallDispatchError(
                "invalid_agent_call_arguments",
                "thread_summary is required when creating a persistent Agent thread",
            )
        external_metadata = _external_persistent_agent_metadata(metadata)
        relation_payload = {
            "conversation_scope": "persistent",
            "wait": bool(wait),
            "thread_key": stable_thread_key,
            "thread_summary": stable_thread_summary,
        }
        if session_run_id:
            relation_payload["parent_session_id"] = session_run_id
        if workdir:
            relation_payload["workspace_root"] = str(workdir)
        run = self.submit_agent_run(
            AgentRunRequest(
                agent_id=target_agent_id,
                prompt=str(prompt or "").strip(),
                owner_session_run_id=session_run_id,
                source=AgentRunSource.DELEGATION,
                workdir=str(workdir) if workdir else None,
                metadata=external_metadata,
                relation=AgentRunRelation(
                    id="",
                    owner_agent_run_id=main_agent_run_id,
                    related_agent_run_id="",
                    relation_type=AgentRunRelationType.AGENT_CALL_PERSISTENT,
                    payload=relation_payload,
                ),
            )
        )
        binding = AgentThreadBinding(
            id=_agent_thread_binding_id(
                owner_session_run_id=session_run_id,
                main_agent_run_id=main_agent_run_id,
                agent_id=target_agent_id,
                thread_key=stable_thread_key,
                binding_lifetime=AgentThreadBindingLifetime.SESSION.value,
            ),
            owner_session_run_id=session_run_id,
            main_agent_run_id=main_agent_run_id,
            agent_id=target_agent_id,
            target_agent_run_id=run.id,
            thread_key=stable_thread_key,
            thread_summary=stable_thread_summary,
            metadata={
                "created_by_agent_run_id": main_agent_run_id,
            },
        )
        self.upsert_agent_thread_binding(binding)
        return run

    def branch_agent_run(
        self,
        *,
        source_agent_run_id: str,
        base_session_item_id: str,
        runtime_root: str,
        repo_root: str | None = None,
        agent_id: str | None = None,
        prompt: str = "",
        task_id: str | None = None,
        branch_binding_id: str | None = None,
        branch_name: str | None = None,
        base_ref: str = "HEAD",
        permission_recompute_policy: str | None = None,
        cleanup_policy: str | None = None,
        executor: ExecutorType | str | None = None,
        runtime_profile_id: str | None = None,
        model: str | None = None,
        select_branch: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        """Create a real git branch/worktree and a related target AgentRun."""

        source_run_id = _require_relation_text(
            source_agent_run_id,
            field="source_agent_run_id",
        )
        base_item_id = _require_relation_text(
            base_session_item_id,
            field="base_session_item_id",
        )
        runtime_root_value = _require_relation_text(runtime_root, field="runtime_root")
        source = self.get_agent_run(source_run_id)
        target_task_id = task_id or _new_id("task")
        target_agent_id = str(agent_id or source.agent_id or "").strip()
        if not target_agent_id:
            raise ValueError("agent_id is required")
        source_workspace_root = _require_relation_text(
            repo_root or source.workdir,
            field="repo_root",
        )
        policy = _normalize_permission_recompute_policy(permission_recompute_policy)
        cleanup = _normalize_relation_cleanup_policy(cleanup_policy)
        manager = WorktreeManager(runtime_root_value)
        plan = manager.plan(
            workspace_id=source.owner_session_run_id or source.workspace_ref or source.id,
            task_id=target_task_id,
            agent_id=target_agent_id,
            branch_name=branch_name,
        )
        prepared = manager.create_branch_worktree(
            source_repo=source_workspace_root,
            plan=plan,
            base_ref=base_ref,
        )
        relation_payload = {
            "source_agent_run_id": source.id,
            "target_agent_run_id": target_task_id,
            "base_session_item_id": base_item_id,
            "base_git_ref": prepared.base_git_ref,
            "base_tree_ref": prepared.base_tree_ref,
            "branch_name": prepared.branch_name,
            "branch_git_ref": prepared.branch_git_ref,
            "branch_worktree_ref": prepared.branch_worktree_ref,
            "permission_recompute_policy": policy,
            "reuse_live_executor_session": False,
            "cleanup_policy": cleanup,
            "runtime_root": str(prepared.runtime_root),
            "source_workspace_root": str(prepared.source_repo),
        }
        try:
            branch_run = self.submit_agent_run(
                AgentRunRequest(
                    agent_id=target_agent_id,
                    prompt=str(prompt or ""),
                    owner_session_run_id=source.owner_session_run_id,
                    source=source.source,
                    executor=executor or source.executor,
                    execution_location=ExecutionLocation.DAEMON_WORKTREE,
                    worktree_role=WorktreeRole.TARGET,
                    publish_policy=PublishPolicy.BRANCH,
                    trigger_mode=source.trigger_mode,
                    runtime_profile_id=runtime_profile_id or source.runtime_profile_id,
                    workdir=prepared.branch_worktree_ref,
                    model=model,
                    workspace_ref=prepared.branch_worktree_ref,
                    metadata=dict(metadata or {}),
                    relation=AgentRunRelation(
                        id="",
                        owner_agent_run_id=source.id,
                        related_agent_run_id=target_task_id,
                        relation_type=AgentRunRelationType.BRANCH,
                        payload=relation_payload,
                    ),
                ),
                task_id=target_task_id,
            )
            self._create_derived_session_run_binding(
                source=source,
                target=branch_run,
                relation_type=AgentRunRelationType.BRANCH,
                base_session_item_id=base_item_id,
                branch_binding_id=branch_binding_id,
                target_session_run_id=source.owner_session_run_id,
                selected=select_branch,
            )
            return branch_run
        except Exception:
            manager.cleanup_branch_worktree(
                source_repo=prepared.source_repo,
                branch_name=prepared.branch_name,
                worktree_path=prepared.worktree_path,
                delete_branch=True,
            )
            raise

    def fork_agent_run(
        self,
        *,
        source_agent_run_id: str,
        base_session_item_id: str,
        fork_workspace_ref: str,
        target_owner_session_run_id: str,
        agent_id: str | None = None,
        prompt: str = "",
        task_id: str | None = None,
        branch_binding_id: str | None = None,
        provenance_status: str = "visible",
        permission_recompute_policy: str | None = None,
        cleanup_policy: str | None = None,
        workdir: str | None = None,
        executor: ExecutorType | str | None = None,
        execution_location: ExecutionLocation | str | None = None,
        runtime_profile_id: str | None = None,
        model: str | None = None,
        select_branch: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRun:
        """Create a derived AgentRun through the relation control-plane path."""

        source_run_id = _require_relation_text(
            source_agent_run_id,
            field="source_agent_run_id",
        )
        base_item_id = _require_relation_text(
            base_session_item_id,
            field="base_session_item_id",
        )
        fork_ref = _require_relation_text(
            fork_workspace_ref,
            field="fork_workspace_ref",
        )
        target_owner_id = _require_relation_text(
            target_owner_session_run_id,
            field="target_owner_session_run_id",
        )
        provenance = str(provenance_status or "visible").strip()
        if provenance not in {"visible", "redacted", "unavailable"}:
            raise ValueError(
                "provenance_status must be visible, redacted, or unavailable"
            )
        source = self.get_agent_run(source_run_id)
        target_task_id = task_id or _new_id("task")
        target_agent_id = str(agent_id or source.agent_id or "").strip()
        if not target_agent_id:
            raise ValueError("agent_id is required")
        relation_payload = {
            "source_agent_run_id": source.id,
            "target_agent_run_id": target_task_id,
            "base_session_item_id": base_item_id,
            "fork_workspace_ref": fork_ref,
            "source_owner_session_run_id": source.owner_session_run_id,
            "target_owner_session_run_id": target_owner_id,
            "source_workspace_ref": source.workspace_ref or "",
            "permission_recompute_policy": _normalize_permission_recompute_policy(
                permission_recompute_policy
            ),
            "reuse_live_executor_session": False,
            "cleanup_policy": _normalize_relation_cleanup_policy(cleanup_policy),
            "provenance_status": provenance,
        }
        fork_run = self.submit_agent_run(
            AgentRunRequest(
                agent_id=target_agent_id,
                prompt=str(prompt or ""),
                owner_session_run_id=target_owner_id,
                source=source.source,
                executor=executor,
                execution_location=execution_location,
                trigger_mode=source.trigger_mode,
                runtime_profile_id=runtime_profile_id,
                workdir=str(workdir) if workdir is not None else None,
                model=model,
                workspace_ref=fork_ref,
                metadata=dict(metadata or {}),
                relation=AgentRunRelation(
                    id="",
                    owner_agent_run_id=source.id,
                    related_agent_run_id=target_task_id,
                    relation_type=AgentRunRelationType.FORK,
                    payload=relation_payload,
                ),
            ),
            task_id=target_task_id,
        )
        self._create_derived_session_run_binding(
            source=source,
            target=fork_run,
            relation_type=AgentRunRelationType.FORK,
            base_session_item_id=base_item_id,
            branch_binding_id=branch_binding_id,
            target_session_run_id=target_owner_id,
            selected=select_branch,
        )
        return fork_run

    def mark_agent_call_waiting(
        self,
        owner_agent_run_id: str,
        *,
        target_agent_run_id: str,
        conversation_scope: str,
        thread_key: str = "",
        wait: bool = True,
    ) -> None:
        if not wait:
            return
        owner_run_id = str(owner_agent_run_id or "").strip()
        target_run_id = str(target_agent_run_id or "").strip()
        if not owner_run_id or not target_run_id:
            return
        if self._store is not None:
            marker = getattr(self._store, "mark_agent_call_waiting", None)
            if callable(marker):
                marker(
                    owner_run_id,
                    target_agent_run_id=target_run_id,
                    conversation_scope=conversation_scope,
                    thread_key=thread_key,
                    wait=True,
                )
                return
            raise RuntimeError("AgentRun store does not support agent call waiting")
        with self._lock:
            task = self._task_locked(owner_run_id)
            if task.is_terminal:
                return
            task.status = AgentRunStatus.WAITING
            task.waiting_reason = AgentRunWaitingReason.AGENT_CALL
            task.resume_policy = AgentRunResumePolicy.EXTERNAL_EVENT
            task.terminal_result = {}
            self._append_event_locked(
                owner_run_id,
                "waiting",
                {
                    "agent_run": _agent_run_to_dict(task),
                    "waiting_reason": AgentRunWaitingReason.AGENT_CALL.value,
                    "resume_policy": AgentRunResumePolicy.EXTERNAL_EVENT.value,
                    "target_agent_run_id": target_run_id,
                    "conversation_scope": str(conversation_scope or "").strip(),
                    "thread_key": str(thread_key or "").strip(),
                },
            )

    def find_agent_thread_binding(
        self,
        *,
        owner_session_run_id: str,
        main_agent_run_id: str,
        agent_id: str,
        thread_key: str = "",
        binding_lifetime: str = "session",
        include_inactive: bool = False,
    ) -> AgentThreadBinding | None:
        if self._store is not None:
            finder = getattr(self._store, "find_agent_thread_binding", None)
            if callable(finder):
                return finder(
                    owner_session_run_id=owner_session_run_id,
                    main_agent_run_id=main_agent_run_id,
                    agent_id=agent_id,
                    thread_key=thread_key,
                    binding_lifetime=binding_lifetime,
                    include_inactive=include_inactive,
                )
            raise RuntimeError("AgentRun store does not support Agent thread bindings")
        with self._lock:
            for binding in self._agent_thread_bindings.values():
                if (
                    (include_inactive or binding.status == AgentThreadBindingStatus.ACTIVE)
                    and binding.binding_lifetime
                    == AgentThreadBindingLifetime(binding_lifetime)
                    and binding.owner_session_run_id
                    == str(owner_session_run_id or "").strip()
                    and binding.main_agent_run_id == str(main_agent_run_id or "").strip()
                    and binding.agent_id == str(agent_id or "").strip()
                    and binding.thread_key == str(thread_key or "").strip()
                ):
                    return binding
        return None

    def upsert_agent_thread_binding(self, binding: AgentThreadBinding) -> None:
        if self._store is not None:
            writer = getattr(self._store, "upsert_agent_thread_binding", None)
            if callable(writer):
                writer(binding)
                return
            raise RuntimeError("AgentRun store does not support Agent thread bindings")
        with self._lock:
            self._agent_thread_bindings[binding.id] = binding
            for task_id in (binding.main_agent_run_id, binding.target_agent_run_id):
                if task_id in self._events:
                    self._append_event_locked(
                        task_id,
                        "agent_thread_binding_upserted",
                        {"binding": _agent_thread_binding_to_dict(binding)},
                    )

    def list_agent_thread_bindings(self, **filters: Any) -> list[AgentThreadBinding]:
        if self._store is not None:
            lister = getattr(self._store, "list_agent_thread_bindings", None)
            if callable(lister):
                return lister(**filters)
            raise RuntimeError("AgentRun store does not support Agent thread bindings")
        with self._lock:
            bindings = list(self._agent_thread_bindings.values())
            for key, value in filters.items():
                if value is None:
                    continue
                expected = getattr(value, "value", value)
                filtered: list[AgentThreadBinding] = []
                for binding in bindings:
                    actual = getattr(binding, key, "")
                    actual_value = getattr(actual, "value", actual)
                    if str(actual_value) == str(expected):
                        filtered.append(binding)
                bindings = filtered
            return bindings

    def create_session_run_binding(
        self,
        *,
        session_run_id: str,
        agent_run_id: str,
        peer_id: str = "",
        session_id: str = "",
        branch_binding_id: str = "",
        selected: bool = True,
        parent_branch_binding_id: str = "",
        base_session_item_id: str = "",
        source_agent_run_id: str = "",
        target_agent_run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionRunBinding:
        normalized_session_run_id = str(session_run_id or "").strip()
        normalized_agent_run_id = str(agent_run_id or "").strip()
        if not normalized_session_run_id:
            raise ValueError("session_run_id is required")
        if not normalized_agent_run_id:
            raise ValueError("agent_run_id is required")
        normalized_branch_binding_id = str(branch_binding_id or "").strip()
        if not normalized_branch_binding_id:
            raise ValueError("branch_binding_id is required")
        binding_id = _session_run_binding_id(
            session_run_id=normalized_session_run_id,
            branch_binding_id=normalized_branch_binding_id,
        )
        binding = SessionRunBinding(
            id=binding_id,
            session_run_id=normalized_session_run_id,
            session_id=str(session_id or "").strip(),
            peer_id=str(peer_id or "").strip(),
            branch_binding_id=normalized_branch_binding_id,
            agent_run_id=normalized_agent_run_id,
            selected=selected,
            parent_branch_binding_id=str(parent_branch_binding_id or "").strip(),
            base_session_item_id=str(base_session_item_id or "").strip(),
            source_agent_run_id=str(source_agent_run_id or "").strip(),
            target_agent_run_id=str(target_agent_run_id or normalized_agent_run_id).strip(),
            metadata=dict(metadata or {}),
        )
        self.upsert_session_run_binding(binding)
        return binding

    def _create_derived_session_run_binding(
        self,
        *,
        source: AgentRun,
        target: AgentRun,
        relation_type: AgentRunRelationType,
        base_session_item_id: str,
        branch_binding_id: str | None,
        target_session_run_id: str,
        selected: bool,
    ) -> SessionRunBinding | None:
        source_session_run_id = str(source.owner_session_run_id or "").strip()
        target_session_run_id = str(target_session_run_id or "").strip()
        if not source_session_run_id or not target_session_run_id:
            return None
        source_bindings = [
            binding
            for binding in self.list_session_run_bindings(
                session_run_id=source_session_run_id,
                status=SessionRunBindingStatus.ACTIVE,
            )
            if binding.agent_run_id == source.id
            or binding.target_agent_run_id == source.id
        ]
        if not source_bindings:
            return None
        source_bindings.sort(
            key=lambda binding: (
                not binding.selected,
                str(binding.updated_at or binding.created_at or ""),
            )
        )
        source_binding = source_bindings[0]
        derived_branch_id = str(branch_binding_id or "").strip()
        if not derived_branch_id:
            derived_branch_id = f"{relation_type.value}-{target.id}"
        return self.create_session_run_binding(
            session_run_id=target_session_run_id,
            session_id=source_binding.session_id,
            peer_id=source_binding.peer_id,
            agent_run_id=target.id,
            branch_binding_id=derived_branch_id,
            selected=selected,
            parent_branch_binding_id=source_binding.branch_binding_id,
            base_session_item_id=str(base_session_item_id or "").strip(),
            source_agent_run_id=source.id,
            target_agent_run_id=target.id,
            metadata={
                "binding_kind": "derived",
                "relation_type": relation_type.value,
                "source_binding_id": source_binding.id,
                "source_branch_binding_id": source_binding.branch_binding_id,
            },
        )

    def find_session_run_binding(
        self,
        *,
        session_run_id: str,
        branch_binding_id: str = "",
        selected_only: bool = True,
        include_inactive: bool = False,
    ) -> SessionRunBinding | None:
        if self._store is not None:
            finder = getattr(self._store, "find_session_run_binding", None)
            if callable(finder):
                return finder(
                    session_run_id=session_run_id,
                    branch_binding_id=branch_binding_id,
                    selected_only=selected_only,
                    include_inactive=include_inactive,
                )
            raise RuntimeError("AgentRun store does not support SessionRun bindings")
        normalized_session_run_id = str(session_run_id or "").strip()
        normalized_branch_binding_id = str(branch_binding_id or "").strip()
        with self._lock:
            candidates = [
                binding
                for binding in self._session_run_bindings.values()
                if binding.session_run_id == normalized_session_run_id
                and (include_inactive or binding.status == SessionRunBindingStatus.ACTIVE)
            ]
            if normalized_branch_binding_id:
                candidates = [
                    binding
                    for binding in candidates
                    if binding.branch_binding_id == normalized_branch_binding_id
                    or binding.id == normalized_branch_binding_id
                ]
            elif selected_only:
                candidates = [binding for binding in candidates if binding.selected]
            candidates.sort(key=lambda binding: str(binding.updated_at or binding.created_at or ""), reverse=True)
            return candidates[0] if candidates else None

    def select_session_run_branch(
        self,
        *,
        session_run_id: str,
        branch_binding_id: str,
        peer_id: str = "",
    ) -> SessionRunBinding:
        binding = self.find_session_run_binding(
            session_run_id=session_run_id,
            branch_binding_id=branch_binding_id,
            selected_only=False,
        )
        if binding is None:
            raise KeyError("session_run_branch_binding_not_found")
        normalized_peer_id = str(peer_id or "").strip()
        if normalized_peer_id and binding.peer_id and binding.peer_id != normalized_peer_id:
            raise PermissionError("session_run_branch_peer_mismatch")
        selected = SessionRunBinding(**_session_run_binding_to_dict(binding))
        selected.selected = True
        self.upsert_session_run_binding(selected)
        refreshed = self.find_session_run_binding(
            session_run_id=session_run_id,
            branch_binding_id=branch_binding_id,
            selected_only=False,
        )
        return refreshed or selected

    def upsert_session_run_binding(self, binding: SessionRunBinding) -> None:
        if self._store is not None:
            writer = getattr(self._store, "upsert_session_run_binding", None)
            if callable(writer):
                writer(binding)
                return
            raise RuntimeError("AgentRun store does not support SessionRun bindings")
        normalized = SessionRunBinding(**_session_run_binding_to_dict(binding))
        now = datetime.now(timezone.utc).isoformat()
        if normalized.created_at is None:
            normalized.created_at = now
        normalized.updated_at = now
        with self._lock:
            if normalized.selected and normalized.status == SessionRunBindingStatus.ACTIVE:
                for existing in self._session_run_bindings.values():
                    if (
                        existing.session_run_id == normalized.session_run_id
                        and existing.id != normalized.id
                        and existing.status == SessionRunBindingStatus.ACTIVE
                    ):
                        existing.selected = False
                        existing.updated_at = now
            self._session_run_bindings[normalized.id] = normalized
            if normalized.agent_run_id in self._events:
                self._append_event_locked(
                    normalized.agent_run_id,
                    "session_run_binding_upserted",
                    {"binding": _session_run_binding_to_dict(normalized)},
                )

    def list_session_run_bindings(self, **filters: Any) -> list[SessionRunBinding]:
        if self._store is not None:
            lister = getattr(self._store, "list_session_run_bindings", None)
            if callable(lister):
                return lister(**filters)
            raise RuntimeError("AgentRun store does not support SessionRun bindings")
        with self._lock:
            bindings = list(self._session_run_bindings.values())
            for key, value in filters.items():
                if value is None:
                    continue
                expected = getattr(value, "value", value)
                filtered: list[SessionRunBinding] = []
                for binding in bindings:
                    actual = getattr(binding, key, "")
                    actual_value = getattr(actual, "value", actual)
                    if str(actual_value) == str(expected):
                        filtered.append(binding)
                bindings = filtered
            return bindings

    def _append_agent_thread_binding_status_event_locked(
        self,
        binding: AgentThreadBinding,
        event_type: str,
        *,
        reason: str = "",
    ) -> None:
        binding.updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "binding_id": binding.id,
            "reason": str(reason or ""),
            "binding": _agent_thread_binding_to_dict(binding),
        }
        for task_id in (binding.main_agent_run_id, binding.target_agent_run_id):
            if task_id in self._events:
                self._append_event_locked(task_id, event_type, payload)

    def _set_agent_thread_binding_status_locked(
        self,
        binding: AgentThreadBinding,
        *,
        status: AgentThreadBindingStatus,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        cancel_target: bool = False,
    ) -> AgentThreadBinding:
        binding.status = status
        if metadata:
            binding.metadata.update(dict(metadata))
        if reason:
            binding.metadata["status_reason"] = str(reason)
        self._agent_thread_bindings[binding.id] = binding
        event_type = (
            "agent_thread_binding_unavailable"
            if status == AgentThreadBindingStatus.UNAVAILABLE
            else "agent_thread_binding_closed"
        )
        self._append_agent_thread_binding_status_event_locked(
            binding,
            event_type,
            reason=reason,
        )
        if cancel_target and binding.target_agent_run_id in self._states:
            target = self._states[binding.target_agent_run_id].task
            self._cancel_task_locked(
                target,
                reason=f"agent_thread_binding_{status.value}:{reason or 'closed'}",
            )
        return binding

    def close_agent_thread_binding(
        self,
        binding_id: str,
        *,
        reason: str = "user_closed",
        cancel_target: bool = True,
    ) -> bool:
        normalized_id = str(binding_id or "").strip()
        if self._store is not None:
            setter = getattr(self._store, "set_agent_thread_binding_status", None)
            if not callable(setter):
                raise RuntimeError("AgentRun store does not support Agent thread bindings")
            binding = setter(
                normalized_id,
                status=AgentThreadBindingStatus.CLOSED,
                reason=reason,
            )
            if binding is None:
                return False
            if cancel_target:
                self.cancel_agent_run(
                    binding.target_agent_run_id,
                    reason=f"agent_thread_binding_closed:{reason}",
                )
            return True
        with self._lock:
            binding = self._agent_thread_bindings.get(normalized_id)
            if binding is None:
                return False
            self._set_agent_thread_binding_status_locked(
                binding,
                status=AgentThreadBindingStatus.CLOSED,
                reason=reason,
                cancel_target=cancel_target,
            )
            return True

    def mark_agent_thread_binding_unavailable(
        self,
        binding_id: str,
        *,
        reason: str = "agent_config_unavailable",
        metadata: dict[str, Any] | None = None,
        cancel_target: bool = True,
    ) -> bool:
        normalized_id = str(binding_id or "").strip()
        if self._store is not None:
            setter = getattr(self._store, "set_agent_thread_binding_status", None)
            if not callable(setter):
                raise RuntimeError("AgentRun store does not support Agent thread bindings")
            binding = setter(
                normalized_id,
                status=AgentThreadBindingStatus.UNAVAILABLE,
                reason=reason,
                metadata=metadata,
            )
            if binding is None:
                return False
            if cancel_target:
                self.cancel_agent_run(
                    binding.target_agent_run_id,
                    reason=f"agent_thread_binding_unavailable:{reason}",
                )
            return True
        with self._lock:
            binding = self._agent_thread_bindings.get(normalized_id)
            if binding is None:
                return False
            self._set_agent_thread_binding_status_locked(
                binding,
                status=AgentThreadBindingStatus.UNAVAILABLE,
                reason=reason,
                metadata=metadata,
                cancel_target=cancel_target,
            )
            return True

    def delete_agent_thread_bindings_for_owner_session(
        self,
        owner_session_run_id: str,
        *,
        reason: str = "owner_session_deleted",
    ) -> list[str]:
        session_run_id = str(owner_session_run_id or "").strip()
        if not session_run_id:
            return []
        if self._store is not None:
            deleter = getattr(
                self._store,
                "delete_agent_thread_bindings_for_owner_session",
                None,
            )
            if not callable(deleter):
                raise RuntimeError("AgentRun store does not support Agent thread bindings")
            deleted = deleter(session_run_id, reason=reason)
            for binding in deleted:
                if (
                    binding.cleanup_policy == "delete_with_owner_session"
                    and binding.status == AgentThreadBindingStatus.ACTIVE
                ):
                    self.cancel_agent_run(
                        binding.target_agent_run_id,
                        reason=f"owner_session_deleted:{reason}",
                    )
            return [binding.id for binding in deleted]
        with self._lock:
            deleted_ids: list[str] = []
            for binding in list(self._agent_thread_bindings.values()):
                if binding.owner_session_run_id != session_run_id:
                    continue
                if (
                    binding.cleanup_policy == "delete_with_owner_session"
                    and binding.status == AgentThreadBindingStatus.ACTIVE
                    and binding.target_agent_run_id in self._states
                ):
                    target = self._states[binding.target_agent_run_id].task
                    self._cancel_task_locked(
                        target,
                        reason=f"owner_session_deleted:{reason}",
                    )
                self._append_agent_thread_binding_status_event_locked(
                    binding,
                    "agent_thread_binding_deleted",
                    reason=reason,
                )
                deleted_ids.append(binding.id)
                self._agent_thread_bindings.pop(binding.id, None)
            return deleted_ids

    def invalidate_agent_thread_bindings(
        self,
        *,
        agent_id: str | None = None,
        owner_session_run_id: str | None = None,
        main_agent_run_id: str | None = None,
        reason: str = "agent_config_unavailable",
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        filters = {
            "agent_id": agent_id,
            "owner_session_run_id": owner_session_run_id,
            "main_agent_run_id": main_agent_run_id,
            "status": AgentThreadBindingStatus.ACTIVE.value,
        }
        bindings = self.list_agent_thread_bindings(**filters)
        unavailable: list[str] = []
        for binding in bindings:
            if self.mark_agent_thread_binding_unavailable(
                binding.id,
                reason=reason,
                metadata=metadata,
            ):
                unavailable.append(binding.id)
        return unavailable

    def _cleanup_bindings_for_terminal_task_locked(self, task: AgentRun) -> None:
        if not task.is_terminal:
            return
        for binding in list(self._agent_thread_bindings.values()):
            if binding.status != AgentThreadBindingStatus.ACTIVE:
                continue
            if (
                binding.main_agent_run_id == task.id
                and binding.binding_lifetime == AgentThreadBindingLifetime.RUN
            ):
                self._set_agent_thread_binding_status_locked(
                    binding,
                    status=AgentThreadBindingStatus.CLOSED,
                    reason=f"main_agent_run_terminal:{task.status.value}",
                    cancel_target=True,
                )
                continue
            if (
                binding.target_agent_run_id == task.id
                and task.status
                in {
                    AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED,
                    AgentRunStatus.BLOCKED,
                }
            ):
                self._set_agent_thread_binding_status_locked(
                    binding,
                    status=AgentThreadBindingStatus.UNAVAILABLE,
                    reason=f"target_agent_run_terminal:{task.status.value}",
                    cancel_target=False,
                )

    def _cleanup_bindings_for_terminal_task(self, task: AgentRun) -> None:
        if not task.is_terminal:
            return
        if self._store is None:
            with self._lock:
                self._cleanup_bindings_for_terminal_task_locked(task)
            return
        for binding in self.list_agent_thread_bindings(
            main_agent_run_id=task.id,
            status=AgentThreadBindingStatus.ACTIVE.value,
        ):
            if binding.binding_lifetime == AgentThreadBindingLifetime.RUN:
                self.close_agent_thread_binding(
                    binding.id,
                    reason=f"main_agent_run_terminal:{task.status.value}",
                    cancel_target=True,
                )
        if task.status not in {
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.BLOCKED,
        }:
            return
        for binding in self.list_agent_thread_bindings(
            target_agent_run_id=task.id,
            status=AgentThreadBindingStatus.ACTIVE.value,
        ):
            self.mark_agent_thread_binding_unavailable(
                binding.id,
                reason=f"target_agent_run_terminal:{task.status.value}",
                cancel_target=False,
            )

    def find_agent_call_grant(
        self,
        *,
        user_id: str,
        grant_scope: str,
        main_agent_id: str,
        target_agent_id: str,
        conversation_scope: str,
        capability_scope: dict[str, Any] | None = None,
        target_config_version: str = "",
    ) -> AgentCallGrant | None:
        if self._store is not None:
            finder = getattr(self._store, "find_agent_call_grant", None)
            if callable(finder):
                return finder(
                    user_id=user_id,
                    grant_scope=grant_scope,
                    main_agent_id=main_agent_id,
                    target_agent_id=target_agent_id,
                    conversation_scope=conversation_scope,
                    capability_scope=capability_scope or {},
                    target_config_version=target_config_version,
                )
            raise RuntimeError("AgentRun store does not support Agent call grants")
        key = _agent_call_grant_key(
            user_id=user_id,
            grant_scope=grant_scope,
            main_agent_id=main_agent_id,
            target_agent_id=target_agent_id,
            conversation_scope=conversation_scope,
            capability_scope=capability_scope or {},
            target_config_version=target_config_version,
        )
        with self._lock:
            grant = self._agent_call_grants.get(key)
        if not _agent_call_grant_is_active(grant):
            return None
        return grant

    def upsert_agent_call_grant(self, grant: AgentCallGrant) -> None:
        if self._store is not None:
            writer = getattr(self._store, "upsert_agent_call_grant", None)
            if callable(writer):
                writer(grant)
                return
            raise RuntimeError("AgentRun store does not support Agent call grants")
        key = _agent_call_grant_key(
            user_id=grant.user_id,
            grant_scope=grant.grant_scope,
            main_agent_id=grant.main_agent_id,
            target_agent_id=grant.target_agent_id,
            conversation_scope=grant.conversation_scope,
            capability_scope=grant.capability_scope,
            target_config_version=grant.target_config_version,
        )
        with self._lock:
            self._agent_call_grants[key] = grant

    def notify_task_available(self) -> None:
        """Wake workers waiting for queued AgentRuns or event changes."""

        with self._wakeup:
            self._wakeup.notify_all()

    def _resolve_request_against_snapshot(
        self,
        request: AgentRunRequest,
        *,
        parent: AgentRun | None = None,
    ) -> AgentRunRequest:
        if parent is not None:
            if request.runtime_profile_id is None:
                request.runtime_profile_id = parent.runtime_profile_id
            if request.executor is None:
                request.executor = parent.executor
            if request.execution_location is None:
                request.execution_location = parent.execution_location
            if request.workdir is None:
                request.workdir = parent.workdir
        snapshot = self.runtime_snapshot
        agents = _dict_from(snapshot.get("agents"))
        profiles = _dict_from(snapshot.get("runtime_profiles"))
        raw_agent = _dict_from(agents.get(request.agent_id))
        agent_config: AgentConfig | None = None
        if raw_agent:
            agent_config = AgentConfig.from_dict(request.agent_id, raw_agent)
            if agent_config.visibility != "user":
                flow = system_flow_for_source(request.source)
                if not agent_config.allows_system_flow(flow):
                    raise ValueError(
                        "agent is restricted to system flows: "
                        f"{request.agent_id} does not allow {flow}"
                    )

        agent_profile_id = str(raw_agent.get("runtime_profile") or "").strip()
        profile_id = str(request.runtime_profile_id or agent_profile_id).strip()
        if raw_agent and not profile_id:
            raise ValueError(
                f"agent {request.agent_id} requires a runtime_profile"
            )
        raw_profile = _dict_from(profiles.get(profile_id)) if profile_id else {}
        if profile_id and not raw_profile:
            raise ValueError(f"runtime profile not found: {profile_id}")

        request.runtime_profile_id = profile_id or None
        request.executor = (
            request.executor
            or _optional_executor(raw_profile.get("executor"))
            or ExecutorType.REULEAUXCODER
        )
        request.execution_location = (
            request.execution_location
            or _optional_location(raw_profile.get("execution_location"))
            or ExecutionLocation.REMOTE_SERVER
        )
        request.worker_kind = request.worker_kind or worker_kind_for_runtime(
            raw_profile,
            request.execution_location,
        )
        request.model_request_origin = (
            request.model_request_origin
            or model_request_origin_for_runtime(
                raw_profile,
                executor=request.executor,
                worker_kind=request.worker_kind,
            )
        )
        request.worktree_role = (
            request.worktree_role
            or optional_worktree_role(raw_profile.get("worktree_role"))
            or WorktreeRole.TARGET
        )
        request.publish_policy = (
            request.publish_policy
            or optional_publish_policy(raw_profile.get("publish_policy"))
            or PublishPolicy.NEVER
        )
        model_binding = _agent_model_binding(raw_agent)
        if model_binding:
            request.metadata.setdefault("model_binding", model_binding)
            if request.model is None:
                request.model = str(model_binding["model"])
        self._validate_runtime_policy(
            request,
            agent_config=agent_config,
        )
        if request.model is None and raw_profile.get("model") is not None:
            request.model = str(raw_profile["model"])
        if (
            parent is not None
            and request.executor_session_id is None
            and _can_resume_from_parent(request, parent)
        ):
            request.executor_session_id = parent.executor_session_id
        return request

    def _validate_runtime_policy(
        self,
        request: AgentRunRequest,
        *,
        agent_config: AgentConfig | None,
    ) -> None:
        validate_agent_run_runtime_policy(request, agent_config=agent_config)

    def _resolve_request_locked(self, request: AgentRunRequest) -> AgentRunRequest:
        return self._resolve_request_against_snapshot(request)

    def _runtime_profile_for_request(self, request: AgentRunRequest) -> dict[str, Any]:
        profiles = _dict_from(self.runtime_snapshot.get("runtime_profiles"))
        return _dict_from(profiles.get(request.runtime_profile_id or ""))

    def _sandbox_profile_for_runtime(
        self,
        runtime_profile: dict[str, Any],
    ) -> SandboxProfile:
        base = self._sandbox_profile or SandboxProfile(image="labrastro-host:test")
        sandbox = _dict_from(runtime_profile.get("sandbox"))
        return SandboxProfile(
            image=str(
                sandbox.get("image")
                or runtime_profile.get("worker_image")
                or base.image
            ),
            cpu_limit=str(sandbox.get("cpu_limit") or base.cpu_limit),
            memory_limit=str(sandbox.get("memory_limit") or base.memory_limit),
            network=str(sandbox.get("network") or base.network),
            workspace_volume_prefix=str(
                sandbox.get("workspace_volume_prefix")
                or base.workspace_volume_prefix
            ),
            idle_ttl_seconds=int(
                sandbox.get("idle_ttl_seconds") or base.idle_ttl_seconds
            ),
            env={
                **base.env,
                **{
                    str(k): str(v)
                    for k, v in _dict_from(sandbox.get("env")).items()
                },
            },
        )

    def _prepare_sandbox_session(
        self,
        request: AgentRunRequest,
        task_id: str,
    ) -> str | None:
        provider = self._sandbox_provider
        if provider is None:
            return None
        if request.sandbox_session_id:
            return None
        if request.metadata.get("skip_sandbox") is True:
            return None
        if request.worker_kind != WorkerKind.SANDBOX_WORKER:
            return None
        metadata = dict(request.metadata)
        runtime_profile = self._runtime_profile_for_request(request)
        profile = self._sandbox_profile_for_runtime(runtime_profile)
        workspace_ref = str(
            request.workspace_ref
            or metadata.get("workspace_ref")
            or metadata.get("workspace_root")
            or task_id
        ).strip()
        if not workspace_ref:
            workspace_ref = task_id
        try:
            sandbox = provider.ensure_sandbox(
                workspace_ref,
                profile,
                {
                    "agent_run_id": task_id,
                    "agent_id": request.agent_id,
                    "source": request.source.value,
                },
            )
            runtime_profile_for_session = dict(runtime_profile)
            runtime_profile_for_session["sandbox"] = profile.__dict__.copy()
            session = provider.start_session(
                sandbox.id,
                runtime_profile_for_session,
                task_id,
            )
            mount = provider.prepare_workspace(
                session.id,
                {
                    "source": workspace_ref,
                    "agent_run_id": task_id,
                    "source_workspace_root": metadata.get("workspace_root"),
                },
            )
            provider.exec_agent_run(
                session.id,
                {
                    "agent_run_id": task_id,
                    "agent_id": request.agent_id,
                    "runtime_profile_id": request.runtime_profile_id,
                    "source": request.source.value,
                },
            )
        except Exception as exc:
            metadata["sandbox_error"] = str(exc)
            request.metadata = metadata
            return f"sandbox provider failed to start session: {exc}"

        request.sandbox_id = sandbox.id
        request.sandbox_session_id = session.id
        request.workspace_ref = workspace_ref
        request.workdir = request.workdir or mount.path
        if metadata.get("workspace_root") is not None:
            metadata.setdefault("source_workspace_root", str(metadata["workspace_root"]))
        metadata["workspace_root"] = mount.path
        metadata["sandbox_id"] = sandbox.id
        metadata["sandbox_session_id"] = session.id
        metadata["workspace_ref"] = workspace_ref
        metadata["workspace_mount"] = mount.path
        metadata["sandbox_container_id"] = session.container_id
        request.metadata = metadata
        return None

    def _stop_sandbox_for_task(
        self,
        task: AgentRun,
        *,
        cancel: bool = False,
    ) -> None:
        provider = self._sandbox_provider
        if provider is None or not task.sandbox_session_id:
            return
        try:
            if cancel:
                provider.cancel(task.sandbox_session_id)
            else:
                provider.stop_session(task.sandbox_session_id)
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            task.metadata["sandbox_stop_error"] = str(exc)

    def claim_agent_run_activation(
        self,
        *,
        worker_id: str,
        worker_kind: WorkerKind | str | None = None,
        executors: list[ExecutorType | str] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
        wait_sec: float = 0.0,
    ) -> AgentRunActivationClaim | None:
        deadline = time.time() + max(0.0, float(wait_sec or 0.0))
        while True:
            claim = self._claim_task_once(
                worker_id=worker_id,
                worker_kind=worker_kind,
                executors=executors,
                peer_id=peer_id,
                peer_features=peer_features,
                workspace_root=workspace_root,
                lease_sec=lease_sec,
            )
            if claim is not None or wait_sec <= 0:
                return claim
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            with self._wakeup:
                self._wakeup.wait(timeout=min(remaining, 1.0))

    def _claim_task_once(
        self,
        *,
        worker_id: str,
        worker_kind: WorkerKind | str | None = None,
        executors: list[ExecutorType | str] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> AgentRunActivationClaim | None:
        if self._store is not None:
            return self._store.claim_agent_run_activation(
                worker_id=worker_id,
                worker_kind=worker_kind,
                executors=executors,
                peer_id=peer_id,
                peer_features=peer_features,
                workspace_root=workspace_root,
                lease_sec=lease_sec,
            )
        allowed = {_coerce_executor(executor) for executor in executors or []}
        features = (
            {str(feature) for feature in peer_features}
            if peer_features is not None
            else None
        )
        worker_kind_value = optional_worker_kind(worker_kind)
        with self._lock:
            self.recover_stale_agent_runs()
            running_tasks = self._running_tasks_locked()
            for state in self._states.values():
                task = state.task
                if task.status != AgentRunStatus.QUEUED:
                    continue
                if allowed and task.executor not in allowed:
                    continue
                if not self._worker_matches_task_locked(
                    task,
                    worker_kind=worker_kind_value,
                    features=features,
                    workspace_root=workspace_root,
                ):
                    continue
                if not runtime_slots_allow_agent_run_claim(
                    running_tasks,
                    task,
                    self.runtime_snapshot,
                    max_running_tasks=self.max_running_tasks,
                ):
                    continue
                task.status = AgentRunStatus.DISPATCHED
                activation = _activation_with_runtime_state(
                    self._activations.get(task.current_activation_id or "")
                    or _activation_from_task(task),
                    status=AgentRunActivationStatus.DISPATCHED,
                    request_id=_new_id("claim"),
                    worker_id=worker_id,
                )
                task.current_activation_id = activation.id
                metadata = self._executor_metadata(task)
                metadata.setdefault("activation_id", activation.id)
                metadata.setdefault("agent_run_id", task.id)
                claim = AgentRunActivationClaim(
                    request_id=activation.request_id or _new_id("claim"),
                    worker_id=worker_id,
                    task=task,
                    executor_request=ExecutorRunRequest(
                        task_id=task.id,
                        agent_id=task.agent_id,
                        executor=task.executor or ExecutorType.REULEAUXCODER,
                        prompt=activation.prompt,
                        execution_location=(
                            task.execution_location
                            or ExecutionLocation.REMOTE_SERVER
                        ),
                        runtime_profile_id=task.runtime_profile_id,
                        worker_kind=metadata.get("worker_kind"),
                        model_request_origin=metadata.get("model_request_origin"),
                        worktree_role=task.worktree_role,
                        publish_policy=task.publish_policy,
                        workdir=task.workdir,
                        branch=_worktree_branch_for_activation(activation) or None,
                        model=str(task.metadata.get("model"))
                        if task.metadata.get("model") is not None
                        else None,
                        executor_session_id=task.executor_session_id,
                        budget=_dict_from(metadata.get("budget")),
                        metadata=metadata,
                    ),
                    runtime_snapshot=dict(self.runtime_snapshot),
                    activation=activation,
                )
                activation.request_id = claim.request_id
                self._activations[activation.id] = activation
                self._claims[claim.request_id] = claim
                now = time.time()
                self._claim_leases[claim.request_id] = {
                    "task_id": task.id,
                    "activation_id": activation.id,
                    "worker_id": worker_id,
                    "peer_id": peer_id or "",
                    "last_heartbeat_at": now,
                    "lease_deadline": now + max(1, int(lease_sec or 15)),
                    "lease_sec": max(1, int(lease_sec or 15)),
                }
                self._append_event_locked(
                    task.id,
                    "claimed",
                    {
                        "worker_id": worker_id,
                        "peer_id": peer_id,
                        "worker_kind": worker_kind_value.value
                        if worker_kind_value is not None
                        else None,
                        "request_id": claim.request_id,
                        "activation_id": activation.id,
                        "activation": _activation_to_dict(activation),
                        "lease_sec": max(1, int(lease_sec or 15)),
                    },
                )
                return claim
            return None

    def heartbeat_agent_run_activation(
        self,
        *,
        request_id: str,
        task_id: str,
        activation_id: str,
        worker_id: str,
        peer_id: str | None = None,
        lease_sec: int | None = None,
        delivered_steer_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if self._store is not None:
            result = self._store.heartbeat_agent_run_activation(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
                lease_sec=lease_sec,
                delivered_steer_ids=delivered_steer_ids,
            )
            self.notify_task_available()
            return result
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return {
                    "ok": False,
                    "cancel_requested": True,
                    "reason": "agent_run_not_found",
                    "lease_sec": 0,
                }
            task = state.task
            lease = self._claim_leases.get(request_id)
            if lease is None:
                return {
                    "ok": False,
                    "cancel_requested": task_id in self._cancel_requests,
                    "reason": self._cancel_requests.get(task_id, "claim_not_found"),
                    "lease_sec": 0,
                }
            ok, reason = self._validate_activation_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            if not ok:
                return {
                    "ok": False,
                    "cancel_requested": True,
                    "reason": reason,
                    "lease_sec": 0,
                }
            effective_lease_sec = max(1, int(lease_sec or lease.get("lease_sec") or 15))
            now = time.time()
            lease["last_heartbeat_at"] = now
            lease["lease_deadline"] = now + effective_lease_sec
            lease["lease_sec"] = effective_lease_sec
            reason = self._cancel_requests.get(task_id, "")
            if task.status == AgentRunStatus.DISPATCHED:
                task.status = AgentRunStatus.RUNNING
            activation_id = str(
                lease.get("activation_id")
                or task.current_activation_id
                or ""
            )
            if activation_id:
                self._activations[activation_id] = _activation_with_runtime_state(
                    self._activations.get(activation_id)
                    or _activation_from_task(task, activation_id=activation_id),
                    status=AgentRunActivationStatus.RUNNING,
                    request_id=request_id,
                    worker_id=worker_id,
                )
            self._mark_activation_steers_delivered_locked(
                task_id,
                activation_id=activation_id,
                steer_ids=delivered_steer_ids or [],
                request_id=request_id,
                worker_id=worker_id,
            )
            activation_steers = (
                []
                if reason
                else self._claim_activation_steers_for_delivery_locked(
                    task_id,
                    activation_id=activation_id,
                    request_id=request_id,
                    worker_id=worker_id,
                )
            )
            return {
                "ok": True,
                "activation_id": activation_id,
                "cancel_requested": bool(reason),
                "reason": reason,
                "lease_sec": effective_lease_sec,
                "activation_steers": [
                    _steer_to_dict(steer) for steer in activation_steers
                ],
            }

    def validate_activation_claim_owner(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        activation_id: str | None = None,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        if self._store is not None:
            return self._store.validate_activation_claim_owner(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
        with self._lock:
            return self._validate_activation_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )

    def recover_stale_agent_runs(self, *, now: float | None = None) -> list[str]:
        if self._store is not None:
            recovered = self._store.recover_stale_agent_runs(now=now)
            if recovered:
                self.notify_task_available()
            return recovered
        current = time.time() if now is None else now
        recovered: list[str] = []
        with self._lock:
            for request_id, lease in list(self._claim_leases.items()):
                deadline = float(lease.get("lease_deadline") or 0)
                if deadline > current:
                    continue
                task_id = str(lease.get("task_id") or "")
                state = self._states.get(task_id)
                if state is None:
                    self._claim_leases.pop(request_id, None)
                    self._claims.pop(request_id, None)
                    continue
                task = state.task
                if task.status in {
                    AgentRunStatus.DISPATCHED,
                    AgentRunStatus.RUNNING,
                    AgentRunStatus.WAITING,
                }:
                    task.status = AgentRunStatus.QUEUED
                    activation_id = str(
                        lease.get("activation_id")
                        or task.current_activation_id
                        or ""
                    )
                    activation = _activation_with_runtime_state(
                        self._activations.get(activation_id)
                        or _activation_from_task(task, activation_id=activation_id),
                        status=AgentRunActivationStatus.QUEUED,
                    )
                    task.current_activation_id = activation.id
                    self._activations[activation.id] = activation
                    self._requeue_delivering_activation_steers_locked(
                        task_id,
                        activation_id=activation_id or activation.id,
                        request_id=request_id,
                    )
                    self._cancel_requests.pop(task_id, None)
                    recovered.append(task_id)
                    self._append_event_locked(
                        task_id,
                        "lease_expired",
                        {
                            "request_id": request_id,
                            "activation_id": activation_id or activation.id,
                            "worker_id": lease.get("worker_id"),
                            "peer_id": lease.get("peer_id"),
                            "activation": _activation_to_dict(activation),
                        },
                    )
                self._claim_leases.pop(request_id, None)
                self._claims.pop(request_id, None)
        return recovered

    def _session_run_binding_metadata_for_task_locked(
        self,
        task: AgentRun,
    ) -> dict[str, str]:
        session_run_id = str(task.owner_session_run_id or "").strip()
        if not session_run_id:
            return {}
        if self._store is not None:
            lister = getattr(self._store, "list_session_run_bindings", None)
            if not callable(lister):
                return {}
            bindings = lister(
                session_run_id=session_run_id,
                status=SessionRunBindingStatus.ACTIVE,
            )
        else:
            bindings = [
                binding
                for binding in self._session_run_bindings.values()
                if binding.session_run_id == session_run_id
                and binding.status == SessionRunBindingStatus.ACTIVE
            ]
        for binding in bindings:
            if binding.agent_run_id != task.id and binding.target_agent_run_id != task.id:
                continue
            branch_binding_id = str(binding.branch_binding_id or "").strip()
            agent_run_id = str(binding.agent_run_id or "").strip()
            if not branch_binding_id or not agent_run_id:
                return {}
            return {
                "session_run_id": str(binding.session_run_id or ""),
                "session_run_binding_id": str(binding.id or ""),
                "branch_binding_id": branch_binding_id,
                "binding_agent_run_id": agent_run_id,
                "parent_branch_binding_id": str(binding.parent_branch_binding_id or ""),
            }
        return {}

    def _executor_metadata(self, task: AgentRun) -> dict[str, Any]:
        metadata = dict(task.metadata)
        for key, value in self._session_run_binding_metadata_for_task_locked(task).items():
            if value:
                metadata.setdefault(key, value)
        executor = task.executor or ExecutorType.REULEAUXCODER
        if task.worktree_role is not None:
            metadata.setdefault("worktree_role", task.worktree_role.value)
        if task.publish_policy is not None:
            metadata.setdefault("publish_policy", task.publish_policy.value)
        worker_kind = str(metadata.get("worker_kind") or "").strip()
        model_request_origin = str(metadata.get("model_request_origin") or "").strip()
        worktree_role = str(metadata.get("worktree_role") or "").strip()
        publish_policy = str(metadata.get("publish_policy") or "").strip()
        if worker_kind:
            metadata.setdefault("worker_kind", worker_kind)
        if model_request_origin:
            metadata.setdefault("model_request_origin", model_request_origin)
        if worktree_role:
            metadata.setdefault("worktree_role", worktree_role)
        if publish_policy:
            metadata.setdefault("publish_policy", publish_policy)
        rendered = self._render_prompt_for_task(task, executor)
        if rendered is not None:
            metadata.setdefault("prompt_files", rendered.files)
            metadata.setdefault("prompt_metadata", rendered.metadata)
            if rendered.metadata.get("system_prompt"):
                metadata.setdefault("system_prompt", rendered.metadata["system_prompt"])
        return self._metadata_with_snapshot_capabilities(
            metadata,
            agent_id=task.agent_id,
            source=task.source,
            runtime_profile_id=task.runtime_profile_id,
        )

    def _metadata_with_snapshot_capabilities(
        self,
        metadata: dict[str, Any],
        *,
        agent_id: str,
        source: AgentRunSource,
        runtime_profile_id: str | None,
    ) -> dict[str, Any]:
        snapshot = self.runtime_snapshot
        raw_agent = _dict_from(_dict_from(snapshot.get("agents")).get(agent_id))
        resolved = _dict_from(raw_agent.get("resolved_capabilities"))
        effective = _dict_from(raw_agent.get("effective_capabilities"))
        overlay = _dict_from(resolved.get("capability_overlay"))
        if resolved:
            metadata.setdefault("resolved_capabilities", resolved)
        if overlay:
            metadata.setdefault("capability_overlay", overlay)
        if effective:
            metadata.setdefault("effective_capabilities", effective)
            metadata.setdefault(
                "execution_policies",
                effective.get("execution_policies", []),
            )
        metadata.setdefault(
            "permission_context",
            {
                "agent_id": agent_id,
                "source": source.value,
                "interactive": source == AgentRunSource.CHAT,
                "runtime_profile_id": runtime_profile_id
                or str(raw_agent.get("runtime_profile") or ""),
                "effective_capabilities": effective,
                "resolved_capabilities": resolved,
            },
        )
        return metadata

    def _session_metadata(
        self,
        task: AgentRun,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_metadata = self._executor_metadata(task)
        session_metadata.update(dict(metadata or {}))
        return session_metadata

    def _worker_matches_task_locked(
        self,
        task: AgentRun,
        *,
        worker_kind: WorkerKind | None,
        features: set[str] | None,
        workspace_root: str | None,
    ) -> bool:
        return worker_matches_agent_run(
            task,
            worker_kind=worker_kind,
            features=features,
            workspace_root=workspace_root,
        )

    def _render_prompt_for_task(
        self, task: AgentRun, executor: ExecutorType
    ) -> Any | None:
        snapshot = self.runtime_snapshot
        agents = _dict_from(snapshot.get("agents"))
        profiles = _dict_from(snapshot.get("runtime_profiles"))
        raw_agent = _dict_from(agents.get(task.agent_id))
        profile_id = task.runtime_profile_id or str(raw_agent.get("runtime_profile") or "")
        raw_profile = _dict_from(profiles.get(profile_id))
        prompt = _dict_from(raw_agent.get("prompt"))
        profile_mcp = _dict_from(raw_profile.get("mcp"))
        resolved = _dict_from(raw_agent.get("resolved_capabilities"))
        credential_refs = {
            **{
                str(key): str(val)
                for key, val in _dict_from(raw_profile.get("credential_refs")).items()
            },
            **{
                str(key): str(val)
                for key, val in _dict_from(raw_agent.get("credential_refs")).items()
            },
        }
        servers = []
        for source in (profile_mcp.get("servers"), resolved.get("mcp_servers")):
            servers.extend(_string_list_from(source))
        context = CanonicalAgentContext(
            agent_id=task.agent_id,
            agent_name=str(raw_agent.get("name") or ""),
            agent_md=(
                str(prompt["agent_md"]) if prompt.get("agent_md") is not None else None
            ),
            system_append=str(prompt.get("system_append") or ""),
            dispatch=_dict_from(raw_agent.get("dispatch")),
            capability_refs=_string_list_from(raw_agent.get("capability_refs")),
            resolved_capabilities=resolved,
            mcp_servers=servers,
            credential_refs=credential_refs,
        )
        return ExecutorPromptRenderer().render(executor.value, context)

    def pin_session(self, task_id: str, session: TaskSessionRef) -> None:
        if self._store is not None:
            self._store.pin_session(task_id, session)
            self.notify_task_available()
            return
        with self._lock:
            task = self._task_locked(task_id)
            task.status = AgentRunStatus.RUNNING
            if session.executor_session_id is not None:
                task.executor_session_id = session.executor_session_id
            if session.workdir is not None:
                task.workdir = session.workdir
            pinned = TaskSessionRef(
                agent_id=session.agent_id,
                executor=session.executor,
                execution_location=session.execution_location,
                task_id=session.task_id,
                workdir=task.workdir,
                branch=session.branch,
                executor_session_id=task.executor_session_id,
                metadata=self._session_metadata(task, session.metadata),
            )
            self._sessions[task_id] = pinned
            self._append_event_locked(
                task_id,
                "session_pinned",
                {
                    "executor_session_id": task.executor_session_id,
                    "workdir": task.workdir,
                    "branch": session.branch,
                },
            )

    def pin_claimed_activation_session(
        self,
        *,
        request_id: str,
        task_id: str,
        activation_id: str,
        worker_id: str,
        peer_id: str | None = None,
        workdir: str | None = None,
        branch: str | None = None,
        executor_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if self._store is not None:
            result = self._store.pin_claimed_activation_session(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
                workdir=workdir,
                branch=branch,
                executor_session_id=executor_session_id,
                metadata=metadata,
            )
            self.notify_task_available()
            return result
        with self._lock:
            ok, reason = self._validate_activation_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            if not ok:
                return False, reason
            task = self._task_locked(task_id)
            session = TaskSessionRef(
                agent_id=task.agent_id,
                executor=task.executor or ExecutorType.REULEAUXCODER,
                execution_location=(
                    task.execution_location or ExecutionLocation.REMOTE_SERVER
                ),
                task_id=task_id,
                workdir=workdir if workdir else None,
                branch=branch if branch else None,
                executor_session_id=(
                    executor_session_id if executor_session_id else None
                ),
                metadata=self._session_metadata(task, metadata),
            )
            self.pin_session(task_id, session)
            activation_id = str(
                self._claim_leases.get(request_id, {}).get("activation_id")
                or task.current_activation_id
                or ""
            )
            if activation_id:
                self._activations[activation_id] = _activation_with_runtime_state(
                    self._activations.get(activation_id)
                    or _activation_from_task(task, activation_id=activation_id),
                    status=AgentRunActivationStatus.RUNNING,
                    request_id=request_id,
                    worker_id=worker_id,
                )
            if metadata:
                self._append_event_locked(
                    task_id,
                    "session_metadata",
                    {
                        "request_id": request_id,
                        "activation_id": activation_id,
                        "worker_id": worker_id,
                        **metadata,
                    },
                )
            return True, ""

    def append_executor_event(
        self,
        task_id: str,
        event: ExecutorEvent,
        *,
        request_id: str | None = None,
        activation_id: str | None = None,
        worker_id: str | None = None,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        if self._store is not None:
            result = self._store.append_executor_event(
                task_id,
                event,
                request_id=request_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            self.notify_task_available()
            return result
        with self._lock:
            if request_id or worker_id or peer_id:
                ok, reason = self._validate_activation_claim_owner_locked(
                    request_id=request_id or "",
                    task_id=task_id,
                    activation_id=activation_id,
                    worker_id=worker_id or "",
                    peer_id=peer_id,
                )
                if not ok:
                    return False, reason
            task = self._task_locked(task_id)
            activation_id = ""
            if request_id:
                activation_id = str(
                    self._claim_leases.get(request_id, {}).get("activation_id") or ""
                )
            activation_id = activation_id or str(
                task.current_activation_id or ""
            )
            payload = event.to_dict()
            if activation_id:
                payload.setdefault("activation_id", activation_id)
                payload.setdefault(
                    "activation",
                    _activation_to_dict(
                        _activation_with_runtime_state(
                            self._activations.get(activation_id)
                            or _activation_from_task(task, activation_id=activation_id),
                            status=(
                                AgentRunActivationStatus.WAITING
                                if event.type.value == "status"
                                and str(event.data.get("status") or "")
                                == "waiting_approval"
                                else AgentRunActivationStatus.RUNNING
                            ),
                            request_id=request_id,
                            worker_id=worker_id,
                        )
                    ),
                )
            self._append_event_locked(task_id, event.type.value, payload)
            for event_type, payload in worktree_lifecycle_events(task, event):
                self._append_event_locked(task_id, event_type, payload)
            expansion = expand_environment_executor_event(task.metadata, event)
            for event_type, payload in expansion.events:
                self._append_event_locked(task_id, event_type, payload)
            if expansion.policy_error:
                task.metadata["environment_policy_violation"] = expansion.policy_error
                task.status = AgentRunStatus.BLOCKED
                task.failure_reason = expansion.policy_error
                task.cancel_reason = None
                self._append_event_locked(
                    task_id,
                    "blocked",
                    {"error": expansion.policy_error},
                )
            if event.type.value == "status":
                status = str(event.data.get("status", ""))
                if status == "waiting_approval":
                    if should_block_waiting_approval(task.source):
                        task.status = AgentRunStatus.BLOCKED
                        task.failure_reason = str(
                            event.data.get("reason")
                            or event.data.get("message")
                            or "approval_required"
                        )
                        task.cancel_reason = None
                        self._append_event_locked(
                            task_id,
                            "permission.blocked_review",
                            blocked_review_event_payload(event.data),
                        )
                    else:
                        task.status = AgentRunStatus.WAITING
                        task.waiting_reason = AgentRunWaitingReason.USER_APPROVAL
                        task.resume_policy = AgentRunResumePolicy.USER_ACTION
                elif status == "running":
                    task.status = AgentRunStatus.RUNNING
                    task.waiting_reason = None
                    task.resume_policy = None
                elif status == "blocked":
                    task.status = AgentRunStatus.BLOCKED
                    task.waiting_reason = None
                    task.resume_policy = None
                    task.failure_reason = str(
                        event.data.get("reason")
                        or event.data.get("message")
                        or "blocked"
                    )
                    task.cancel_reason = None
            return True, ""

    def append_agent_run_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> AgentRunEvent | None:
        """Append a named AgentRun event that is not an executor event."""
        event_type = str(event_type or "").strip()
        if not event_type:
            raise ValueError("agent_run_event_type_required")
        public_payload = dict(payload) if isinstance(payload, dict) else {}
        if self._store is not None:
            appender = getattr(self._store, "append_agent_run_event", None)
            if not callable(appender):
                raise RuntimeError("agent_run_event_append_unavailable")
            result = appender(task_id, event_type, public_payload)
            self.notify_task_available()
            return result
        with self._lock:
            self._task_locked(task_id)
            return self._append_event_locked(task_id, event_type, public_payload)

    def complete_claimed_agent_run_activation(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        activation_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str, AgentRun | None]:
        if self._store is not None:
            result_value = self._store.complete_claimed_agent_run_activation(
                task_id,
                result,
                request_id=request_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
                artifacts=artifacts,
            )
            if result_value[2] is not None and result_value[2].is_terminal:
                self._stop_sandbox_for_task(result_value[2])
                self._cleanup_bindings_for_terminal_task(result_value[2])
            self.notify_task_available()
            return result_value
        with self._lock:
            ok, reason = self._validate_activation_claim_owner_locked(
                request_id=request_id,
                task_id=task_id,
                activation_id=activation_id,
                worker_id=worker_id,
                peer_id=peer_id,
            )
            if not ok:
                return False, reason, None
            lease = self._claim_leases.get(request_id, {})
            activation_id = str(
                lease.get("activation_id")
                or self._task_locked(task_id).current_activation_id
                or ""
            )
            if activation_id:
                self._task_locked(task_id).current_activation_id = activation_id
            return True, "", self.complete_agent_run_activation(
                task_id,
                result,
                activation_id=activation_id,
                artifacts=artifacts,
            )

    def complete_agent_run_activation(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        activation_id: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentRun:
        completion_activation_id = str(activation_id or "").strip()
        if self._store is not None:
            task = self._store.complete_agent_run_activation(
                task_id,
                result,
                activation_id=completion_activation_id,
                artifacts=artifacts,
            )
            if task.is_terminal:
                self._stop_sandbox_for_task(task)
                self._cleanup_bindings_for_terminal_task(task)
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            self._validate_completion_activation_locked(
                task,
                completion_activation_id,
            )
            policy_error = str(
                task.metadata.get("environment_policy_violation") or ""
            ).strip()
            requested_cancel_reason = str(
                self._cancel_requests.get(task_id) or ""
            ).strip()
            if result.succeeded and not policy_error:
                required_feedback_waiting_reason = _waiting_reason_for_required_feedback(
                    self._feedbacks.get(task_id, [])
                )
                if (
                    task.status == AgentRunStatus.WAITING
                    or required_feedback_waiting_reason is not None
                ):
                    task.status = AgentRunStatus.WAITING
                    task.waiting_reason = (
                        task.waiting_reason
                        or required_feedback_waiting_reason
                        or AgentRunWaitingReason.SERVER_PROCESSING
                    )
                    task.resume_policy = (
                        task.resume_policy
                        or (
                            AgentRunResumePolicy.EXTERNAL_EVENT
                            if task.waiting_reason
                            in {
                                AgentRunWaitingReason.AGENT_CALL,
                                AgentRunWaitingReason.SERVER_PROCESSING,
                            }
                            else AgentRunResumePolicy.USER_ACTION
                        )
                    )
                    task.terminal_result = {}
                else:
                    self._states[task_id].complete_task_lifecycle(output=result.output)
                task.failure_reason = None
                task.cancel_reason = None
            elif policy_error:
                task.status = AgentRunStatus.BLOCKED
                _set_terminal_result(task, output=policy_error)
                task.failure_reason = policy_error
                task.cancel_reason = None
            elif result.status == "cancelled":
                cancel_reason = (
                    result.output
                    or requested_cancel_reason
                    or result.error
                    or "cancelled"
                )
                task.status = AgentRunStatus.CANCELLED
                _set_terminal_result(task, output=result.output or cancel_reason)
                task.failure_reason = "cancelled"
                task.cancel_reason = cancel_reason
            elif result.status == "blocked":
                task.status = AgentRunStatus.BLOCKED
                _set_terminal_result(task, output=result.output or result.error)
                task.failure_reason = result.error or result.output or "blocked"
                task.cancel_reason = None
            else:
                task.status = AgentRunStatus.FAILED
                _set_terminal_result(task, output=result.output)
                task.failure_reason = result.error or "agent_error"
                task.cancel_reason = None
            if result.executor_session_id:
                task.executor_session_id = result.executor_session_id
                self._sessions[task_id] = TaskSessionRef(
                    agent_id=task.agent_id,
                    executor=task.executor or ExecutorType.REULEAUXCODER,
                    execution_location=(
                        task.execution_location or ExecutionLocation.REMOTE_SERVER
                    ),
                    task_id=task_id,
                    workdir=task.workdir,
                    branch=self._sessions.get(task_id).branch
                    if task_id in self._sessions
                    else _worktree_branch_for_activation(
                        self._activations.get(task.current_activation_id or "")
                    )
                    or None,
                    executor_session_id=task.executor_session_id,
                    metadata=self._session_metadata(task),
                )
            for event in result.events:
                self._append_event_locked(task_id, event.type.value, event.to_dict())
                expansion = expand_environment_executor_event(task.metadata, event)
                for event_type, payload in expansion.events:
                    self._append_event_locked(task_id, event_type, payload)
                if expansion.policy_error and not policy_error:
                    policy_error = expansion.policy_error
                    task.metadata["environment_policy_violation"] = policy_error
                    task.status = AgentRunStatus.BLOCKED
                    _set_terminal_result(task, output=policy_error)
                    task.failure_reason = policy_error
                    task.cancel_reason = None
                    self._append_event_locked(
                        task_id,
                        "blocked",
                        {"error": policy_error},
                    )
            for artifact in executor_result_artifacts(result, artifacts):
                self.attach_artifact(task_id, **artifact)
            summary = environment_summary_event(
                task.metadata,
                task.status.value,
                output=_terminal_output(task) or result.output,
                error=policy_error or result.error or "",
            )
            if summary is not None:
                self._append_event_locked(task_id, summary[0], summary[1])
            claim_request_id = ""
            claim_worker_id: str | None = None
            for request_id, claim in self._claims.items():
                if claim.task.id == task_id:
                    claim_request_id = request_id
                    claim_worker_id = claim.worker_id
                    break
            activation = _activation_with_runtime_state(
                self._activations[completion_activation_id],
                status=_activation_status_for_result(result),
                request_id=claim_request_id or None,
                worker_id=claim_worker_id,
                output=_terminal_output(task) or result.output,
                result_payload=result.to_dict(),
            )
            self._activations[activation.id] = activation
            self._append_event_locked(
                task_id,
                "activation_completed",
                {
                    "activation_id": activation.id,
                    "activation": _activation_to_dict(activation),
                    "result": result.to_dict(),
                },
            )
            self._append_event_locked(
                task_id,
                task.status.value,
                {"result": result.to_dict(), "agent_run": _agent_run_to_dict(task)},
            )
            self._resume_from_pending_agent_call_feedback_locked(task_id)
            task = self._task_locked(task_id)
            if task.is_terminal:
                self._append_parent_terminal_event_locked(task)
                self._cleanup_bindings_for_terminal_task_locked(task)
            self._clear_task_claims_locked(task_id)
            self._cancel_requests.pop(task_id, None)
            if task.is_terminal:
                self._stop_sandbox_for_task(task)
            return task

    def retry_agent_run(
        self,
        task_id: str,
        *,
        resume_session: bool = False,
    ) -> AgentRun:
        return self.continue_agent_run(
            task_id,
            input_kind=AgentRunActivationInputKind.ADMIN_RESUME,
            input_payload={"resume_session": bool(resume_session)},
            resume_session=resume_session,
        )

    def append_agent_run_feedback(
        self,
        task_id: str,
        *,
        source: AgentRunFeedbackSource | str,
        kind: AgentRunFeedbackKind | str,
        payload: dict[str, Any],
        visibility: AgentRunFeedbackVisibility | str = AgentRunFeedbackVisibility.INTERNAL,
        requires_activation: bool = False,
        metadata: dict[str, Any] | None = None,
        feedback_id: str | None = None,
    ) -> AgentRunFeedback:
        if self._store is not None:
            return self._store.append_agent_run_feedback(
                task_id,
                source=source,
                kind=kind,
                payload=payload,
                visibility=visibility,
                requires_activation=requires_activation,
                metadata=metadata,
                feedback_id=feedback_id,
            )
        feedback = AgentRunFeedback(
            id=feedback_id or _new_id("feedback"),
            agent_run_id=task_id,
            source=source,
            kind=kind,
            payload=dict(payload),
            created_at=datetime.now(timezone.utc).isoformat(),
            visibility=visibility,
            requires_activation=requires_activation,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._task_locked(task_id)
            self._feedbacks.setdefault(task_id, []).append(feedback)
            self._append_event_locked(
                task_id,
                "agent_run_feedback_added",
                {
                    "feedback_id": feedback.id,
                    "feedback": _feedback_to_dict(feedback),
                },
            )
        return feedback

    def _mark_activation_steers_delivered_locked(
        self,
        task_id: str,
        *,
        activation_id: str,
        steer_ids: list[str],
        request_id: str,
        worker_id: str,
    ) -> None:
        delivered_ids = {str(item) for item in steer_ids if str(item).strip()}
        if not delivered_ids:
            return
        delivered_at = datetime.now(timezone.utc).isoformat()
        for steer in self._steers.get(task_id, []):
            if steer.id not in delivered_ids or steer.activation_id != activation_id:
                continue
            if steer.status not in {
                ActivationSteerStatus.QUEUED,
                ActivationSteerStatus.DELIVERING,
            }:
                continue
            steer.status = ActivationSteerStatus.DELIVERED
            steer.delivered_at = delivered_at
            steer.metadata.update(
                {
                    "delivered_by_request_id": request_id,
                    "delivered_by_worker_id": worker_id,
                }
            )
            self._append_event_locked(
                task_id,
                "activation_steer_delivered",
                {
                    "activation_id": activation_id,
                    "steer_id": steer.id,
                    "steer": _steer_to_dict(steer),
                },
            )

    def _claim_activation_steers_for_delivery_locked(
        self,
        task_id: str,
        *,
        activation_id: str,
        request_id: str,
        worker_id: str,
    ) -> list[ActivationSteer]:
        claimed: list[ActivationSteer] = []
        for steer in self._steers.get(task_id, []):
            if steer.activation_id != activation_id:
                continue
            if steer.status != ActivationSteerStatus.QUEUED:
                continue
            steer.status = ActivationSteerStatus.DELIVERING
            steer.metadata.update(
                {
                    "delivering_request_id": request_id,
                    "delivering_worker_id": worker_id,
                }
            )
            claimed.append(steer)
            self._append_event_locked(
                task_id,
                "activation_steer_delivering",
                {
                    "activation_id": activation_id,
                    "steer_id": steer.id,
                    "steer": _steer_to_dict(steer),
                },
            )
        return claimed

    def _requeue_delivering_activation_steers_locked(
        self,
        task_id: str,
        *,
        activation_id: str,
        request_id: str,
    ) -> None:
        if not activation_id or not request_id:
            return
        for steer in self._steers.get(task_id, []):
            if steer.activation_id != activation_id:
                continue
            if steer.status != ActivationSteerStatus.DELIVERING:
                continue
            if str(steer.metadata.get("delivering_request_id") or "") != request_id:
                continue
            steer.status = ActivationSteerStatus.QUEUED
            steer.metadata.pop("delivering_request_id", None)
            steer.metadata.pop("delivering_worker_id", None)

    def append_activation_steer(
        self,
        task_id: str,
        *,
        source: ActivationSteerSource | str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        steer_id: str | None = None,
    ) -> ActivationSteer:
        source_value = ActivationSteerSource(getattr(source, "value", source))
        resolved_steer_id = steer_id or _new_id("steer")
        metadata_value = _normalize_activation_steer_metadata(
            metadata,
            source=source_value,
            fallback_key=resolved_steer_id,
        )
        if self._store is not None:
            return self._store.append_activation_steer(
                task_id,
                source=source_value,
                payload=payload,
                metadata=metadata_value,
                steer_id=steer_id,
            )
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            task = self._task_locked(task_id)
            if task.status not in _ACTIVE_STEER_AGENT_RUN_STATUSES:
                raise ValueError("only active AgentRun activations can be steered")
            if task.metadata.get("activation_steer_supported") is False:
                raise ValueError("activation steer is not supported by this executor")
            activation_id = str(
                task.current_activation_id or ""
            )
            if not activation_id or activation_id not in self._activations:
                raise ValueError("active AgentRun activation is required")
            claim = self._active_claim_for_task_locked(task_id)
            if claim is None or str(claim.get("activation_id") or "") != activation_id:
                raise ValueError("active worker claim is required")
            _validate_activation_steer_payload(payload)
            sender, idempotency_key = _activation_steer_idempotency_scope(
                metadata_value
            )
            for existing in self._steers.get(task_id, []):
                if existing.activation_id != activation_id:
                    continue
                existing_sender, existing_key = _activation_steer_idempotency_scope(
                    existing.metadata
                )
                if existing_sender != sender or existing_key != idempotency_key:
                    continue
                if _canonical_json(existing.payload) != _canonical_json(payload):
                    raise ValueError("activation_steer_idempotency_conflict")
                return existing
            steer = ActivationSteer(
                id=resolved_steer_id,
                activation_id=activation_id,
                source=source_value,
                payload=dict(payload),
                created_at=created_at,
                status=ActivationSteerStatus.QUEUED,
                metadata=metadata_value,
            )
            self._steers.setdefault(task_id, []).append(steer)
            self._append_event_locked(
                task_id,
                "activation_steer_queued",
                {
                    "activation_id": activation_id,
                    "steer_id": steer.id,
                    "steer": _steer_to_dict(steer),
                },
            )
            return steer

    def continue_agent_run(
        self,
        task_id: str,
        *,
        input_kind: AgentRunActivationInputKind | str,
        input_payload: dict[str, Any],
        resume_session: bool = False,
        feedback_id: str | None = None,
        prompt: str | None = None,
    ) -> AgentRun:
        if self._store is not None:
            task = self._store.continue_agent_run(
                task_id,
                input_kind=input_kind,
                input_payload=input_payload,
                resume_session=resume_session,
                feedback_id=feedback_id,
                prompt=prompt,
            )
            self.notify_task_available()
            return task
        input_kind_value = AgentRunActivationInputKind(
            getattr(input_kind, "value", input_kind)
        )
        with self._lock:
            task = self._task_locked(task_id)
            if not task.is_terminal and task.status != AgentRunStatus.WAITING:
                raise ValueError("only terminal or waiting AgentRuns can be continued")
            previous_activation = self._activations.get(task.current_activation_id or "")
            previous_seq = (
                previous_activation.seq
                if previous_activation is not None
                else _activation_seq_from_id(task.current_activation_id)
                or 1
            )
            previous_activation_id = str(
                previous_activation.id
                if previous_activation is not None
                else task.current_activation_id or _activation_id_for_task(task.id, previous_seq)
            )
            activation_payload = dict(input_payload)
            activation_payload.setdefault("resume_session", bool(resume_session))
            activation_payload.setdefault(
                "retry_of_activation_id",
                previous_activation_id,
            )
            activation_prompt = (
                str(previous_activation.prompt if previous_activation is not None else "")
                if prompt is None
                else str(prompt)
            )
            _validate_activation_input_payload(activation_payload)
            next_seq = previous_seq + 1
            activation = AgentRunActivation(
                id=_activation_id_for_task(task.id, next_seq),
                agent_run_id=task.id,
                seq=next_seq,
                input_kind=input_kind_value,
                input_payload=activation_payload,
                prompt=activation_prompt,
                status=AgentRunActivationStatus.QUEUED,
                metadata={
                    "executor_session_id": task.executor_session_id,
                    "runtime_profile_id": task.runtime_profile_id,
                },
            )
            task.current_activation_id = activation.id
            task.status = AgentRunStatus.QUEUED
            task.waiting_reason = None
            task.resume_policy = None
            task.terminal_result = {}
            task.failure_reason = None
            task.cancel_reason = None
            if not resume_session:
                task.executor_session_id = None
            self._activations[activation.id] = activation
            if feedback_id:
                for feedback in self._feedbacks.get(task.id, []):
                    if feedback.id == feedback_id:
                        feedback.consumed_by_activation_id = activation.id
                        self._append_event_locked(
                            task.id,
                            "agent_run_feedback_consumed",
                            {
                                "feedback_id": feedback.id,
                                "activation_id": activation.id,
                                "feedback": _feedback_to_dict(feedback),
                            },
                        )
                        break
            self._append_event_locked(
                task.id,
                "activation_queued",
                {
                    "activation_id": activation.id,
                    "activation": _activation_to_dict(activation),
                    "retry_of_activation_id": previous_activation_id,
                    "feedback_id": feedback_id or "",
                },
            )
            self.notify_task_available()
            return task

    def fail_agent_run(self, task_id: str, *, error: str) -> AgentRun:
        if self._store is not None:
            task = self._store.fail_agent_run(task_id, error=error)
            self._stop_sandbox_for_task(task)
            self._cleanup_bindings_for_terminal_task(task)
            self.notify_task_available()
            return task
        with self._lock:
            task = self._task_locked(task_id)
            task.status = AgentRunStatus.FAILED
            _set_terminal_result(task, output=error)
            task.failure_reason = error
            task.cancel_reason = None
            self._append_event_locked(task_id, "failed", {"error": error})
            self._append_parent_terminal_event_locked(task)
            self._cleanup_bindings_for_terminal_task_locked(task)
            self._clear_task_claims_locked(task_id)
            self._cancel_requests.pop(task_id, None)
            self._stop_sandbox_for_task(task)
            return task

    def _store_branch_relations_for_cleanup(
        self,
        owner_agent_run_id: str,
        descendants: list[AgentRun],
    ) -> list[AgentRunRelation]:
        if self._store is None:
            return []
        relations: list[AgentRunRelation] = []
        seen_relation_ids: set[str] = set()
        for parent_id in [owner_agent_run_id, *[task.id for task in descendants]]:
            try:
                detail = self._store.load_agent_run_detail(parent_id, event_limit=1)
            except KeyError:
                continue
            for raw_relation in detail.get("relations", []):
                if not isinstance(raw_relation, dict):
                    continue
                if str(raw_relation.get("owner_agent_run_id") or "") != parent_id:
                    continue
                if str(raw_relation.get("relation_type") or "") != (
                    AgentRunRelationType.BRANCH.value
                ):
                    continue
                if str(raw_relation.get("status") or "") != "active":
                    continue
                relation_id = str(raw_relation.get("id") or "")
                if relation_id in seen_relation_ids:
                    continue
                relations.append(_relation_from_dict(raw_relation))
                seen_relation_ids.add(relation_id)
        return relations

    def _cleanup_branch_relation_worktree(
        self,
        relation: AgentRunRelation,
    ) -> dict[str, Any]:
        if relation.relation_type != AgentRunRelationType.BRANCH:
            return {"ok": True, "skipped": True}
        payload = dict(relation.payload)
        if str(payload.get("cleanup_policy") or "") != "delete_with_owner_session":
            return {"ok": True, "skipped": True}
        runtime_root = str(payload.get("runtime_root") or "").strip()
        source_workspace_root = str(payload.get("source_workspace_root") or "").strip()
        branch_name = str(payload.get("branch_name") or "").strip()
        branch_worktree_ref = str(payload.get("branch_worktree_ref") or "").strip()
        if (
            not runtime_root
            or not source_workspace_root
            or not branch_name
            or not branch_worktree_ref
        ):
            return {
                "ok": False,
                "error": "branch_relation_cleanup_payload_incomplete",
            }
        result = WorktreeManager(runtime_root).cleanup_branch_worktree(
            source_repo=source_workspace_root,
            branch_name=branch_name,
            worktree_path=branch_worktree_ref,
            delete_branch=True,
        )
        return result.to_dict()

    def _cleanup_child_branch_worktrees_locked(self, owner_agent_run_id: str) -> None:
        pending = [owner_agent_run_id]
        seen: set[str] = set()
        while pending:
            current_parent_id = pending.pop()
            if current_parent_id in seen:
                continue
            seen.add(current_parent_id)
            child_relations = [
                relation
                for relation in self._relations.values()
                if relation.owner_agent_run_id == current_parent_id
                and relation.related_agent_run_id not in seen
                and relation.status.value == "active"
            ]
            for relation in child_relations:
                if relation.relation_type == AgentRunRelationType.BRANCH:
                    result = self._cleanup_branch_relation_worktree(relation)
                    event_type = (
                        "agent_run_branch_worktree_cleaned"
                        if result.get("ok") is True
                        else "agent_run_branch_worktree_cleanup_failed"
                    )
                    for event_task_id in {
                        relation.owner_agent_run_id,
                        relation.related_agent_run_id,
                    }:
                        if event_task_id in self._events:
                            self._append_event_locked(
                                event_task_id,
                                event_type,
                                {
                                    "relation_id": relation.id,
                                    "cleanup": result,
                                },
                            )
                pending.append(relation.related_agent_run_id)

    def cancel_agent_run(self, task_id: str, *, reason: str = "user_cancelled") -> bool:
        if self._store is not None:
            task_before = self._store.get_agent_run(task_id)
            descendants_before = self._store_descendant_agent_runs(task_id)
            branch_relations = self._store_branch_relations_for_cleanup(
                task_id,
                descendants_before,
            )
            ok = self._store.cancel_agent_run(task_id, reason=reason)
            if ok:
                task_after = self._store.get_agent_run(task_id)
                stopped: set[str] = set()
                for task in [task_before, *descendants_before]:
                    if task.sandbox_session_id and task.sandbox_session_id not in stopped:
                        self._stop_sandbox_for_task(task, cancel=True)
                        stopped.add(task.sandbox_session_id)
                for relation in branch_relations:
                    self._cleanup_branch_relation_worktree(relation)
                if task_after.is_terminal:
                    self._cleanup_bindings_for_terminal_task(task_after)
            self.notify_task_available()
            return ok
        with self._lock:
            task = self._task_locked(task_id)
            if task.is_terminal:
                return False
            changed = self._cancel_task_locked(task, reason=reason)
            if changed:
                self._cancel_child_agent_runs_locked(task_id, reason=reason)
                self._cleanup_child_branch_worktrees_locked(task_id)
            return changed

    def _cancel_task_locked(
        self,
        task: AgentRun,
        *,
        reason: str,
    ) -> bool:
        if task.is_terminal:
            return False
        if task.sandbox_session_id:
            self._stop_sandbox_for_task(task, cancel=True)
            task.status = AgentRunStatus.CANCELLED
            _set_terminal_result(task, output=reason)
            task.failure_reason = "cancelled"
            task.cancel_reason = reason
            self._append_event_locked(task.id, "cancelled", {"reason": reason})
            self._append_parent_terminal_event_locked(task)
            self._cleanup_bindings_for_terminal_task_locked(task)
            self._clear_task_claims_locked(task.id)
            self._cancel_requests.pop(task.id, None)
            return True
        if task.status in {
            AgentRunStatus.DISPATCHED,
            AgentRunStatus.RUNNING,
            AgentRunStatus.WAITING,
        }:
            self._cancel_requests[task.id] = reason
            claim = self._active_claim_for_task_locked(task.id)
            self._append_event_locked(
                task.id,
                "cancel_requested",
                {
                    "reason": reason,
                    "activation_id": str(claim.get("activation_id") or "") if claim else "",
                    "worker_id": str(claim.get("worker_id") or "") if claim else "",
                },
            )
            return True
        task.status = AgentRunStatus.CANCELLED
        _set_terminal_result(task, output=reason)
        task.failure_reason = "cancelled"
        task.cancel_reason = reason
        self._append_event_locked(task.id, "cancelled", {"reason": reason})
        self._append_parent_terminal_event_locked(task)
        self._cleanup_bindings_for_terminal_task_locked(task)
        self._clear_task_claims_locked(task.id)
        self._cancel_requests.pop(task.id, None)
        return True

    def _cancel_child_agent_runs_locked(
        self,
        owner_agent_run_id: str,
        *,
        reason: str,
    ) -> None:
        pending = [owner_agent_run_id]
        seen: set[str] = set()
        while pending:
            current_parent_id = pending.pop()
            if current_parent_id in seen:
                continue
            seen.add(current_parent_id)
            child_ids = [
                relation.related_agent_run_id
                for relation in self._relations.values()
                if relation.owner_agent_run_id == current_parent_id
                and relation.related_agent_run_id not in seen
                and relation.related_agent_run_id in self._states
                and relation.status.value == "active"
            ]
            for child_id in child_ids:
                child = self._states[child_id].task
                child_reason = f"parent_cancelled:{reason}"
                if self._cancel_task_locked(child, reason=child_reason):
                    self._append_event_locked(
                        child_id,
                        "parent_cancelled",
                        {"owner_agent_run_id": current_parent_id, "reason": reason},
                    )
                pending.append(child_id)

    def _store_descendant_agent_runs(self, owner_agent_run_id: str) -> list[AgentRun]:
        if self._store is None:
            return []
        list_descendants = getattr(self._store, "list_descendant_agent_runs", None)
        if not callable(list_descendants):
            return []
        try:
            descendants = list_descendants(owner_agent_run_id, include_terminal=False)
        except TypeError:
            descendants = list_descendants(owner_agent_run_id)
        return [
            task
            for task in descendants
            if isinstance(task, AgentRun)
        ]

    def _append_parent_terminal_event_locked(self, task: AgentRun) -> None:
        relations = [
            relation
            for relation in self._relations.values()
            if relation.related_agent_run_id == task.id
            and relation.owner_agent_run_id != task.id
            and relation.owner_agent_run_id in self._states
            and relation.status.value == "active"
        ]
        activation = self._activations.get(task.current_activation_id or "")
        task_prompt = activation.prompt if activation is not None else ""
        for relation in relations:
            owner_run_id = relation.owner_agent_run_id
            if relation.relation_type in {
                AgentRunRelationType.AGENT_CALL_EPHEMERAL,
                AgentRunRelationType.AGENT_CALL_PERSISTENT,
            }:
                self._append_agent_call_feedback_locked(task, relation)
                self._append_event_locked(
                    owner_run_id,
                    _agent_call_feedback_kind(task).value,
                    _agent_call_feedback_payload(task, relation=relation),
                )
                if (
                    relation.relation_type == AgentRunRelationType.AGENT_CALL_EPHEMERAL
                    or relation.payload.get("lifecycle_hook_id")
                ):
                    for event_type, payload in agent_relation_terminal_lifecycle_events(
                        task,
                        owner_agent_run_id=owner_run_id,
                        relation_metadata=relation.payload,
                        task_prompt=task_prompt,
                    ):
                        self._append_event_locked(owner_run_id, event_type, payload)
                continue
            self._append_event_locked(
                owner_run_id,
                "agent_relation_completed",
                _agent_relation_completed_payload(
                    task,
                    owner_agent_run_id=owner_run_id,
                    task_prompt=task_prompt,
                ),
            )
            for event_type, payload in agent_relation_terminal_lifecycle_events(
                task,
                owner_agent_run_id=owner_run_id,
                relation_metadata=relation.payload,
                task_prompt=task_prompt,
            ):
                self._append_event_locked(owner_run_id, event_type, payload)

    def _resume_from_pending_agent_call_feedback_locked(self, owner_run_id: str) -> bool:
        owner_state = self._states.get(owner_run_id)
        owner = owner_state.task if owner_state is not None else None
        if (
            owner is None
            or owner.status != AgentRunStatus.WAITING
            or owner.waiting_reason != AgentRunWaitingReason.AGENT_CALL
            or not _activation_can_resume_from_feedback(
                self._activations.get(owner.current_activation_id or "")
            )
        ):
            return False
        for feedback in self._feedbacks.get(owner_run_id, []):
            if (
                not feedback.requires_activation
                or feedback.consumed_by_activation_id
                or feedback.kind not in _AGENT_CALL_FEEDBACK_KINDS
            ):
                continue
            payload = dict(feedback.payload)
            self.continue_agent_run(
                owner_run_id,
                input_kind=AgentRunActivationInputKind.AGENT_FEEDBACK,
                input_payload={
                    "feedback_id": feedback.id,
                    "kind": feedback.kind.value,
                    "target_agent_run_id": str(
                        payload.get("target_agent_run_id")
                        or feedback.metadata.get("target_agent_run_id")
                        or ""
                    ),
                },
                feedback_id=feedback.id,
                resume_session=True,
                prompt=_agent_call_feedback_prompt(payload),
            )
            return True
        return False

    def _append_agent_call_feedback_locked(
        self,
        task: AgentRun,
        relation: AgentRunRelation,
    ) -> None:
        owner_run_id = relation.owner_agent_run_id
        wait = bool(dict(relation.payload).get("wait") is True)
        payload = _agent_call_feedback_payload(task, relation=relation)
        feedback = AgentRunFeedback(
            id=_new_id("feedback"),
            agent_run_id=owner_run_id,
            source=AgentRunFeedbackSource.AGENT,
            kind=_agent_call_feedback_kind(task),
            payload=payload,
            created_at=datetime.now(timezone.utc).isoformat(),
            visibility=AgentRunFeedbackVisibility.INTERNAL,
            requires_activation=wait,
            metadata={
                "target_agent_run_id": task.id,
                "relation_id": relation.id,
            },
        )
        self._feedbacks.setdefault(owner_run_id, []).append(feedback)
        self._append_event_locked(
            owner_run_id,
            "agent_run_feedback_added",
            {
                "feedback_id": feedback.id,
                "feedback": _feedback_to_dict(feedback),
            },
        )
        if wait:
            self._resume_from_pending_agent_call_feedback_locked(owner_run_id)

    def attach_artifact(
        self,
        task_id: str,
        *,
        type: str,
        status: str = "generated",
        artifact_id: str | None = None,
        branch_name: str | None = None,
        pr_url: str | None = None,
        content: str | None = None,
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskArtifact:
        if self._store is not None:
            artifact = self._store.attach_artifact(
                task_id,
                type=type,
                status=status,
                artifact_id=artifact_id,
                branch_name=branch_name,
                pr_url=pr_url,
                content=content,
                path=path,
                metadata=metadata,
            )
            self.notify_task_available()
            return artifact
        with self._lock:
            state = self._states[task_id]
            artifact = state.attach_artifact(
                artifact_id=artifact_id or _new_id("artifact"),
                type=type,
                status=status,
                branch_name=branch_name,
                pr_url=pr_url,
                content=content,
                path=path,
                metadata=metadata,
            )
            if artifact.type == ArtifactType.PULL_REQUEST:
                state.issue_status = IssueStatus.IN_REVIEW
            self._append_event_locked(
                task_id,
                "artifact_attached",
                artifact_attached_event_payload(artifact),
            )
            return artifact

    def create_or_update_pr(self, task_id: str, *, diff: str = "") -> TaskArtifact:
        if self._store is not None:
            artifact = self._store.create_or_update_pr(task_id, diff=diff)
            self.notify_task_available()
            return artifact
        with self._lock:
            task = self._task_locked(task_id)
            session = self._sessions.get(task_id)
            pr = self.pr_flow.create_or_update(
                task,
                diff=diff,
                branch_name=(session.branch if session else None)
                or _worktree_branch_for_activation(
                    self._activations.get(task.current_activation_id or "")
                )
                or None,
            )
            return self.attach_artifact(
                task_id,
                type=ArtifactType.PULL_REQUEST.value,
                status=ArtifactStatus.PR_CREATED.value,
                branch_name=pr.branch_name,
                pr_url=pr.pr_url,
                content=diff,
                metadata=pr.metadata,
            )

    def list_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> list[AgentRunEvent]:
        limit = clamp_event_limit(limit)
        if self._store is not None:
            return self._store.list_events(task_id, after_seq=after_seq, limit=limit)
        with self._lock:
            return [
                event
                for event in list(self._events.get(task_id, []))
                if event.seq > after_seq
            ][:limit]

    def wait_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        timeout_sec: float = 0.0,
        limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> list[AgentRunEvent]:
        limit = clamp_event_limit(limit)
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while True:
            events = self.list_events(task_id, after_seq=after_seq, limit=limit)
            if events or timeout_sec <= 0:
                return events
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            with self._wakeup:
                self._wakeup.wait(timeout=min(remaining, 1.0))

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        if self._store is not None:
            return self._store.list_artifacts(task_id)
        with self._lock:
            return list(self._states[task_id].artifacts.values())

    def get_agent_run(self, task_id: str) -> AgentRun:
        if self._store is not None:
            return self._store.get_agent_run(task_id)
        with self._lock:
            return self._task_locked(task_id)

    def agent_run_to_dict(self, task_id: str) -> dict[str, Any]:
        if self._store is not None:
            return self._store.agent_run_to_dict(task_id)
        return _agent_run_to_dict(self.get_agent_run(task_id))

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.artifacts_to_dict(task_id)
        return [_artifact_to_dict(artifact) for artifact in self.list_artifacts(task_id)]

    def list_agent_runs(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
        after_created_at: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.list_agent_runs(
                status=status,
                agent_id=agent_id,
                limit=limit,
                after_created_at=after_created_at,
            )
        with self._lock:
            tasks = [_agent_run_to_dict(state.task) for state in self._states.values()]
            if status:
                tasks = [task for task in tasks if task.get("status") == status]
            if agent_id:
                tasks = [task for task in tasks if task.get("agent_id") == agent_id]
            return list(reversed(tasks))[: max(1, int(limit or 50))]

    def load_agent_run_detail(
        self,
        task_id: str,
        *,
        event_limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> dict[str, Any]:
        event_limit = clamp_event_limit(event_limit)
        if self._store is not None:
            return self._store.load_agent_run_detail(task_id, event_limit=event_limit)
        task = self.agent_run_to_dict(task_id)
        with self._lock:
            raw_events = list(self._events.get(task_id, []))[-event_limit:]
            activations = [
                _activation_to_dict(activation)
                for activation in sorted(
                    (
                        activation
                        for activation in self._activations.values()
                        if activation.agent_run_id == task_id
                    ),
                    key=lambda activation: activation.seq,
                )
            ]
            feedback = [
                _feedback_to_dict(item)
                for item in self._feedbacks.get(task_id, [])
            ]
            activation_steers = [
                _steer_to_dict(item)
                for item in self._steers.get(task_id, [])
            ]
            relations = [
                _relation_to_dict(item)
                for item in self._relations.values()
                if item.owner_agent_run_id == task_id or item.related_agent_run_id == task_id
            ]
            agent_thread_bindings = [
                _agent_thread_binding_to_dict(item)
                for item in self._agent_thread_bindings.values()
                if item.main_agent_run_id == task_id
                or item.target_agent_run_id == task_id
            ]
        events = [event.to_dict() for event in raw_events]
        session = self._sessions.get(task_id)
        return {
            "agent_run": task,
            "artifacts": self.artifacts_to_dict(task_id),
            "session": {
                "agent_id": session.agent_id,
                "executor": session.executor.value,
                "execution_location": session.execution_location.value,
                "task_id": session.task_id,
                "workdir": session.workdir,
                "branch": session.branch,
                "executor_session_id": session.executor_session_id,
                "metadata": dict(session.metadata),
            }
            if session is not None
            else None,
            "claim": None,
            "activations": activations,
            "activation_steers": activation_steers,
            "feedback": feedback,
            "relations": relations,
            "agent_thread_bindings": agent_thread_bindings,
            "events": events,
        }

    def _running_count_locked(self) -> int:
        return len(self._running_tasks_locked())

    def _running_tasks_locked(self) -> list[AgentRun]:
        return [
            state.task
            for state in self._states.values()
            if state.task.status
            in {AgentRunStatus.DISPATCHED, AgentRunStatus.RUNNING, AgentRunStatus.WAITING}
        ]

    def _task_locked(self, task_id: str) -> AgentRun:
        state = self._states.get(task_id)
        if state is None:
            raise KeyError(f"AgentRun not found: {task_id}")
        return state.task

    def _validate_completion_activation_locked(
        self,
        task: AgentRun,
        activation_id: str,
    ) -> AgentRunActivation:
        if not activation_id:
            raise ValueError("activation_id_required")
        activation = self._activations.get(activation_id)
        if activation is None or activation.agent_run_id != task.id:
            raise ValueError("activation_not_found")
        if str(task.current_activation_id or "") != activation_id:
            raise ValueError("activation_mismatch")
        return activation

    def _active_claim_for_task_locked(self, task_id: str) -> dict[str, Any] | None:
        for lease in self._claim_leases.values():
            if str(lease.get("task_id") or "") == task_id:
                return lease
        return None

    def _validate_activation_claim_owner_locked(
        self,
        *,
        request_id: str,
        task_id: str,
        activation_id: str | None = None,
        worker_id: str,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        if task_id not in self._states:
            return False, "agent_run_not_found"
        lease = self._claim_leases.get(request_id)
        if lease is None:
            return False, "claim_not_found"
        if str(lease.get("task_id") or "") != task_id:
            return False, "task_mismatch"
        if str(lease.get("worker_id") or "") != worker_id:
            return False, "worker_mismatch"
        expected_activation = str(lease.get("activation_id") or "")
        if activation_id and expected_activation != activation_id:
            return False, "activation_mismatch"
        expected_peer = str(lease.get("peer_id") or "")
        if peer_id and expected_peer and expected_peer != peer_id:
            return False, "peer_mismatch"
        return True, ""

    def _clear_task_claims_locked(self, task_id: str) -> None:
        for request_id, claim in list(self._claims.items()):
            if claim.task.id == task_id:
                self._claims.pop(request_id, None)
                self._claim_leases.pop(request_id, None)

    def _append_event_locked(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> AgentRunEvent:
        events = self._events.setdefault(task_id, [])
        event = AgentRunEvent(
            task_id=task_id,
            seq=len(events) + 1,
            type=event_type,
            payload=payload,
        )
        events.append(event)
        self.notify_task_available()
        return event


__all__ = [
    "AgentRunControlPlane",
    "AgentCallDispatchError",
    "AgentRunActivationClaim",
    "AgentRunEvent",
    "AgentRunRequest",
    "InMemoryPRFlow",
    "PRArtifactResult",
    "PRFlow",
    "AgentRunActivationClaim",
    "AgentRunEvent",
    "AgentRunRequest",
]
