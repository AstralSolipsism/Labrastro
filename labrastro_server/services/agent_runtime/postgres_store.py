"""Postgres-backed AgentRun control-plane store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import hashlib
import json

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AgentCallGrant,
    AGENT_RUN_METADATA_ACTIVATION_KEYS,
    ActivationSteer,
    ActivationSteerSource,
    ActivationSteerStatus,
    AgentRunActivation,
    AgentRunActivationInputKind,
    AgentRunActivationStatus,
    AgentRunFeedback,
    AgentRunFeedbackKind,
    AgentRunFeedbackSource,
    AgentRunFeedbackVisibility,
    AgentRunRelation,
    AgentRunRelationType,
    AgentRunResumePolicy,
    AgentRunSource,
    AgentThreadBinding,
    AgentThreadBindingLifetime,
    AgentThreadBindingStatus,
    SessionRunBinding,
    SessionRunBindingStatus,
    AgentRunWaitingReason,
    ArtifactStatus,
    ArtifactType,
    ExecutionLocation,
    ExecutorType,
    ModelRequestOrigin,
    MergeStatus,
    PublishPolicy,
    TaskArtifact,
    AgentRun,
    TaskSessionRef,
    AgentRunStatus,
    TriggerMode,
    WorkerKind,
    WorktreeRole,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunRequest,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.environment_events import (
    environment_summary_event,
    expand_environment_executor_event,
)
from labrastro_server.services.agent_runtime.permission_events import (
    blocked_review_event_payload,
    should_block_waiting_approval,
)
from labrastro_server.services.agent_runtime.lifecycle import IssueStatus
from labrastro_server.services.agent_runtime.prompt_renderer import (
    CanonicalAgentContext,
    ExecutorPromptRenderer,
)
from labrastro_server.services.agent_runtime.runtime_store import (
    DEFAULT_RUNTIME_EVENT_LIMIT,
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


try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import bindparam, text
    from sqlalchemy.dialects.postgresql import JSONB
except ImportError:  # pragma: no cover
    bindparam = None
    text = None
    JSONB = None


def _require_sqlalchemy() -> None:
    if text is None or bindparam is None or JSONB is None:
        raise RuntimeError("Postgres runtime store requires sqlalchemy and psycopg.")


def _new_id(prefix: str) -> str:
    import uuid

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
    return f"labrastro-agent-run-{safe or _new_id('session')}"


def _ensure_reuleauxcoder_executor_session(request: Any, task_id: str) -> None:
    if request.executor == ExecutorType.REULEAUXCODER and not request.executor_session_id:
        request.executor_session_id = _default_executor_session_id(task_id)


def _dict_from(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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


def _can_resume_from_parent(request: Any, parent: AgentRun) -> bool:
    if not parent.executor_session_id:
        return False
    return (
        request.agent_id == parent.agent_id
        and request.runtime_profile_id == parent.runtime_profile_id
        and request.executor == parent.executor
        and request.execution_location == parent.execution_location
        and workspace_key(request.workdir) == workspace_key(parent.workdir)
    )


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


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _stable_json(value: Any) -> str:
    return json.dumps(
        _dict_from(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _agent_call_grant_scope_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _enum_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _jsonable_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def _steer_from_row(row: Any) -> ActivationSteer:
    return ActivationSteer(
        id=str(row["id"]),
        activation_id=str(row["activation_id"]),
        source=str(row["source"]),
        payload=_dict_from(row["payload"]),
        created_at=_timestamp_text(row["created_at"]) or "",
        delivered_at=_timestamp_text(row["delivered_at"]),
        status=str(row["status"]),
        metadata=_dict_from(row["metadata"]),
    )


def _timestamp_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _activation_from_row(row: Any) -> AgentRunActivation:
    return AgentRunActivation(
        id=str(row["id"]),
        agent_run_id=str(row["agent_run_id"]),
        seq=int(row["seq"]),
        input_kind=str(row["input_kind"]),
        input_payload=_dict_from(row["input_payload"]),
        prompt=str(row["prompt"] or ""),
        status=str(row["status"]),
        output=row["output"],
        result_payload=_dict_from(row["result_payload"]),
        worker_id=row["worker_id"],
        request_id=row["request_id"],
        started_at=_timestamp_text(row["started_at"]),
        ended_at=_timestamp_text(row["ended_at"]),
        metadata=_dict_from(row["metadata"]),
    )


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


def _feedback_from_row(row: Any) -> AgentRunFeedback:
    return AgentRunFeedback(
        id=str(row["id"]),
        agent_run_id=str(row["agent_run_id"]),
        source=str(row["source"]),
        kind=str(row["kind"]),
        payload=_dict_from(row["payload"]),
        created_at=_timestamp_text(row["created_at"]) or "",
        consumed_by_activation_id=row["consumed_by_activation_id"],
        visibility=str(row["visibility"]),
        requires_activation=bool(row["requires_activation"]),
        metadata=_dict_from(row["metadata"]),
    )


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


def _relation_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "owner_agent_run_id": str(row["owner_agent_run_id"]),
        "related_agent_run_id": str(row["related_agent_run_id"]),
        "relation_type": str(row["relation_type"]),
        "relation_scope": str(row["relation_scope"]),
        "created_by_activation_id": row["created_by_activation_id"],
        "status": str(row["status"]),
        "payload": _dict_from(row["payload"]),
        "metadata": _dict_from(row["metadata"]),
    }


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


def _agent_thread_binding_row_to_dict(row: Any) -> dict[str, Any]:
    row_data = _jsonable_row(row)
    return {
        "id": str(row_data.get("id") or ""),
        "owner_session_run_id": str(row_data.get("owner_session_run_id") or ""),
        "main_agent_run_id": str(row_data.get("main_agent_run_id") or ""),
        "agent_id": str(row_data.get("agent_id") or ""),
        "target_agent_run_id": str(row_data.get("target_agent_run_id") or ""),
        "thread_key": str(row_data.get("thread_key") or ""),
        "thread_summary": str(row_data.get("thread_summary") or ""),
        "binding_lifetime": str(
            row_data.get("binding_lifetime") or AgentThreadBindingLifetime.SESSION.value
        ),
        "workdir_policy": str(row_data.get("workdir_policy") or "inherit_main"),
        "visibility": str(
            row_data.get("visibility") or "hidden_from_user_transcript"
        ),
        "status": str(row_data.get("status") or AgentThreadBindingStatus.ACTIVE.value),
        "cleanup_policy": str(
            row_data.get("cleanup_policy") or "delete_with_owner_session"
        ),
        "created_at": row_data.get("created_at"),
        "updated_at": row_data.get("updated_at"),
        "metadata": _dict_from(row_data.get("metadata")),
    }


def _parallel_binding_from_row(row: Any) -> AgentThreadBinding:
    row_data = _jsonable_row(row)
    return AgentThreadBinding(
        id=str(row["id"]),
        owner_session_run_id=str(row["owner_session_run_id"] or ""),
        main_agent_run_id=str(row["main_agent_run_id"] or ""),
        agent_id=str(row["agent_id"] or ""),
        target_agent_run_id=str(row["target_agent_run_id"] or ""),
        thread_key=str(row["thread_key"] or ""),
        thread_summary=str(row_data.get("thread_summary") or ""),
        binding_lifetime=str(
            row["binding_lifetime"] or AgentThreadBindingLifetime.SESSION.value
        ),
        workdir_policy=str(row["workdir_policy"] or "inherit_main"),
        visibility=str(row["visibility"] or "hidden_from_user_transcript"),
        status=str(row["status"] or AgentThreadBindingStatus.ACTIVE.value),
        cleanup_policy=str(row["cleanup_policy"] or "delete_with_owner_session"),
        created_at=(
            row["created_at"].isoformat()
            if isinstance(row["created_at"], datetime)
            else row["created_at"]
        ),
        updated_at=(
            row["updated_at"].isoformat()
            if isinstance(row["updated_at"], datetime)
            else row["updated_at"]
        ),
        metadata=_dict_from(row["metadata"]),
    )


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


def _session_run_binding_row_to_dict(row: Any) -> dict[str, Any]:
    row_data = _jsonable_row(row)
    return {
        "id": str(row_data.get("id") or ""),
        "session_run_id": str(row_data.get("session_run_id") or ""),
        "session_id": str(row_data.get("session_id") or ""),
        "peer_id": str(row_data.get("peer_id") or ""),
        "branch_binding_id": str(row_data.get("branch_binding_id") or ""),
        "agent_run_id": str(row_data.get("agent_run_id") or ""),
        "selected": bool(row_data.get("selected")),
        "parent_branch_binding_id": str(row_data.get("parent_branch_binding_id") or ""),
        "base_session_item_id": str(row_data.get("base_session_item_id") or ""),
        "source_agent_run_id": str(row_data.get("source_agent_run_id") or ""),
        "target_agent_run_id": str(row_data.get("target_agent_run_id") or ""),
        "status": str(row_data.get("status") or SessionRunBindingStatus.ACTIVE.value),
        "created_at": row_data.get("created_at"),
        "updated_at": row_data.get("updated_at"),
        "metadata": _dict_from(row_data.get("metadata")),
    }


def _session_run_binding_from_row(row: Any) -> SessionRunBinding:
    return SessionRunBinding(**_session_run_binding_row_to_dict(row))


def _agent_call_grant_from_row(row: Any) -> AgentCallGrant:
    return AgentCallGrant(
        user_id=str(row["user_id"] or ""),
        grant_scope=str(row["grant_scope"] or ""),
        main_agent_id=str(row["main_agent_id"] or ""),
        target_agent_id=str(row["target_agent_id"] or ""),
        conversation_scope=str(row["conversation_scope"] or ""),
        capability_scope=_dict_from(row["capability_scope"]),
        target_config_version=str(row["target_config_version"] or ""),
        granted_at=(
            row["granted_at"].isoformat()
            if isinstance(row["granted_at"], datetime)
            else row["granted_at"]
        ),
        expires_at=(
            row["expires_at"].isoformat()
            if isinstance(row["expires_at"], datetime)
            else row["expires_at"]
        ),
        revoked_at=(
            row["revoked_at"].isoformat()
            if isinstance(row["revoked_at"], datetime)
            else row["revoked_at"]
        ),
        metadata=_dict_from(row["metadata"]),
    )


def _relation_from_submit_request(
    task_id: str,
    request: Any,
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


_TERMINAL_AGENT_RUN_STATUSES = {"completed", "failed", "cancelled", "blocked"}
_CANCEL_REQUEST_AGENT_RUN_STATUSES = {"dispatched", "running", "waiting"}
_ACTIVE_STEER_AGENT_RUN_STATUSES = {"running"}
_ACTIVE_AGENT_RUN_STATUSES = {"queued", "dispatched", "running", "waiting"}
_AGENT_CALL_FEEDBACK_KINDS = {
    AgentRunFeedbackKind.AGENT_CALL_RESULT.value,
    AgentRunFeedbackKind.AGENT_CALL_FAILED.value,
}


def _waiting_reason_for_required_feedback_kind(
    kind: str,
) -> AgentRunWaitingReason:
    if kind == AgentRunFeedbackKind.USER_MESSAGE.value:
        return AgentRunWaitingReason.USER_INPUT
    if kind in {
        AgentRunFeedbackKind.AGENT_CALL_RESULT.value,
        AgentRunFeedbackKind.AGENT_CALL_FAILED.value,
    }:
        return AgentRunWaitingReason.AGENT_CALL
    return AgentRunWaitingReason.SERVER_PROCESSING


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


def _agent_run_storage_values(task: AgentRun) -> dict[str, Any]:
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
        "terminal_result": _dict_from(task.terminal_result),
        "executor_session_id": task.executor_session_id,
        "current_activation_id": task.current_activation_id,
        "workdir": task.workdir,
        "sandbox_id": task.sandbox_id,
        "sandbox_session_id": task.sandbox_session_id,
        "workspace_ref": task.workspace_ref,
        "retention_scope": task.retention_scope,
        "cleanup_policy": task.cleanup_policy,
        "failure_reason": task.failure_reason,
        "cancel_reason": task.cancel_reason,
        "budget": _dict_from(task.metadata.get("budget")),
        "metadata": dict(task.metadata),
    }


def _worktree_branch_for_activation(activation: Any | None) -> str:
    input_payload = _dict_from(
        getattr(activation, "input_payload", None)
        if activation is not None
        else None
    )
    return str(input_payload.get("worktree_branch") or "").strip()


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


class PostgresAgentRunStore:
    """Durable AgentRun queue with Postgres transaction semantics."""

    def __init__(
        self,
        engine: Any,
        *,
        max_running_tasks: int = 4,
        runtime_snapshot: dict[str, Any] | None = None,
        pr_flow: Any | None = None,
    ) -> None:
        _require_sqlalchemy()
        self.engine = engine
        self.max_running_tasks = max(1, int(max_running_tasks or 1))
        self.runtime_snapshot = dict(runtime_snapshot or {})
        from labrastro_server.services.agent_runtime.control_plane import InMemoryPRFlow

        self.pr_flow = pr_flow or InMemoryPRFlow()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO labrastro_agent_run_locks(name) VALUES ('global_claim') "
                    "ON CONFLICT (name) DO NOTHING"
                )
            )
        self.recover_host_restarted_tasks()

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if max_running_tasks is not None:
            self.max_running_tasks = max(1, int(max_running_tasks or 1))
        if runtime_snapshot is not None:
            self.runtime_snapshot = dict(runtime_snapshot)

    def _upsert_activation_with_conn(self, conn: Any, activation: Any) -> None:
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_activations (
                    id, agent_run_id, seq, input_kind, input_payload,
                    prompt, status, output, result_payload, worker_id,
                    request_id, started_at, ended_at, metadata
                ) VALUES (
                    :id, :agent_run_id, :seq, :input_kind,
                    CAST(:input_payload AS JSONB), :prompt, :status, :output,
                    CAST(:result_payload AS JSONB), :worker_id, :request_id,
                    :started_at, :ended_at, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (id) DO UPDATE SET
                    input_kind=EXCLUDED.input_kind,
                    input_payload=EXCLUDED.input_payload,
                    prompt=EXCLUDED.prompt,
                    status=EXCLUDED.status,
                    output=EXCLUDED.output,
                    result_payload=EXCLUDED.result_payload,
                    worker_id=EXCLUDED.worker_id,
                    request_id=EXCLUDED.request_id,
                    started_at=EXCLUDED.started_at,
                    ended_at=EXCLUDED.ended_at,
                    metadata=EXCLUDED.metadata,
                    updated_at=now()
                """
            ),
            {
                "id": activation.id,
                "agent_run_id": activation.agent_run_id,
                "seq": int(activation.seq),
                "input_kind": _enum_text(activation.input_kind),
                "input_payload": _json(activation.input_payload),
                "prompt": activation.prompt or "",
                "status": _enum_text(activation.status),
                "output": activation.output,
                "result_payload": _json(activation.result_payload),
                "worker_id": activation.worker_id,
                "request_id": activation.request_id,
                "started_at": activation.started_at,
                "ended_at": activation.ended_at,
                "metadata": _json(activation.metadata),
            },
        )

    def _load_current_activation_with_conn(
        self,
        conn: Any,
        task: AgentRun,
    ) -> AgentRunActivation | None:
        row = None
        if task.current_activation_id:
            row = conn.execute(
                text(
                    """
                    SELECT id, agent_run_id, seq, input_kind, input_payload,
                           prompt, status, output, result_payload, worker_id,
                           request_id, started_at, ended_at, metadata
                    FROM labrastro_agent_run_activations
                    WHERE id=:activation_id AND agent_run_id=:task_id
                    """
                ),
                {
                    "activation_id": task.current_activation_id,
                    "task_id": task.id,
                },
            ).mappings().first()
        if row is None:
            row = conn.execute(
                text(
                    """
                    SELECT id, agent_run_id, seq, input_kind, input_payload,
                           prompt, status, output, result_payload, worker_id,
                           request_id, started_at, ended_at, metadata
                    FROM labrastro_agent_run_activations
                    WHERE agent_run_id=:task_id
                    ORDER BY seq DESC
                    LIMIT 1
                    """
                ),
                {"task_id": task.id},
            ).mappings().first()
        return _activation_from_row(row) if row is not None else None

    def _validate_completion_activation_with_conn(
        self,
        conn: Any,
        task: AgentRun,
        activation_id: str,
    ) -> AgentRunActivation:
        if not activation_id:
            raise ValueError("activation_id_required")
        row = conn.execute(
            text(
                """
                SELECT id, agent_run_id, seq, input_kind, input_payload,
                       prompt, status, output, result_payload, worker_id,
                       request_id, started_at, ended_at, metadata
                FROM labrastro_agent_run_activations
                WHERE id=:activation_id AND agent_run_id=:task_id
                """
            ),
            {"activation_id": activation_id, "task_id": task.id},
        ).mappings().first()
        if row is None:
            raise ValueError("activation_not_found")
        if str(task.current_activation_id or "") != activation_id:
            raise ValueError("activation_mismatch")
        return _activation_from_row(row)

    def _next_activation_seq_with_conn(self, conn: Any, task_id: str) -> int:
        value = conn.execute(
            text(
                """
                SELECT COALESCE(MAX(seq), 0) + 1
                FROM labrastro_agent_run_activations
                WHERE agent_run_id=:task_id
                """
            ),
            {"task_id": task_id},
        ).scalar()
        return int(value or 1)

    def _upsert_relation_with_conn(
        self,
        conn: Any,
        relation: AgentRunRelation,
    ) -> None:
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_relations (
                    id, owner_agent_run_id, related_agent_run_id, relation_type,
                    relation_scope, created_by_activation_id, status, payload, metadata
                ) VALUES (
                    :id, :owner_agent_run_id, :related_agent_run_id, :relation_type,
                    :relation_scope, :created_by_activation_id, :status,
                    CAST(:payload AS JSONB), CAST(:metadata AS JSONB)
                )
                ON CONFLICT (owner_agent_run_id, related_agent_run_id, relation_type)
                DO UPDATE SET
                    relation_scope=EXCLUDED.relation_scope,
                    created_by_activation_id=EXCLUDED.created_by_activation_id,
                    status=EXCLUDED.status,
                    payload=EXCLUDED.payload,
                    metadata=EXCLUDED.metadata,
                    updated_at=now()
                """
            ),
            {
                "id": relation.id,
                "owner_agent_run_id": relation.owner_agent_run_id,
                "related_agent_run_id": relation.related_agent_run_id,
                "relation_type": relation.relation_type.value,
                "relation_scope": relation.relation_scope,
                "created_by_activation_id": relation.created_by_activation_id,
                "status": relation.status.value,
                "payload": _json(relation.payload),
                "metadata": _json(relation.metadata),
            },
        )

    def upsert_agent_thread_binding(self, binding: AgentThreadBinding) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_agent_thread_bindings (
                        id, owner_session_run_id, main_agent_run_id, agent_id,
                        target_agent_run_id, thread_key, binding_lifetime, workdir_policy,
                        visibility, status, cleanup_policy, created_at, updated_at,
                        metadata
                    ) VALUES (
                        :id, :owner_session_run_id, :main_agent_run_id, :agent_id,
                        :target_agent_run_id, :thread_key, :binding_lifetime,
                        :workdir_policy, :visibility, :status, :cleanup_policy,
                        COALESCE(CAST(:created_at AS TIMESTAMPTZ), now()),
                        now(), CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (id)
                    DO UPDATE SET
                        target_agent_run_id=EXCLUDED.target_agent_run_id,
                        workdir_policy=EXCLUDED.workdir_policy,
                        visibility=EXCLUDED.visibility,
                        status=EXCLUDED.status,
                        cleanup_policy=EXCLUDED.cleanup_policy,
                        updated_at=now(),
                        metadata=EXCLUDED.metadata
                    """
                ),
                {
                    "id": binding.id,
                    "owner_session_run_id": binding.owner_session_run_id,
                    "main_agent_run_id": binding.main_agent_run_id,
                    "agent_id": binding.agent_id,
                    "target_agent_run_id": binding.target_agent_run_id,
                    "thread_key": binding.thread_key,
                    "binding_lifetime": binding.binding_lifetime.value,
                    "workdir_policy": binding.workdir_policy.value,
                    "visibility": binding.visibility,
                    "status": binding.status.value,
                    "cleanup_policy": binding.cleanup_policy,
                    "created_at": binding.created_at,
                    "metadata": _json(binding.metadata),
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
        status_filter = "" if include_inactive else "AND status='active'"
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT id, owner_session_run_id, main_agent_run_id, agent_id,
                           target_agent_run_id, thread_key, binding_lifetime, workdir_policy,
                           visibility, status, cleanup_policy, created_at,
                           updated_at, metadata
                    FROM labrastro_agent_thread_bindings
                    WHERE owner_session_run_id=:owner_session_run_id
                      AND main_agent_run_id=:main_agent_run_id
                      AND agent_id=:agent_id
                      AND thread_key=:thread_key
                      AND binding_lifetime=:binding_lifetime
                      {status_filter}
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "owner_session_run_id": str(owner_session_run_id or "").strip(),
                    "main_agent_run_id": str(main_agent_run_id or "").strip(),
                    "agent_id": str(agent_id or "").strip(),
                    "thread_key": str(thread_key or "").strip(),
                    "binding_lifetime": str(
                        binding_lifetime or AgentThreadBindingLifetime.SESSION.value
                    ).strip()
                    or AgentThreadBindingLifetime.SESSION.value,
                },
            ).mappings().first()
        return _parallel_binding_from_row(row) if row is not None else None

    def list_agent_thread_bindings(self, **filters: Any) -> list[AgentThreadBinding]:
        clauses: list[str] = ["1=1"]
        params: dict[str, Any] = {}
        for key in (
            "owner_session_run_id",
            "main_agent_run_id",
            "target_agent_run_id",
            "agent_id",
            "status",
        ):
            value = filters.get(key)
            if value is None:
                continue
            clauses.append(f"{key}=:{key}")
            params[key] = str(value)
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, owner_session_run_id, main_agent_run_id, agent_id,
                           target_agent_run_id, thread_key, binding_lifetime,
                           workdir_policy, visibility, status, cleanup_policy,
                           created_at, updated_at, metadata
                    FROM labrastro_agent_thread_bindings
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, id ASC
                    """
                ),
                params,
            ).mappings()
            return [_parallel_binding_from_row(row) for row in rows]

    def set_agent_thread_binding_status(
        self,
        binding_id: str,
        *,
        status: AgentThreadBindingStatus | str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AgentThreadBinding | None:
        binding_status = AgentThreadBindingStatus(str(getattr(status, "value", status)))
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, owner_session_run_id, main_agent_run_id, agent_id,
                           target_agent_run_id, thread_key, binding_lifetime,
                           workdir_policy, visibility, status, cleanup_policy,
                           created_at, updated_at, metadata
                    FROM labrastro_agent_thread_bindings
                    WHERE id=:binding_id
                    """
                ),
                {"binding_id": str(binding_id or "").strip()},
            ).mappings().first()
            if row is None:
                return None
            row_metadata = _dict_from(row["metadata"])
            row_metadata.update(dict(metadata or {}))
            if reason:
                row_metadata["status_reason"] = str(reason)
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_thread_bindings
                    SET status=:status,
                        metadata=CAST(:metadata AS JSONB),
                        updated_at=now()
                    WHERE id=:binding_id
                    """
                ),
                {
                    "binding_id": str(binding_id or "").strip(),
                    "status": binding_status.value,
                    "metadata": _json(row_metadata),
                },
            )
            binding = _parallel_binding_from_row(row)
            binding.status = binding_status
            binding.metadata = row_metadata
            event_type = (
                "agent_thread_binding_unavailable"
                if binding_status == AgentThreadBindingStatus.UNAVAILABLE
                else "agent_thread_binding_closed"
            )
            event_payload = {
                "binding_id": binding.id,
                "reason": str(reason or ""),
                "binding": _agent_thread_binding_to_dict(binding),
            }
            for task_id in (binding.main_agent_run_id, binding.target_agent_run_id):
                self._append_event(conn, task_id, event_type, event_payload)
        bindings = self.list_agent_thread_bindings()
        return next(
            (
                binding
                for binding in bindings
                if binding.id == str(binding_id or "").strip()
            ),
            None,
        )

    def delete_agent_thread_bindings_for_owner_session(
        self,
        owner_session_run_id: str,
        *,
        reason: str = "owner_session_deleted",
    ) -> list[AgentThreadBinding]:
        session_run_id = str(owner_session_run_id or "").strip()
        if not session_run_id:
            return []
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, owner_session_run_id, main_agent_run_id, agent_id,
                           target_agent_run_id, thread_key, binding_lifetime,
                           workdir_policy, visibility, status, cleanup_policy,
                           created_at, updated_at, metadata
                    FROM labrastro_agent_thread_bindings
                    WHERE owner_session_run_id=:owner_session_run_id
                    ORDER BY updated_at DESC, id ASC
                    """
                ),
                {"owner_session_run_id": session_run_id},
            ).mappings().all()
            bindings = [_parallel_binding_from_row(row) for row in rows]
            for binding in bindings:
                event_payload = {
                    "binding_id": binding.id,
                    "reason": str(reason or ""),
                    "binding": _agent_thread_binding_to_dict(binding),
                }
                for task_id in (binding.main_agent_run_id, binding.target_agent_run_id):
                    self._append_event(
                        conn,
                        task_id,
                        "agent_thread_binding_deleted",
                        event_payload,
                    )
            conn.execute(
                text(
                    """
                    DELETE FROM labrastro_agent_thread_bindings
                    WHERE owner_session_run_id=:owner_session_run_id
                    """
                ),
                {"owner_session_run_id": session_run_id},
            )
        return bindings

    def upsert_session_run_binding(self, binding: SessionRunBinding) -> None:
        with self.engine.begin() as conn:
            if (
                binding.selected
                and binding.status == SessionRunBindingStatus.ACTIVE
                and binding.session_run_id
            ):
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_session_run_bindings
                        SET selected=false, updated_at=now()
                        WHERE session_run_id=:session_run_id
                          AND id<>:id
                          AND status='active'
                        """
                    ),
                    {
                        "session_run_id": binding.session_run_id,
                        "id": binding.id,
                    },
                )
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_session_run_bindings (
                        id, session_run_id, session_id, peer_id, branch_binding_id,
                        agent_run_id, selected, parent_branch_binding_id,
                        base_session_item_id, source_agent_run_id, target_agent_run_id,
                        status, metadata, created_at, updated_at
                    ) VALUES (
                        :id, :session_run_id, :session_id, :peer_id,
                        :branch_binding_id, :agent_run_id, :selected,
                        :parent_branch_binding_id, :base_session_item_id,
                        :source_agent_run_id, :target_agent_run_id,
                        :status, CAST(:metadata AS JSONB),
                        COALESCE(CAST(:created_at AS TIMESTAMPTZ), now()), now()
                    )
                    ON CONFLICT (id)
                    DO UPDATE SET
                        session_id=EXCLUDED.session_id,
                        peer_id=EXCLUDED.peer_id,
                        branch_binding_id=EXCLUDED.branch_binding_id,
                        agent_run_id=EXCLUDED.agent_run_id,
                        selected=EXCLUDED.selected,
                        parent_branch_binding_id=EXCLUDED.parent_branch_binding_id,
                        base_session_item_id=EXCLUDED.base_session_item_id,
                        source_agent_run_id=EXCLUDED.source_agent_run_id,
                        target_agent_run_id=EXCLUDED.target_agent_run_id,
                        status=EXCLUDED.status,
                        metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """
                ),
                {
                    "id": binding.id,
                    "session_run_id": binding.session_run_id,
                    "session_id": binding.session_id,
                    "peer_id": binding.peer_id,
                    "branch_binding_id": binding.branch_binding_id,
                    "agent_run_id": binding.agent_run_id,
                    "selected": bool(binding.selected),
                    "parent_branch_binding_id": binding.parent_branch_binding_id,
                    "base_session_item_id": binding.base_session_item_id,
                    "source_agent_run_id": binding.source_agent_run_id,
                    "target_agent_run_id": binding.target_agent_run_id,
                    "status": binding.status.value,
                    "created_at": binding.created_at,
                    "metadata": _json(binding.metadata),
                },
            )
            self._append_event(
                conn,
                binding.agent_run_id,
                "session_run_binding_upserted",
                {"binding": _session_run_binding_to_dict(binding)},
            )

    def find_session_run_binding(
        self,
        *,
        session_run_id: str,
        branch_binding_id: str = "",
        selected_only: bool = True,
        include_inactive: bool = False,
    ) -> SessionRunBinding | None:
        clauses = ["session_run_id=:session_run_id"]
        params: dict[str, Any] = {
            "session_run_id": str(session_run_id or "").strip(),
        }
        branch_id = str(branch_binding_id or "").strip()
        if branch_id:
            clauses.append("(branch_binding_id=:branch_binding_id OR id=:branch_binding_id)")
            params["branch_binding_id"] = branch_id
        elif selected_only:
            clauses.append("selected=true")
        if not include_inactive:
            clauses.append("status='active'")
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT id, session_run_id, session_id, peer_id, branch_binding_id,
                           agent_run_id, selected, parent_branch_binding_id,
                           base_session_item_id, source_agent_run_id,
                           target_agent_run_id, status, metadata, created_at, updated_at
                    FROM labrastro_session_run_bindings
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, id ASC
                    LIMIT 1
                    """
                ),
                params,
            ).mappings().first()
        return _session_run_binding_from_row(row) if row is not None else None

    def list_session_run_bindings(self, **filters: Any) -> list[SessionRunBinding]:
        clauses: list[str] = ["1=1"]
        params: dict[str, Any] = {}
        for key in (
            "session_run_id",
            "session_id",
            "peer_id",
            "branch_binding_id",
            "agent_run_id",
            "selected",
            "status",
        ):
            value = filters.get(key)
            if value is None:
                continue
            clauses.append(f"{key}=:{key}")
            params[key] = bool(value) if key == "selected" else str(getattr(value, "value", value))
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, session_run_id, session_id, peer_id, branch_binding_id,
                           agent_run_id, selected, parent_branch_binding_id,
                           base_session_item_id, source_agent_run_id,
                           target_agent_run_id, status, metadata, created_at, updated_at
                    FROM labrastro_session_run_bindings
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, id ASC
                    """
                ),
                params,
            ).mappings()
            return [_session_run_binding_from_row(row) for row in rows]

    def mark_agent_call_waiting(
        self,
        task_id: str,
        *,
        target_agent_run_id: str,
        conversation_scope: str,
        thread_key: str = "",
        wait: bool = True,
    ) -> None:
        if not wait:
            return
        with self.engine.begin() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            if task.is_terminal:
                return
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status='waiting',
                        waiting_reason='agent_call',
                        resume_policy='external_event',
                        terminal_result='{}'::jsonb,
                        updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {"task_id": task_id},
            )
            task = self._task_from_row(self._task_row(conn, task_id))
            self._append_event(
                conn,
                task_id,
                "waiting",
                {
                    "agent_run": _agent_run_to_dict(task),
                    "waiting_reason": AgentRunWaitingReason.AGENT_CALL.value,
                    "resume_policy": AgentRunResumePolicy.EXTERNAL_EVENT.value,
                    "target_agent_run_id": str(target_agent_run_id or "").strip(),
                    "conversation_scope": str(conversation_scope or "").strip(),
                    "thread_key": str(thread_key or "").strip(),
                },
            )

    def upsert_agent_call_grant(self, grant: AgentCallGrant) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_agent_call_grants (
                        user_id, grant_scope, main_agent_id, target_agent_id,
                        conversation_scope,
                        capability_scope_hash, capability_scope,
                        target_config_version, granted_at, expires_at,
                        revoked_at, metadata, updated_at
                    ) VALUES (
                        :user_id, :grant_scope, :main_agent_id, :target_agent_id,
                        :conversation_scope,
                        :capability_scope_hash, CAST(:capability_scope AS JSONB),
                        :target_config_version,
                        COALESCE(CAST(:granted_at AS TIMESTAMPTZ), now()),
                        CAST(:expires_at AS TIMESTAMPTZ),
                        CAST(:revoked_at AS TIMESTAMPTZ),
                        CAST(:metadata AS JSONB),
                        now()
                    )
                    ON CONFLICT (
                        user_id, grant_scope, main_agent_id, target_agent_id,
                        conversation_scope,
                        capability_scope_hash, target_config_version
                    )
                    DO UPDATE SET
                        capability_scope=EXCLUDED.capability_scope,
                        granted_at=EXCLUDED.granted_at,
                        expires_at=EXCLUDED.expires_at,
                        revoked_at=EXCLUDED.revoked_at,
                        metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """
                ),
                {
                    "user_id": grant.user_id,
                    "grant_scope": grant.grant_scope,
                    "main_agent_id": grant.main_agent_id,
                    "target_agent_id": grant.target_agent_id,
                    "conversation_scope": grant.conversation_scope,
                    "capability_scope_hash": _agent_call_grant_scope_hash(
                        grant.capability_scope
                    ),
                    "capability_scope": _json(grant.capability_scope),
                    "target_config_version": grant.target_config_version,
                    "granted_at": grant.granted_at,
                    "expires_at": grant.expires_at,
                    "revoked_at": grant.revoked_at,
                    "metadata": _json(grant.metadata),
                },
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
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT user_id, grant_scope, main_agent_id, target_agent_id,
                           conversation_scope,
                           capability_scope, target_config_version, granted_at,
                           expires_at, revoked_at, metadata
                    FROM labrastro_agent_call_grants
                    WHERE user_id=:user_id
                      AND grant_scope=:grant_scope
                      AND main_agent_id=:main_agent_id
                      AND target_agent_id=:target_agent_id
                      AND conversation_scope=:conversation_scope
                      AND capability_scope_hash=:capability_scope_hash
                      AND target_config_version=:target_config_version
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                    LIMIT 1
                    """
                ),
                {
                    "user_id": str(user_id or "").strip(),
                    "grant_scope": str(grant_scope or "").strip(),
                    "main_agent_id": str(main_agent_id or "").strip(),
                    "target_agent_id": str(target_agent_id or "").strip(),
                    "conversation_scope": str(conversation_scope or "").strip(),
                    "capability_scope_hash": _agent_call_grant_scope_hash(
                        capability_scope or {}
                    ),
                    "target_config_version": str(target_config_version or "").strip(),
                },
            ).mappings().first()
        return _agent_call_grant_from_row(row) if row is not None else None

    def _agent_run_exists_with_conn(self, conn: Any, task_id: str) -> bool:
        return bool(
            conn.execute(
                text("SELECT 1 FROM labrastro_agent_runs WHERE id=:task_id"),
                {"task_id": task_id},
            ).scalar()
        )

    def submit_agent_run(self, request: Any, *, task_id: str | None = None) -> AgentRun:
        task_id = task_id or _new_id("task")
        request = self._resolve_request(request)
        _ensure_reuleauxcoder_executor_session(request, task_id)
        metadata = dict(request.metadata)
        if request.sandbox_id:
            metadata.setdefault("sandbox_id", request.sandbox_id)
        if request.sandbox_session_id:
            metadata.setdefault("sandbox_session_id", request.sandbox_session_id)
        if request.workspace_ref:
            metadata.setdefault("workspace_ref", request.workspace_ref)
        if request.model is not None:
            metadata.setdefault("model", request.model)
        if getattr(request, "budget", None):
            metadata.setdefault("budget", dict(request.budget))
        if getattr(request, "worker_kind", None) is not None:
            metadata.setdefault("worker_kind", request.worker_kind.value)
        if getattr(request, "model_request_origin", None) is not None:
            metadata.setdefault(
                "model_request_origin",
                request.model_request_origin.value,
            )
        if getattr(request, "worktree_role", None) is not None:
            metadata.setdefault("worktree_role", request.worktree_role.value)
        if getattr(request, "publish_policy", None) is not None:
            metadata.setdefault("publish_policy", request.publish_policy.value)
        from labrastro_server.services.agent_runtime.control_plane import (
            _initial_activation_for_request,
        )

        metadata, activation = _initial_activation_for_request(
            metadata,
            task_id=task_id,
            request=request,
        )
        relation = _relation_from_submit_request(task_id, request)
        task = AgentRun(
            id=task_id,
            agent_id=request.agent_id,
            owner_session_run_id=str(getattr(request, "owner_session_run_id", "") or ""),
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
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_agent_runs (
                        id, agent_id, kind, owner_session_run_id, source,
                        trigger_mode, status, waiting_reason, resume_policy,
                        runtime_profile_id, executor, execution_location,
                        worktree_role, publish_policy, executor_session_id,
                        current_activation_id, workdir, sandbox_id,
                        sandbox_session_id, workspace_ref, retention_scope,
                        cleanup_policy, metadata, runtime_snapshot
                    ) VALUES (
                        :id, :agent_id, :kind, :owner_session_run_id, :source,
                        :trigger_mode, :status, :waiting_reason, :resume_policy,
                        :runtime_profile_id, :executor, :execution_location,
                        :worktree_role, :publish_policy, :executor_session_id,
                        :current_activation_id, :workdir, :sandbox_id,
                        :sandbox_session_id, :workspace_ref, :retention_scope,
                        :cleanup_policy, CAST(:metadata AS JSONB),
                        CAST(:runtime_snapshot AS JSONB)
                    )
                    """
                ),
                {
                    **_agent_run_storage_values(task),
                    "kind": task.kind,
                    "owner_session_run_id": task.owner_session_run_id,
                    "source": task.source.value,
                    "trigger_mode": task.trigger_mode.value,
                    "status": task.status.value,
                    "waiting_reason": task.waiting_reason.value if task.waiting_reason else None,
                    "resume_policy": task.resume_policy.value if task.resume_policy else None,
                    "executor": task.executor.value if task.executor else None,
                    "execution_location": (
                        task.execution_location.value if task.execution_location else None
                    ),
                    "worktree_role": task.worktree_role.value if task.worktree_role else None,
                    "publish_policy": task.publish_policy.value if task.publish_policy else None,
                    "metadata": _json(metadata),
                    "runtime_snapshot": _json(self.runtime_snapshot),
                },
            )
            self._append_event(conn, task.id, "queued", {"agent_run": _agent_run_to_dict(task)})
            from labrastro_server.services.agent_runtime.control_plane import (
                _activation_to_dict,
            )

            self._upsert_activation_with_conn(conn, activation)
            self._append_event(
                conn,
                task.id,
                "activation_queued",
                {
                    "activation_id": activation.id,
                    "activation": _activation_to_dict(activation),
                },
            )
            if relation is not None:
                self._upsert_relation_with_conn(conn, relation)
                relation_payload = {"relation": _relation_to_dict(relation)}
                self._append_event(
                    conn,
                    task.id,
                    "agent_run_relation_created",
                    relation_payload,
                )
                if self._agent_run_exists_with_conn(conn, relation.owner_agent_run_id):
                    self._append_event(
                        conn,
                        relation.owner_agent_run_id,
                        "agent_run_relation_created",
                        relation_payload,
                    )
        return task

    def claim_agent_run_activation(
        self,
        *,
        worker_id: str,
        worker_kind: Any | None = None,
        executors: list[Any] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> Any | None:
        from labrastro_server.services.agent_runtime.control_plane import (
            AgentRunActivationClaim,
            _activation_from_task,
            _activation_to_dict,
            _activation_with_runtime_state,
        )

        allowed = {_coerce_executor(executor) for executor in executors or []}
        features = (
            {str(feature) for feature in peer_features}
            if peer_features is not None
            else None
        )
        worker_kind_value = optional_worker_kind(worker_kind)
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT name FROM labrastro_agent_run_locks WHERE name='global_claim' FOR UPDATE")
            ).first()
            self._recover_stale_with_conn(conn)
            running_rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_agent_runs
                    WHERE status IN ('dispatched', 'running', 'waiting')
                    """
                )
            ).mappings().all()
            running_tasks = [self._task_from_row(row) for row in running_rows]
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_agent_runs
                    WHERE status = 'queued'
                    ORDER BY created_at ASC
                    LIMIT 100
                    FOR UPDATE SKIP LOCKED
                    """
                )
            ).mappings().all()
            for row in rows:
                task = self._task_from_row(row)
                if allowed and task.executor not in allowed:
                    continue
                if not self._worker_matches_task(
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
                if not self._agent_concurrency_allows(conn, task):
                    continue
                request_id = _new_id("claim")
                now = datetime.now(timezone.utc)
                effective_lease = max(1, int(lease_sec or 15))
                metadata = self._executor_metadata(task)
                activation = _activation_with_runtime_state(
                    self._load_current_activation_with_conn(conn, task)
                    or _activation_from_task(task),
                    status=AgentRunActivationStatus.DISPATCHED,
                    request_id=request_id,
                    worker_id=worker_id,
                )
                task.current_activation_id = activation.id
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='dispatched',
                            current_activation_id=:activation_id,
                            dispatched_at=COALESCE(dispatched_at, now()),
                            updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task.id, "activation_id": activation.id},
                )
                task.status = AgentRunStatus.DISPATCHED
                metadata.setdefault("activation_id", activation.id)
                metadata.setdefault("agent_run_id", task.id)
                self._upsert_activation_with_conn(conn, activation)
                conn.execute(
                    text(
                        """
                        INSERT INTO labrastro_agent_run_activation_claims (
                            request_id, task_id, activation_id, worker_id, peer_id, status,
                            lease_sec, lease_deadline, last_heartbeat_at,
                            runtime_snapshot, metadata
                        ) VALUES (
                            :request_id, :task_id, :activation_id, :worker_id, :peer_id, 'active',
                            :lease_sec,
                            :last_heartbeat_at + (:lease_sec * interval '1 second'),
                            :last_heartbeat_at,
                            CAST(:runtime_snapshot AS JSONB),
                            CAST(:metadata AS JSONB)
                        )
                        """
                    ),
                    {
                        "request_id": request_id,
                        "task_id": task.id,
                        "activation_id": activation.id,
                        "worker_id": worker_id,
                        "peer_id": peer_id or "",
                        "lease_sec": effective_lease,
                        "last_heartbeat_at": now,
                        "runtime_snapshot": _json(self.runtime_snapshot),
                        "metadata": _json(
                            {
                                "activation_id": activation.id,
                                "worker_kind": worker_kind_value.value
                                if worker_kind_value is not None
                                else "",
                            }
                        ),
                    },
                )
                self._append_event(
                    conn,
                    task.id,
                    "claimed",
                    {
                        "worker_id": worker_id,
                        "peer_id": peer_id,
                        "worker_kind": worker_kind_value.value
                        if worker_kind_value is not None
                        else None,
                        "request_id": request_id,
                        "activation_id": activation.id,
                        "activation": _activation_to_dict(activation),
                        "lease_sec": effective_lease,
                    },
                )
                return AgentRunActivationClaim(
                    request_id=request_id,
                    worker_id=worker_id,
                    task=task,
                    executor_request=ExecutorRunRequest(
                        task_id=task.id,
                        agent_id=task.agent_id,
                        executor=task.executor or ExecutorType.REULEAUXCODER,
                        prompt=activation.prompt,
                        execution_location=(
                            task.execution_location or ExecutionLocation.REMOTE_SERVER
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
        with self.engine.begin() as conn:
            row = self._active_claim(conn, request_id)
            if row is None:
                reason = self._cancel_reason(conn, task_id) or "claim_not_found"
                return {
                    "ok": False,
                    "cancel_requested": bool(reason),
                    "reason": reason,
                    "lease_sec": 0,
                }
            ok, reason = self._claim_owner_ok(
                row,
                task_id,
                worker_id,
                peer_id,
                activation_id=activation_id,
            )
            if not ok:
                return {
                    "ok": False,
                    "cancel_requested": True,
                    "reason": reason,
                    "lease_sec": 0,
                }
            effective_lease = max(1, int(lease_sec or row["lease_sec"] or 15))
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_run_activation_claims
                    SET last_heartbeat_at=now(),
                        lease_deadline=now() + (:lease_sec * interval '1 second'),
                        lease_sec=:lease_sec
                    WHERE request_id=:request_id
                    """
                ),
                {"request_id": request_id, "lease_sec": effective_lease},
            )
            task = self.get_agent_run(task_id)
            row_metadata = _dict_from(row.get("metadata"))
            activation_id = str(
                row.get("activation_id")
                or row_metadata.get("activation_id")
                or task.current_activation_id
                or ""
            )
            if task.status == AgentRunStatus.DISPATCHED:
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='running',
                            started_at=COALESCE(started_at, now()),
                            updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id},
                )
                task.status = AgentRunStatus.RUNNING
                self._append_event(conn, task_id, "status", {"status": "running"})
            cancel_reason = self._cancel_reason(conn, task_id)
            from labrastro_server.services.agent_runtime.control_plane import (
                _activation_from_task,
                _activation_with_runtime_state,
            )

            activation = _activation_with_runtime_state(
                self._load_current_activation_with_conn(conn, task)
                or _activation_from_task(task, activation_id=activation_id),
                status=AgentRunActivationStatus.RUNNING,
                request_id=request_id,
                worker_id=worker_id,
            )
            activation_id = activation_id or activation.id
            task.current_activation_id = activation_id
            self._upsert_activation_with_conn(conn, activation)
            for steer_id in [
                str(item)
                for item in delivered_steer_ids or []
                if str(item).strip()
            ]:
                delivered_rows = conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_run_activation_steers
                        SET status=:status,
                            delivered_at=now(),
                            metadata=metadata || CAST(:metadata AS JSONB)
                        WHERE id=:steer_id
                          AND activation_id=:activation_id
                          AND status IN ('queued', 'delivering')
                        RETURNING id, activation_id, source, payload, created_at,
                                  delivered_at, status, metadata
                        """
                    ),
                    {
                        "status": ActivationSteerStatus.DELIVERED.value,
                        "steer_id": steer_id,
                        "activation_id": activation_id,
                        "metadata": _json(
                            {
                                "delivered_by_request_id": request_id,
                                "delivered_by_worker_id": worker_id,
                            }
                        ),
                    },
                ).mappings().all()
                for steer_row in delivered_rows:
                    steer_payload = _jsonable_row(steer_row)
                    self._append_event(
                        conn,
                        task_id,
                        "activation_steer_delivered",
                        {
                            "activation_id": activation_id,
                            "steer_id": str(steer_row["id"]),
                            "steer": steer_payload,
                        },
                    )
            activation_steers: list[dict[str, Any]] = []
            if not cancel_reason:
                steer_rows = conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_run_activation_steers
                        SET status=:status,
                            metadata=metadata || CAST(:metadata AS JSONB)
                        WHERE activation_id=:activation_id
                          AND status='queued'
                        RETURNING id, activation_id, source, payload, created_at,
                                  delivered_at, status, metadata
                        """
                    ),
                    {
                        "status": ActivationSteerStatus.DELIVERING.value,
                        "activation_id": activation_id,
                        "metadata": _json(
                            {
                                "delivering_request_id": request_id,
                                "delivering_worker_id": worker_id,
                            }
                        ),
                    },
                ).mappings().all()
                for steer_row in steer_rows:
                    steer_payload = _jsonable_row(steer_row)
                    activation_steers.append(steer_payload)
                    self._append_event(
                        conn,
                        task_id,
                        "activation_steer_delivering",
                        {
                            "activation_id": activation_id,
                            "steer_id": str(steer_row["id"]),
                            "steer": steer_payload,
                        },
                    )
            return {
                "ok": True,
                "activation_id": activation_id,
                "cancel_requested": bool(cancel_reason),
                "reason": cancel_reason or "",
                "lease_sec": effective_lease,
                "activation_steers": activation_steers,
            }

    def validate_activation_claim_owner(
        self,
        *,
        request_id: str,
        task_id: str,
        activation_id: str | None = None,
        worker_id: str,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        with self.engine.begin() as conn:
            row = self._active_claim(conn, request_id)
            if row is None:
                return False, "claim_not_found"
            return self._claim_owner_ok(
                row,
                task_id,
                worker_id,
                peer_id,
                activation_id=activation_id,
            )

    def recover_stale_agent_runs(self, *, now: float | None = None) -> list[str]:
        with self.engine.begin() as conn:
            return self._recover_stale_with_conn(conn, now=now)

    def recover_host_restarted_tasks(self) -> list[str]:
        recovered: list[str] = []
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id FROM labrastro_agent_runs
                    WHERE status IN ('dispatched', 'running')
                    FOR UPDATE
                    """
                )
            ).mappings().all()
            for row in rows:
                task_id = str(row["id"])
                recovered.append(task_id)
                claim_row = conn.execute(
                    text(
                        """
                        SELECT activation_id FROM labrastro_agent_run_activation_claims
                        WHERE task_id=:task_id AND status='active'
                        ORDER BY claimed_at DESC
                        LIMIT 1
                        """
                    ),
                    {"task_id": task_id},
                ).mappings().first()
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='failed', failure_reason='host_restarted',
                            terminal_result=jsonb_build_object(
                                'output',
                                'host restarted while task was in flight'
                            ),
                            completed_at=now(), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id},
                )
                task = self._task_from_row(self._task_row(conn, task_id))
                from labrastro_server.services.agent_runtime.control_plane import (
                    _activation_from_task,
                    _activation_with_runtime_state,
                )

                activation_id = str(
                    claim_row["activation_id"]
                    if claim_row is not None and claim_row.get("activation_id")
                    else task.current_activation_id or ""
                )
                if activation_id:
                    task.current_activation_id = activation_id
                activation = _activation_with_runtime_state(
                    self._load_current_activation_with_conn(conn, task)
                    or _activation_from_task(task, activation_id=activation_id),
                    result_payload={"reason": "host_restarted"},
                )
                self._upsert_activation_with_conn(conn, activation)
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_run_activation_claims
                        SET status='released', released_at=now()
                        WHERE task_id=:task_id AND status='active'
                        """
                    ),
                    {"task_id": task_id},
                )
                self._append_event(
                    conn,
                    task_id,
                    "host_recovered_task_failed",
                    {"failure_reason": "host_restarted"},
                )
        return recovered

    def pin_session(self, task_id: str, session: TaskSessionRef) -> None:
        task = self.get_agent_run(task_id)
        with self.engine.begin() as conn:
            self._pin_session_with_conn(
                conn,
                task,
                session,
                metadata=self._session_metadata(task, session.metadata),
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
        with self.engine.begin() as conn:
            row = self._active_claim(conn, request_id)
            if row is None:
                return False, "claim_not_found"
            ok, reason = self._claim_owner_ok(
                row,
                task_id,
                worker_id,
                peer_id,
                activation_id=activation_id,
            )
            if not ok:
                return False, reason
            task = self._task_from_row(self._task_row(conn, task_id))
            session = TaskSessionRef(
                agent_id=task.agent_id,
                executor=task.executor or ExecutorType.REULEAUXCODER,
                execution_location=(
                    task.execution_location or ExecutionLocation.REMOTE_SERVER
                ),
                task_id=task_id,
                workdir=workdir if workdir else None,
                branch=branch if branch else None,
                executor_session_id=executor_session_id if executor_session_id else None,
                metadata=self._session_metadata(task, metadata),
            )
            self._pin_session_with_conn(
                conn,
                task,
                session,
                metadata=session.metadata,
            )
            if metadata:
                self._append_event(
                    conn,
                    task_id,
                    "session_metadata",
                    {"request_id": request_id, "worker_id": worker_id, **metadata},
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
        requested_activation_id = str(activation_id or "").strip()
        with self.engine.begin() as conn:
            activation_id = ""
            if request_id or worker_id or peer_id:
                row = self._active_claim(conn, request_id or "")
                if row is None:
                    return False, "claim_not_found"
                ok, reason = self._claim_owner_ok(
                    row,
                    task_id,
                    worker_id or "",
                    peer_id,
                    activation_id=requested_activation_id,
                )
                if not ok:
                    return False, reason
                row_metadata = _dict_from(row.get("metadata"))
                activation_id = str(
                    row.get("activation_id") or row_metadata.get("activation_id") or ""
                )
            task = self._task_from_row(self._task_row(conn, task_id))
            activation_id = activation_id or str(
                task.current_activation_id or ""
            )
            payload = event.to_dict()
            if activation_id:
                payload.setdefault("activation_id", activation_id)
            self._append_event(conn, task_id, event.type.value, payload)
            for event_type, payload in worktree_lifecycle_events(task, event):
                self._append_event(conn, task_id, event_type, payload)
            expansion = expand_environment_executor_event(task.metadata, event)
            for event_type, payload in expansion.events:
                self._append_event(conn, task_id, event_type, payload)
            if expansion.policy_error:
                metadata = dict(task.metadata)
                metadata["environment_policy_violation"] = expansion.policy_error
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='blocked', failure_reason=:failure_reason,
                            cancel_reason=NULL, metadata=CAST(:metadata AS JSONB),
                            updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {
                        "task_id": task_id,
                        "failure_reason": expansion.policy_error,
                        "metadata": _json(metadata),
                    },
                )
                self._append_event(
                    conn,
                    task_id,
                    "blocked",
                    {"error": expansion.policy_error},
                )
            if event.type.value == "status":
                status = str(event.data.get("status", ""))
                if status == "waiting_approval" and should_block_waiting_approval(task.source):
                    self._append_event(
                        conn,
                        task_id,
                        "permission.blocked_review",
                        blocked_review_event_payload(event.data),
                    )
                    conn.execute(
                        text(
                            """
                            UPDATE labrastro_agent_runs
                            SET status='blocked', waiting_reason=NULL,
                                resume_policy=NULL, failure_reason=:failure_reason,
                                cancel_reason=NULL, updated_at=now()
                            WHERE id=:task_id
                            """
                        ),
                        {
                            "task_id": task_id,
                            "failure_reason": str(
                                event.data.get("reason")
                                or event.data.get("message")
                                or "approval_required"
                            ),
                        },
                    )
                    return True, ""
                mapped = {
                    "waiting_approval": "waiting",
                    "running": "running",
                    "blocked": "blocked",
                }.get(status)
                if mapped:
                    failure_reason = (
                        str(
                            event.data.get("reason")
                            or event.data.get("message")
                            or "blocked"
                        )
                        if mapped == "blocked"
                        else None
                    )
                    conn.execute(
                        text(
                            """
                            UPDATE labrastro_agent_runs
                            SET status=:status,
                                waiting_reason=:waiting_reason,
                                resume_policy=:resume_policy,
                                failure_reason=CASE
                                    WHEN :failure_reason IS NULL THEN failure_reason
                                    ELSE :failure_reason
                                END,
                                cancel_reason=CASE
                                    WHEN :failure_reason IS NULL THEN cancel_reason
                                    ELSE NULL
                                END,
                                updated_at=now()
                            WHERE id=:task_id
                            """
                        ),
                        {
                            "task_id": task_id,
                            "status": mapped,
                            "waiting_reason": (
                                "user_approval" if mapped == "waiting" else None
                            ),
                            "resume_policy": (
                                "user_action" if mapped == "waiting" else None
                            ),
                            "failure_reason": failure_reason,
                        },
                    )
            return True, ""

    def append_agent_run_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event_type = str(event_type or "").strip()
        if not event_type:
            raise ValueError("agent_run_event_type_required")
        with self.engine.begin() as conn:
            self._task_row(conn, task_id)
            self._append_event(
                conn,
                task_id,
                event_type,
                dict(payload) if isinstance(payload, dict) else {},
            )

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
        with self.engine.begin() as conn:
            row = self._active_claim(conn, request_id, for_update=True)
            if row is None:
                return False, "claim_not_found", None
            ok, reason = self._claim_owner_ok(
                row,
                task_id,
                worker_id,
                peer_id,
                activation_id=activation_id,
            )
            if not ok:
                return False, reason, None
            completed = self._complete_agent_run_activation_with_conn(
                conn,
                task_id,
                result,
                activation_id=activation_id,
                artifacts=artifacts,
            )
        return True, "", completed

    def complete_agent_run_activation(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        activation_id: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentRun:
        with self.engine.begin() as conn:
            return self._complete_agent_run_activation_with_conn(
                conn,
                task_id,
                result,
                activation_id=activation_id,
                artifacts=artifacts,
            )

    def _complete_agent_run_activation_with_conn(
        self,
        conn: Any,
        task_id: str,
        result: ExecutorRunResult,
        *,
        activation_id: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentRun:
        completion_activation_id = str(activation_id or "").strip()
        task = self._task_from_row(self._task_row(conn, task_id))
        completion_activation = self._validate_completion_activation_with_conn(
            conn,
            task,
            completion_activation_id,
        )
        expanded_events = [
            (event, expand_environment_executor_event(task.metadata, event))
            for event in result.events
        ]
        policy_error = str(
            task.metadata.get("environment_policy_violation") or ""
        ).strip()
        for _, expansion in expanded_events:
            if expansion.policy_error and not policy_error:
                policy_error = expansion.policy_error
        if result.succeeded and not policy_error:
            required_feedback_kind = conn.execute(
                text(
                    """
                    SELECT kind
                    FROM labrastro_agent_run_feedback
                    WHERE agent_run_id=:task_id
                      AND requires_activation IS TRUE
                      AND consumed_by_activation_id IS NULL
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """
                ),
                {"task_id": task_id},
            ).scalar()
            if task.status == AgentRunStatus.WAITING or required_feedback_kind:
                status = "waiting"
                issue_status = str(getattr(task, "issue_status", "") or "open")
                output = ""
                waiting_reason = (
                    task.waiting_reason
                    or _waiting_reason_for_required_feedback_kind(
                        str(required_feedback_kind or "")
                    )
                )
                resume_policy = (
                    task.resume_policy
                    or (
                        AgentRunResumePolicy.EXTERNAL_EVENT
                        if waiting_reason
                        in {
                            AgentRunWaitingReason.AGENT_CALL,
                            AgentRunWaitingReason.SERVER_PROCESSING,
                        }
                        else AgentRunResumePolicy.USER_ACTION
                    )
                )
            else:
                status = "completed"
                issue_status = (
                    "in_review" if self._has_open_pr(conn, task_id) else "done"
                )
                output = result.output
                waiting_reason = None
                resume_policy = None
            failure_reason = None
            cancel_reason = None
        elif policy_error:
            status = "blocked"
            issue_status = "blocked"
            output = policy_error
            waiting_reason = None
            resume_policy = None
            failure_reason = policy_error
            cancel_reason = None
        elif result.status == "cancelled":
            status = "cancelled"
            issue_status = "blocked"
            waiting_reason = None
            resume_policy = None
            cancel_reason = (
                result.output
                or self._cancel_reason(conn, task_id)
                or result.error
                or "cancelled"
            )
            output = result.output or cancel_reason
            failure_reason = "cancelled"
        elif result.status == "blocked":
            status = "blocked"
            issue_status = "blocked"
            output = result.output or result.error
            waiting_reason = None
            resume_policy = None
            failure_reason = result.error or result.output or "blocked"
            cancel_reason = None
        else:
            status = "failed"
            issue_status = "blocked"
            output = result.output
            waiting_reason = None
            resume_policy = None
            failure_reason = result.error or "agent_error"
            cancel_reason = None
        metadata = dict(task.metadata)
        if policy_error:
            metadata["environment_policy_violation"] = policy_error
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_runs
                SET status=:status,
                    terminal_result=CAST(:terminal_result AS JSONB),
                    waiting_reason=:waiting_reason,
                    resume_policy=:resume_policy,
                    executor_session_id=COALESCE(:executor_session_id, executor_session_id),
                    issue_status=:issue_status,
                    failure_reason=:failure_reason,
                    cancel_reason=:cancel_reason,
                    metadata=CAST(:metadata AS JSONB),
                    completed_at=CASE
                        WHEN :mark_completed THEN now()
                        ELSE NULL
                    END,
                    updated_at=now()
                WHERE id=:task_id
                """
            ),
            {
                "task_id": task_id,
                "status": status,
                "terminal_result": _json({"output": output or ""}),
                "waiting_reason": (
                    waiting_reason.value if waiting_reason is not None else None
                ),
                "resume_policy": (
                    resume_policy.value if resume_policy is not None else None
                ),
                "executor_session_id": result.executor_session_id,
                "issue_status": issue_status,
                "failure_reason": failure_reason,
                "cancel_reason": cancel_reason,
                "metadata": _json(metadata),
                "mark_completed": status != "waiting",
            },
        )
        if result.executor_session_id:
            self._upsert_session_with_conn(
                conn,
                task,
                executor_session_id=result.executor_session_id,
                metadata=self._session_metadata(task),
            )
        for event, expansion in expanded_events:
            self._append_event(conn, task_id, event.type.value, event.to_dict())
            for event_type, payload in expansion.events:
                self._append_event(conn, task_id, event_type, payload)
            if expansion.policy_error:
                self._append_event(
                    conn,
                    task_id,
                    "blocked",
                    {"error": expansion.policy_error},
                )
        for artifact in executor_result_artifacts(result, artifacts):
            self._attach_artifact_with_conn(conn, task_id, **artifact)
        task = self._task_from_row(self._task_row(conn, task_id))
        summary = environment_summary_event(
            task.metadata,
            status,
            output=output or "",
            error=policy_error or result.error or "",
        )
        if summary is not None:
            self._append_event(conn, task_id, summary[0], summary[1])
        from labrastro_server.services.agent_runtime.control_plane import (
            _activation_status_for_result,
            _activation_to_dict,
            _activation_with_runtime_state,
        )

        claim_row = conn.execute(
            text(
                """
                SELECT * FROM labrastro_agent_run_activation_claims
                WHERE task_id=:task_id AND status='active'
                ORDER BY claimed_at DESC
                LIMIT 1
                """
            ),
            {"task_id": task_id},
        ).mappings().first()
        activation = _activation_with_runtime_state(
            completion_activation,
            status=_activation_status_for_result(result),
            request_id=str(claim_row["request_id"]) if claim_row is not None else None,
            worker_id=str(claim_row["worker_id"]) if claim_row is not None else None,
            output=output or "",
            result_payload=result.to_dict(),
        )
        self._upsert_activation_with_conn(conn, activation)
        self._append_event(
            conn,
            task_id,
            "activation_completed",
            {
                "activation_id": activation.id,
                "activation": _activation_to_dict(activation),
                "result": result.to_dict(),
            },
        )
        self._append_event(
            conn,
            task_id,
            status,
            {"result": result.to_dict(), "agent_run": _agent_run_to_dict(task)},
        )
        if task.is_terminal:
            self._append_parent_terminal_event(conn, task)
        self._release_claims(conn, task_id, status="completed")
        self._resolve_cancel(conn, task_id)
        self._resume_from_pending_agent_call_feedback(conn, task_id)
        return self._task_from_row(self._task_row(conn, task_id))

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
        with self.engine.begin() as conn:
            self._task_row(conn, task_id)
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_agent_run_feedback (
                        id, agent_run_id, source, kind, payload,
                        created_at, consumed_by_activation_id, visibility,
                        requires_activation, metadata
                    ) VALUES (
                        :id, :agent_run_id, :source, :kind,
                        CAST(:payload AS JSONB), CAST(:created_at AS TIMESTAMPTZ),
                        :consumed_by_activation_id, :visibility,
                        :requires_activation, CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "id": feedback.id,
                    "agent_run_id": feedback.agent_run_id,
                    "source": feedback.source.value,
                    "kind": feedback.kind.value,
                    "payload": _json(feedback.payload),
                    "created_at": feedback.created_at,
                    "consumed_by_activation_id": feedback.consumed_by_activation_id,
                    "visibility": feedback.visibility.value,
                    "requires_activation": feedback.requires_activation,
                    "metadata": _json(feedback.metadata),
                },
            )
            self._append_event(
                conn,
                task_id,
                "agent_run_feedback_added",
                {
                    "feedback_id": feedback.id,
                    "feedback": _feedback_to_dict(feedback),
                },
            )
        return feedback

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
        created_at = datetime.now(timezone.utc).isoformat()
        metadata_value = _normalize_activation_steer_metadata(
            metadata,
            source=source_value,
            fallback_key=resolved_steer_id,
        )
        with self.engine.begin() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            if task.status.value not in _ACTIVE_STEER_AGENT_RUN_STATUSES:
                raise ValueError("only active AgentRun activations can be steered")
            if task.metadata.get("activation_steer_supported") is False:
                raise ValueError("activation steer is not supported by this executor")
            activation_id = str(
                task.current_activation_id or ""
            )
            if not activation_id:
                raise ValueError("active AgentRun activation is required")
            active_claim = conn.execute(
                text(
                    """
                    SELECT request_id FROM labrastro_agent_run_activation_claims
                    WHERE task_id=:task_id
                      AND activation_id=:activation_id
                      AND status='active'
                    LIMIT 1
                    """
                ),
                {"task_id": task_id, "activation_id": activation_id},
            ).mappings().first()
            if active_claim is None:
                raise ValueError("active worker claim is required")
            _validate_activation_steer_payload(payload)
            sender, idempotency_key = _activation_steer_idempotency_scope(
                metadata_value
            )
            existing_row = conn.execute(
                text(
                    """
                    SELECT id, activation_id, source, payload, created_at,
                           delivered_at, status, metadata
                    FROM labrastro_agent_run_activation_steers
                    WHERE activation_id=:activation_id
                      AND metadata->>'sender'=:sender
                      AND metadata->>'idempotency_key'=:idempotency_key
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """
                ),
                {
                    "activation_id": activation_id,
                    "sender": sender,
                    "idempotency_key": idempotency_key,
                },
            ).mappings().first()
            if existing_row is not None:
                existing = _steer_from_row(existing_row)
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
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_agent_run_activation_steers (
                        id, activation_id, source, payload, created_at,
                        delivered_at, status, metadata
                    ) VALUES (
                        :id, :activation_id, :source, CAST(:payload AS JSONB),
                        CAST(:created_at AS TIMESTAMPTZ), :delivered_at,
                        :status, CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "id": steer.id,
                    "activation_id": steer.activation_id,
                    "source": steer.source.value,
                    "payload": _json(steer.payload),
                    "created_at": steer.created_at,
                    "delivered_at": steer.delivered_at,
                    "status": steer.status.value,
                    "metadata": _json(steer.metadata),
                },
            )
            self._append_event(
                conn,
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
        from labrastro_server.services.agent_runtime.control_plane import (
            _activation_id_for_task,
            _activation_to_dict,
            _validate_activation_input_payload,
        )

        input_kind_value = AgentRunActivationInputKind(
            getattr(input_kind, "value", input_kind)
        )
        with self.engine.begin() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            if not task.is_terminal and task.status != AgentRunStatus.WAITING:
                raise ValueError("only terminal or waiting AgentRuns can be continued")
            previous_activation = self._load_current_activation_with_conn(conn, task)
            previous_seq = (
                previous_activation.seq
                if previous_activation is not None
                else max(1, self._next_activation_seq_with_conn(conn, task.id) - 1)
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
            next_seq = self._next_activation_seq_with_conn(conn, task.id)
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
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status='queued',
                        waiting_reason=NULL,
                        resume_policy=NULL,
                        terminal_result='{}'::jsonb,
                        failure_reason=NULL, cancel_reason=NULL,
                        executor_session_id=CASE
                            WHEN :resume_session THEN executor_session_id
                            ELSE NULL
                        END,
                        current_activation_id=:activation_id,
                        completed_at=NULL,
                        updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "resume_session": bool(resume_session),
                    "activation_id": activation.id,
                },
            )
            task = self._task_from_row(self._task_row(conn, task_id))
            self._upsert_activation_with_conn(conn, activation)
            if feedback_id:
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_run_feedback
                        SET consumed_by_activation_id=:activation_id
                        WHERE id=:feedback_id AND agent_run_id=:task_id
                        """
                    ),
                    {
                        "activation_id": activation.id,
                        "feedback_id": feedback_id,
                        "task_id": task_id,
                    },
                )
                self._append_event(
                    conn,
                    task_id,
                    "agent_run_feedback_consumed",
                    {
                        "feedback_id": feedback_id,
                        "activation_id": activation.id,
                    },
                )
            self._append_event(
                conn,
                task.id,
                "activation_queued",
                {
                    "activation_id": activation.id,
                    "activation": _activation_to_dict(activation),
                    "retry_of_activation_id": previous_activation_id,
                    "feedback_id": feedback_id or "",
                },
            )
            return task

    def fail_agent_run(self, task_id: str, *, error: str) -> AgentRun:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status='failed',
                        terminal_result=CAST(:terminal_result AS JSONB),
                        failure_reason=:error, cancel_reason=NULL,
                        completed_at=now(), updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "error": error,
                    "terminal_result": _json({"output": error}),
                },
            )
            self._append_event(conn, task_id, "failed", {"error": error})
            task = self._task_from_row(self._task_row(conn, task_id))
            self._append_parent_terminal_event(conn, task)
            self._release_claims(conn, task_id, status="released")
            self._resolve_cancel(conn, task_id)
        return self.get_agent_run(task_id)

    def cancel_agent_run(self, task_id: str, *, reason: str = "user_cancelled") -> bool:
        task = self.get_agent_run(task_id)
        if task.is_terminal:
            return False
        with self.engine.begin() as conn:
            if task.sandbox_session_id:
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='cancelled',
                            terminal_result=CAST(:terminal_result AS JSONB),
                            failure_reason='cancelled', cancel_reason=:reason,
                            completed_at=now(), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {
                        "task_id": task_id,
                        "reason": reason,
                        "terminal_result": _json({"output": reason}),
                    },
                )
                self._append_event(conn, task_id, "cancelled", {"reason": reason})
                task = self._task_from_row(self._task_row(conn, task_id))
                self._append_parent_terminal_event(conn, task)
                self._release_claims(conn, task_id, status="cancelled")
                self._resolve_cancel(conn, task_id)
                self._cancel_child_agent_runs(conn, task_id, reason=reason)
                return True
            if task.status in {
                AgentRunStatus.DISPATCHED,
                AgentRunStatus.RUNNING,
                AgentRunStatus.WAITING,
            }:
                conn.execute(
                    text(
                        """
                        INSERT INTO labrastro_agent_run_cancel_requests(task_id, reason)
                        VALUES (:task_id, :reason)
                        ON CONFLICT (task_id) DO UPDATE
                        SET reason=EXCLUDED.reason, requested_at=now(), resolved_at=NULL
                        """
                    ),
                    {"task_id": task_id, "reason": reason},
                )
                claim_row = conn.execute(
                    text(
                        """
                        SELECT activation_id, worker_id FROM labrastro_agent_run_activation_claims
                        WHERE task_id=:task_id AND status='active'
                        ORDER BY claimed_at DESC
                        LIMIT 1
                        """
                    ),
                    {"task_id": task_id},
                ).mappings().first()
                self._append_event(
                    conn,
                    task_id,
                    "cancel_requested",
                    {
                        "reason": reason,
                        "activation_id": str(claim_row["activation_id"]) if claim_row else "",
                        "worker_id": str(claim_row["worker_id"]) if claim_row else "",
                    },
                )
                self._cancel_child_agent_runs(conn, task_id, reason=reason)
                return True
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status='cancelled',
                        terminal_result=CAST(:terminal_result AS JSONB),
                        failure_reason='cancelled', cancel_reason=:reason,
                        completed_at=now(), updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "reason": reason,
                    "terminal_result": _json({"output": reason}),
                },
            )
            self._append_event(conn, task_id, "cancelled", {"reason": reason})
            task = self._task_from_row(self._task_row(conn, task_id))
            self._append_parent_terminal_event(conn, task)
            self._release_claims(conn, task_id, status="cancelled")
            self._cancel_child_agent_runs(conn, task_id, reason=reason)
            return True

    def _cancel_child_agent_runs(
        self,
        conn: Any,
        owner_agent_run_id: str,
        *,
        reason: str,
        seen: set[str] | None = None,
    ) -> None:
        seen = seen or set()
        if owner_agent_run_id in seen:
            return
        seen.add(owner_agent_run_id)
        rows = conn.execute(
            text(
                """
                SELECT runs.id, runs.status, runs.metadata,
                       claims.activation_id, claims.worker_id
                FROM labrastro_agent_run_relations relations
                JOIN labrastro_agent_runs runs
                  ON runs.id = relations.related_agent_run_id
                LEFT JOIN labrastro_agent_run_activation_claims claims
                  ON claims.task_id = runs.id
                 AND claims.status = 'active'
                WHERE relations.owner_agent_run_id = :owner_agent_run_id
                  AND relations.status = 'active'
                  AND runs.id != :owner_agent_run_id
                  AND runs.status NOT IN ('completed', 'failed', 'cancelled', 'blocked')
                ORDER BY runs.created_at ASC
                """
            ),
            {"owner_agent_run_id": owner_agent_run_id},
        ).fetchall()
        for row in rows:
            item = row._mapping if hasattr(row, "_mapping") else row
            child_id = str(item["id"])
            child_status = str(item["status"] or "")
            child_reason = f"parent_cancelled:{reason}"
            child_metadata = _dict_from(item["metadata"])
            if child_metadata.get("sandbox_session_id"):
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='cancelled',
                            terminal_result=CAST(:terminal_result AS JSONB),
                            failure_reason='cancelled', cancel_reason=:reason,
                            completed_at=now(), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {
                        "task_id": child_id,
                        "reason": child_reason,
                        "terminal_result": _json({"output": child_reason}),
                    },
                )
                self._release_claims(conn, child_id, status="cancelled")
                self._resolve_cancel(conn, child_id)
                self._append_event(conn, child_id, "cancelled", {"reason": child_reason})
                child = self._task_from_row(self._task_row(conn, child_id))
                self._append_parent_terminal_event(conn, child)
            elif child_status in _CANCEL_REQUEST_AGENT_RUN_STATUSES:
                conn.execute(
                    text(
                        """
                        INSERT INTO labrastro_agent_run_cancel_requests(task_id, reason)
                        VALUES (:task_id, :reason)
                        ON CONFLICT (task_id) DO UPDATE
                        SET reason=EXCLUDED.reason, requested_at=now(), resolved_at=NULL
                        """
                    ),
                    {
                        "task_id": child_id,
                        "reason": child_reason,
                        "terminal_result": _json({"output": child_reason}),
                    },
                )
                self._append_event(
                    conn,
                    child_id,
                    "cancel_requested",
                    {
                        "reason": child_reason,
                        "activation_id": str(item["activation_id"] or ""),
                        "worker_id": str(item["worker_id"] or ""),
                    },
                )
            elif child_status not in _TERMINAL_AGENT_RUN_STATUSES:
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='cancelled',
                            terminal_result=CAST(:terminal_result AS JSONB),
                            failure_reason='cancelled', cancel_reason=:reason,
                            completed_at=now(), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": child_id, "reason": child_reason},
                )
                self._release_claims(conn, child_id, status="cancelled")
                self._resolve_cancel(conn, child_id)
                self._append_event(conn, child_id, "cancelled", {"reason": child_reason})
                child = self._task_from_row(self._task_row(conn, child_id))
                self._append_parent_terminal_event(conn, child)
            self._append_event(
                conn,
                child_id,
                "parent_cancelled",
                {"owner_agent_run_id": owner_agent_run_id, "reason": reason},
            )
            self._cancel_child_agent_runs(
                conn,
                child_id,
                reason=reason,
                seen=seen,
            )

    def attach_artifact(self, task_id: str, **kwargs: Any) -> TaskArtifact:
        with self.engine.begin() as conn:
            return self._attach_artifact_with_conn(conn, task_id, **kwargs)

    def create_or_update_pr(self, task_id: str, *, diff: str = "") -> TaskArtifact:
        task = self.get_agent_run(task_id)
        with self.engine.begin() as conn:
            session_row = conn.execute(
                text(
                    """
                    SELECT branch FROM labrastro_agent_run_sessions
                    WHERE task_id=:task_id
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
            pr = self.pr_flow.create_or_update(
                task,
                diff=diff,
                branch_name=(
                    str(session_row["branch"] or "") if session_row is not None else ""
                )
                or _worktree_branch_for_activation(
                    self._load_current_activation_with_conn(conn, task)
                )
                or None,
            )
            return self._attach_artifact_with_conn(
                conn,
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
    ) -> list[Any]:
        from labrastro_server.services.agent_runtime.control_plane import AgentRunEvent

        limit = clamp_event_limit(limit)
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT task_id, seq, type, payload
                    FROM labrastro_agent_run_events
                    WHERE task_id=:task_id AND seq > :after_seq
                    ORDER BY seq ASC
                    LIMIT :limit
                    """
                ),
                {"task_id": task_id, "after_seq": after_seq, "limit": limit},
            ).mappings()
            return [
                AgentRunEvent(
                    task_id=str(row["task_id"]),
                    seq=int(row["seq"]),
                    type=str(row["type"]),
                    payload=_dict_from(row["payload"]),
                )
                for row in rows
            ]

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_agent_run_artifacts
                    WHERE task_id=:task_id
                    ORDER BY artifact_seq ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings()
            return [self._artifact_from_row(row) for row in rows]

    def get_agent_run(self, task_id: str) -> AgentRun:
        with self.engine.begin() as conn:
            return self._task_from_row(self._task_row(conn, task_id))

    def agent_run_to_dict(self, task_id: str) -> dict[str, Any]:
        return _agent_run_to_dict(self.get_agent_run(task_id))

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]:
        return [_artifact_to_dict(artifact) for artifact in self.list_artifacts(task_id)]

    def list_agent_runs(self, **filters: Any) -> list[dict[str, Any]]:
        clauses = ["deleted_at IS NULL" if False else "1=1"]
        params: dict[str, Any] = {"limit": max(1, min(500, int(filters.get("limit") or 50)))}
        for key in ("status", "agent_id"):
            if filters.get(key):
                clauses.append(f"{key} = :{key}")
                params[key] = str(filters[key])
        if filters.get("after_created_at"):
            clauses.append("created_at > CAST(:after_created_at AS TIMESTAMPTZ)")
            params["after_created_at"] = str(filters["after_created_at"])
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM labrastro_agent_runs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings()
            return [_agent_run_to_dict(self._task_from_row(row)) for row in rows]

    def list_descendant_agent_runs(
        self,
        owner_agent_run_id: str,
        *,
        include_terminal: bool = True,
    ) -> list[AgentRun]:
        status_clause = (
            ""
            if include_terminal
            else "WHERE status NOT IN ('completed', 'failed', 'cancelled', 'blocked')"
        )
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    WITH RECURSIVE descendants AS (
                        SELECT runs.*, ARRAY[runs.id] AS path
                        FROM labrastro_agent_run_relations relations
                        JOIN labrastro_agent_runs runs
                          ON runs.id = relations.related_agent_run_id
                        WHERE relations.owner_agent_run_id = :owner_agent_run_id
                          AND relations.status = 'active'
                          AND runs.id != :owner_agent_run_id
                        UNION ALL
                        SELECT child.*, descendants.path || child.id
                        FROM descendants
                        JOIN labrastro_agent_run_relations relations
                          ON relations.owner_agent_run_id = descendants.id
                         AND relations.status = 'active'
                        JOIN labrastro_agent_runs child
                          ON child.id = relations.related_agent_run_id
                        WHERE NOT child.id = ANY(descendants.path)
                    )
                    , deduped AS (
                        SELECT DISTINCT ON (id) *
                        FROM descendants
                        {status_clause}
                        ORDER BY id, array_length(path, 1) ASC, created_at ASC
                    )
                    SELECT *
                    FROM deduped
                    ORDER BY array_length(path, 1) ASC, path ASC, created_at ASC
                    """
                ),
                {"owner_agent_run_id": owner_agent_run_id},
            ).mappings()
            return [self._task_from_row(row) for row in rows]

    def load_agent_run_detail(
        self,
        task_id: str,
        *,
        event_limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> dict[str, Any]:
        from labrastro_server.services.agent_runtime.control_plane import AgentRunEvent

        event_limit = clamp_event_limit(event_limit)
        with self.engine.begin() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            session = conn.execute(
                text("SELECT * FROM labrastro_agent_run_sessions WHERE task_id=:task_id"),
                {"task_id": task_id},
            ).mappings().first()
            claim = conn.execute(
                text(
                    """
                    SELECT request_id, task_id, activation_id, worker_id, peer_id, status,
                           lease_sec, lease_deadline, last_heartbeat_at, claimed_at,
                           released_at, metadata
                    FROM labrastro_agent_run_activation_claims
                    WHERE task_id=:task_id
                    ORDER BY claimed_at DESC
                    LIMIT 1
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
            activation_rows = conn.execute(
                text(
                    """
                    SELECT id, agent_run_id, seq, input_kind, input_payload,
                           prompt, status, output, result_payload, worker_id,
                           request_id, started_at, ended_at, metadata,
                           created_at, updated_at
                    FROM labrastro_agent_run_activations
                    WHERE agent_run_id=:task_id
                    ORDER BY seq ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
            feedback_rows = conn.execute(
                text(
                    """
                    SELECT id, agent_run_id, source, kind, payload, created_at,
                           consumed_by_activation_id, visibility,
                           requires_activation, metadata
                    FROM labrastro_agent_run_feedback
                    WHERE agent_run_id=:task_id
                    ORDER BY created_at ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
            steer_rows = conn.execute(
                text(
                    """
                    SELECT steer.id, steer.activation_id, steer.source, steer.payload,
                           steer.created_at, steer.delivered_at, steer.status,
                           steer.metadata
                    FROM labrastro_agent_run_activation_steers steer
                    JOIN labrastro_agent_run_activations activation
                      ON activation.id = steer.activation_id
                    WHERE activation.agent_run_id=:task_id
                    ORDER BY steer.created_at ASC, steer.id ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
            relation_rows = conn.execute(
                text(
                    """
                    SELECT id, owner_agent_run_id, related_agent_run_id,
                           relation_type, relation_scope, created_by_activation_id,
                           status, payload, metadata
                    FROM labrastro_agent_run_relations
                    WHERE owner_agent_run_id=:task_id
                       OR related_agent_run_id=:task_id
                    ORDER BY id ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
            binding_rows = conn.execute(
                text(
                    """
                    SELECT id, owner_session_run_id, main_agent_run_id, agent_id,
                           target_agent_run_id, thread_key, binding_lifetime,
                           workdir_policy, visibility, status, cleanup_policy,
                           created_at, updated_at, metadata
                    FROM labrastro_agent_thread_bindings
                    WHERE main_agent_run_id=:task_id
                       OR target_agent_run_id=:task_id
                    ORDER BY id ASC
                    """
                ),
                {"task_id": task_id},
            ).mappings().all()
            event_rows = conn.execute(
                text(
                    """
                    SELECT task_id, seq, type, payload
                    FROM (
                        SELECT task_id, seq, type, payload
                        FROM labrastro_agent_run_events
                        WHERE task_id=:task_id
                        ORDER BY seq DESC
                        LIMIT :limit
                    ) limited_events
                    ORDER BY seq ASC
                    """
                ),
                {"task_id": task_id, "limit": event_limit},
            ).mappings().all()
        events = [
            AgentRunEvent(
                task_id=str(row["task_id"]),
                seq=int(row["seq"]),
                type=str(row["type"]),
                payload=_dict_from(row["payload"]),
            ).to_dict()
            for row in event_rows
        ]
        return {
            "agent_run": _agent_run_to_dict(task),
            "artifacts": self.artifacts_to_dict(task_id),
            "session": _jsonable_row(session) if session is not None else None,
            "claim": _jsonable_row(claim) if claim is not None else None,
            "activations": [_jsonable_row(row) for row in activation_rows],
            "activation_steers": [_jsonable_row(row) for row in steer_rows],
            "feedback": [_jsonable_row(row) for row in feedback_rows],
            "relations": [_relation_row_to_dict(row) for row in relation_rows],
            "agent_thread_bindings": [
                _agent_thread_binding_row_to_dict(row) for row in binding_rows
            ],
            "events": events,
        }

    def _resolve_request(self, request: Any) -> Any:
        parent = None
        relation = getattr(request, "relation", None)
        if relation is not None and relation.relation_type in {
            AgentRunRelationType.AGENT_CALL_EPHEMERAL,
            AgentRunRelationType.AGENT_CALL_PERSISTENT,
        }:
            owner_agent_run_id = str(relation.owner_agent_run_id or "").strip()
            if owner_agent_run_id:
                parent = self.get_agent_run(owner_agent_run_id)
        if parent is not None:
            if request.runtime_profile_id is None:
                request.runtime_profile_id = parent.runtime_profile_id
            if request.executor is None:
                request.executor = parent.executor
            if request.execution_location is None:
                request.execution_location = parent.execution_location
            if request.workdir is None:
                request.workdir = parent.workdir
        agents = _dict_from(self.runtime_snapshot.get("agents"))
        profiles = _dict_from(self.runtime_snapshot.get("runtime_profiles"))
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
        self._validate_runtime_policy(request, agent_config=agent_config)
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
        request: Any,
        *,
        agent_config: AgentConfig | None,
    ) -> None:
        validate_agent_run_runtime_policy(request, agent_config=agent_config)

    def _append_parent_terminal_event(
        self,
        conn: Any,
        task: AgentRun,
    ) -> None:
        from labrastro_server.services.agent_runtime.control_plane import (
            _agent_call_feedback_kind,
            _agent_call_feedback_payload,
        )

        owner_rows = conn.execute(
            text(
                """
                SELECT id, owner_agent_run_id, relation_type, payload, metadata
                FROM labrastro_agent_run_relations
                WHERE related_agent_run_id=:task_id
                  AND owner_agent_run_id != :task_id
                  AND status='active'
                ORDER BY id ASC
                """
            ),
            {"task_id": task.id},
        ).mappings().all()
        activation = self._load_current_activation_with_conn(conn, task)
        task_prompt = activation.prompt if activation is not None else ""
        for row in owner_rows:
            owner_agent_run_id = str(row["owner_agent_run_id"])
            relation_type = str(row["relation_type"])
            relation_payload = _dict_from(row["payload"])
            relation = AgentRunRelation(
                id=str(row["id"] or ""),
                owner_agent_run_id=owner_agent_run_id,
                related_agent_run_id=task.id,
                relation_type=relation_type,
                payload=relation_payload,
                metadata=_dict_from(row["metadata"]),
            )
            if relation.relation_type in {
                AgentRunRelationType.AGENT_CALL_EPHEMERAL,
                AgentRunRelationType.AGENT_CALL_PERSISTENT,
            }:
                self._append_agent_call_feedback(conn, task, relation)
                self._append_event(
                    conn,
                    owner_agent_run_id,
                    _agent_call_feedback_kind(task).value,
                    _agent_call_feedback_payload(task, relation=relation),
                )
                if (
                    relation.relation_type == AgentRunRelationType.AGENT_CALL_EPHEMERAL
                    or relation.payload.get("lifecycle_hook_id")
                ):
                    for event_type, payload in agent_relation_terminal_lifecycle_events(
                        task,
                        owner_agent_run_id=owner_agent_run_id,
                        relation_metadata=relation_payload,
                        task_prompt=task_prompt,
                    ):
                        self._append_event(conn, owner_agent_run_id, event_type, payload)
                continue
            self._append_event(
                conn,
                owner_agent_run_id,
                "agent_relation_completed",
                _agent_relation_completed_payload(
                    task,
                    owner_agent_run_id=owner_agent_run_id,
                    task_prompt=task_prompt,
                ),
            )
            for event_type, payload in agent_relation_terminal_lifecycle_events(
                task,
                owner_agent_run_id=owner_agent_run_id,
                relation_metadata=relation_payload,
                task_prompt=task_prompt,
            ):
                self._append_event(conn, owner_agent_run_id, event_type, payload)

    def _resume_from_pending_agent_call_feedback(self, conn: Any, owner_run_id: str) -> bool:
        owner = self._task_from_row(self._task_row(conn, owner_run_id))
        activation = self._load_current_activation_with_conn(conn, owner)
        row = conn.execute(
            text(
                """
                SELECT id, agent_run_id, source, kind, payload, created_at,
                       consumed_by_activation_id, visibility,
                       requires_activation, metadata
                FROM labrastro_agent_run_feedback
                WHERE agent_run_id=:task_id
                  AND requires_activation IS TRUE
                  AND consumed_by_activation_id IS NULL
                  AND kind IN ('agent_call_result', 'agent_call_failed')
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ),
            {"task_id": owner_run_id},
        ).mappings().first()
        if row is None:
            return False
        from labrastro_server.services.agent_runtime.control_plane import (
            _activation_can_resume_from_feedback,
            _activation_id_for_task,
            _activation_to_dict,
            _agent_call_feedback_prompt,
            _validate_activation_input_payload,
        )

        if not (
            owner.status == AgentRunStatus.WAITING
            and owner.waiting_reason == AgentRunWaitingReason.AGENT_CALL
            and _activation_can_resume_from_feedback(activation)
        ):
            return False
        feedback = _feedback_from_row(row)
        payload = dict(feedback.payload)
        previous_seq = (
            activation.seq
            if activation is not None
            else max(1, self._next_activation_seq_with_conn(conn, owner.id) - 1)
        )
        previous_activation_id = str(
            activation.id
            if activation is not None
            else owner.current_activation_id
            or _activation_id_for_task(owner.id, previous_seq)
        )
        activation_payload = {
            "feedback_id": feedback.id,
            "kind": feedback.kind.value,
            "target_agent_run_id": str(
                payload.get("target_agent_run_id")
                or feedback.metadata.get("target_agent_run_id")
                or ""
            ),
            "resume_session": True,
            "retry_of_activation_id": previous_activation_id,
        }
        _validate_activation_input_payload(activation_payload)
        next_seq = self._next_activation_seq_with_conn(conn, owner.id)
        next_activation = AgentRunActivation(
            id=_activation_id_for_task(owner.id, next_seq),
            agent_run_id=owner.id,
            seq=next_seq,
            input_kind=AgentRunActivationInputKind.AGENT_FEEDBACK,
            input_payload=activation_payload,
            prompt=_agent_call_feedback_prompt(payload),
            status=AgentRunActivationStatus.QUEUED,
            metadata={
                "executor_session_id": owner.executor_session_id,
                "runtime_profile_id": owner.runtime_profile_id,
            },
        )
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_runs
                SET status='queued',
                    waiting_reason=NULL,
                    resume_policy=NULL,
                    terminal_result='{}'::jsonb,
                    failure_reason=NULL,
                    cancel_reason=NULL,
                    current_activation_id=:activation_id,
                    completed_at=NULL,
                    updated_at=now()
                WHERE id=:task_id
                """
            ),
            {"task_id": owner.id, "activation_id": next_activation.id},
        )
        self._upsert_activation_with_conn(conn, next_activation)
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_run_feedback
                SET consumed_by_activation_id=:activation_id
                WHERE id=:feedback_id AND agent_run_id=:task_id
                """
            ),
            {
                "activation_id": next_activation.id,
                "feedback_id": feedback.id,
                "task_id": owner.id,
            },
        )
        feedback.consumed_by_activation_id = next_activation.id
        self._append_event(
            conn,
            owner.id,
            "agent_run_feedback_consumed",
            {
                "feedback_id": feedback.id,
                "activation_id": next_activation.id,
                "feedback": _feedback_to_dict(feedback),
            },
        )
        self._append_event(
            conn,
            owner.id,
            "activation_queued",
            {
                "activation_id": next_activation.id,
                "activation": _activation_to_dict(next_activation),
                "retry_of_activation_id": previous_activation_id,
                "feedback_id": feedback.id,
            },
        )
        return True

    def _append_agent_call_feedback(
        self,
        conn: Any,
        task: AgentRun,
        relation: AgentRunRelation,
    ) -> None:
        from labrastro_server.services.agent_runtime.control_plane import (
            _agent_call_feedback_kind,
            _agent_call_feedback_payload,
        )

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
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_feedback (
                    id, agent_run_id, source, kind, payload,
                    created_at, consumed_by_activation_id, visibility,
                    requires_activation, metadata
                ) VALUES (
                    :id, :agent_run_id, :source, :kind,
                    CAST(:payload AS JSONB), CAST(:created_at AS TIMESTAMPTZ),
                    :consumed_by_activation_id, :visibility,
                    :requires_activation, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": feedback.id,
                "agent_run_id": feedback.agent_run_id,
                "source": feedback.source.value,
                "kind": feedback.kind.value,
                "payload": _json(feedback.payload),
                "created_at": feedback.created_at,
                "consumed_by_activation_id": feedback.consumed_by_activation_id,
                "visibility": feedback.visibility.value,
                "requires_activation": feedback.requires_activation,
                "metadata": _json(feedback.metadata),
            },
        )
        self._append_event(
            conn,
            owner_run_id,
            "agent_run_feedback_added",
            {
                "feedback_id": feedback.id,
                "feedback": _feedback_to_dict(feedback),
            },
        )
        if wait:
            self._resume_from_pending_agent_call_feedback(conn, owner_run_id)

    def _append_event(
        self, conn: Any, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        seq = conn.execute(
            text(
                """
                UPDATE labrastro_agent_runs
                SET next_event_seq=next_event_seq + 1, updated_at=now()
                WHERE id=:task_id
                RETURNING next_event_seq - 1 AS seq
                """
            ),
            {"task_id": task_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_events(task_id, seq, type, payload)
                VALUES (:task_id, :seq, :type, CAST(:payload AS JSONB))
                """
            ),
            {
                "task_id": task_id,
                "seq": int(seq),
                "type": event_type,
                "payload": _json(payload),
            },
        )

    def _task_row(self, conn: Any, task_id: str) -> Any:
        row = conn.execute(
            text("SELECT * FROM labrastro_agent_runs WHERE id=:task_id"),
            {"task_id": task_id},
        ).mappings().first()
        if row is None:
            raise KeyError(f"AgentRun not found: {task_id}")
        return row

    def _task_from_row(self, row: Any) -> AgentRun:
        row_mapping = getattr(row, "_mapping", row)
        metadata = _dict_from(row["metadata"])
        return AgentRun(
            id=str(row["id"]),
            agent_id=str(row["agent_id"]),
            kind=str(row["kind"] or "agent_run") if "kind" in row_mapping else "agent_run",
            owner_session_run_id=(
                str(row["owner_session_run_id"] or "")
                if "owner_session_run_id" in row_mapping
                else ""
            ),
            source=AgentRunSource(
                str(
                    row["source"]
                    if "source" in row_mapping and row["source"] is not None
                    else "manual"
                )
            ),
            trigger_mode=TriggerMode(str(row["trigger_mode"])),
            status=AgentRunStatus(str(row["status"])),
            waiting_reason=(
                row["waiting_reason"]
                if "waiting_reason" in row_mapping
                else None
            ),
            resume_policy=(
                row["resume_policy"]
                if "resume_policy" in row_mapping
                else None
            ),
            runtime_profile_id=row["runtime_profile_id"],
            executor=_optional_executor(row["executor"]),
            execution_location=_optional_location(row["execution_location"]),
            worktree_role=optional_worktree_role(
                row["worktree_role"]
                if "worktree_role" in row_mapping and row["worktree_role"] is not None
                else metadata.get("worktree_role")
            ),
            publish_policy=optional_publish_policy(
                row["publish_policy"]
                if "publish_policy" in row_mapping and row["publish_policy"] is not None
                else metadata.get("publish_policy")
            ),
            terminal_result=_dict_from(row["terminal_result"]),
            executor_session_id=row["executor_session_id"],
            current_activation_id=row["current_activation_id"],
            workdir=row["workdir"],
            sandbox_id=(
                row["sandbox_id"]
                if "sandbox_id" in row_mapping and row["sandbox_id"] is not None
                else metadata.get("sandbox_id")
            ),
            sandbox_session_id=(
                row["sandbox_session_id"]
                if "sandbox_session_id" in row_mapping and row["sandbox_session_id"] is not None
                else metadata.get("sandbox_session_id")
            ),
            workspace_ref=(
                row["workspace_ref"]
                if "workspace_ref" in row_mapping and row["workspace_ref"] is not None
                else metadata.get("workspace_ref")
            ),
            retention_scope=(
                str(row["retention_scope"] or "session")
                if "retention_scope" in row_mapping
                else "session"
            ),
            cleanup_policy=(
                str(row["cleanup_policy"] or "delete_with_owner_session")
                if "cleanup_policy" in row_mapping
                else "delete_with_owner_session"
            ),
            failure_reason=row["failure_reason"],
            cancel_reason=row["cancel_reason"],
            metadata=metadata,
        )

    def _artifact_from_row(self, row: Any) -> TaskArtifact:
        return TaskArtifact(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            type=ArtifactType(str(row["type"])),
            status=ArtifactStatus(str(row["status"])),
            branch_name=row["branch_name"],
            pr_url=row["pr_url"],
            content=row["content"],
            path=row["path"],
            metadata=_dict_from(row["metadata"]),
            merge_status=MergeStatus(str(row["merge_status"]))
            if row["merge_status"]
            else None,
            merged_by=row["merged_by"],
        )

    def _worker_matches_task(
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

    def _agent_concurrency_allows(self, conn: Any, task: AgentRun) -> bool:
        raw_agent = _dict_from(_dict_from(self.runtime_snapshot.get("agents")).get(task.agent_id))
        raw_limit = raw_agent.get("max_concurrent_tasks")
        if raw_limit is None:
            return True
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return True
        if limit < 1:
            return False
        count = conn.execute(
            text(
                """
                SELECT count(*) FROM labrastro_agent_runs
                WHERE agent_id=:agent_id
                  AND status IN ('dispatched', 'running', 'waiting')
                """
            ),
            {"agent_id": task.agent_id},
        ).scalar_one()
        return int(count) < limit

    def _executor_metadata(self, task: AgentRun) -> dict[str, Any]:
        metadata = dict(task.metadata)
        if task.worktree_role is not None:
            metadata.setdefault("worktree_role", task.worktree_role.value)
        if task.publish_policy is not None:
            metadata.setdefault("publish_policy", task.publish_policy.value)
        rendered = self._render_prompt_for_task(
            task, task.executor or ExecutorType.REULEAUXCODER
        )
        if rendered is not None:
            metadata.setdefault("prompt_files", rendered.files)
            metadata.setdefault("prompt_metadata", rendered.metadata)
            if rendered.metadata.get("system_prompt"):
                metadata.setdefault("system_prompt", rendered.metadata["system_prompt"])
        raw_agent = _dict_from(_dict_from(self.runtime_snapshot.get("agents")).get(task.agent_id))
        resolved = _dict_from(raw_agent.get("resolved_capabilities"))
        effective = _dict_from(raw_agent.get("effective_capabilities"))
        overlay = _dict_from(resolved.get("capability_overlay"))
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
                "agent_id": task.agent_id,
                "source": task.source.value,
                "interactive": task.source.value == "chat",
                "runtime_profile_id": task.runtime_profile_id or str(raw_agent.get("runtime_profile") or ""),
                "effective_capabilities": effective,
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

    def _render_prompt_for_task(self, task: AgentRun, executor: ExecutorType) -> Any:
        agents = _dict_from(self.runtime_snapshot.get("agents"))
        profiles = _dict_from(self.runtime_snapshot.get("runtime_profiles"))
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
        servers: list[str] = []
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

    def _active_claim(
        self,
        conn: Any,
        request_id: str,
        *,
        for_update: bool = False,
    ) -> Any | None:
        lock_clause = " FOR UPDATE" if for_update else ""
        return conn.execute(
            text(
                f"""
                SELECT * FROM labrastro_agent_run_activation_claims
                WHERE request_id=:request_id AND status='active'
                {lock_clause}
                """
            ),
            {"request_id": request_id},
        ).mappings().first()

    def _claim_owner_ok(
        self,
        row: Any,
        task_id: str,
        worker_id: str,
        peer_id: str | None,
        *,
        activation_id: str | None = None,
    ) -> tuple[bool, str]:
        if str(row["task_id"]) != task_id:
            return False, "task_mismatch"
        if str(row["worker_id"]) != worker_id:
            return False, "worker_mismatch"
        expected_activation = str(row.get("activation_id") or "")
        if activation_id and expected_activation != activation_id:
            return False, "activation_mismatch"
        expected_peer = str(row["peer_id"] or "")
        if peer_id and expected_peer and expected_peer != peer_id:
            return False, "peer_mismatch"
        return True, ""

    def _cancel_reason(self, conn: Any, task_id: str) -> str:
        reason = conn.execute(
            text(
                """
                SELECT reason FROM labrastro_agent_run_cancel_requests
                WHERE task_id=:task_id AND resolved_at IS NULL
                """
            ),
            {"task_id": task_id},
        ).scalar()
        return str(reason or "")

    def _recover_stale_with_conn(self, conn: Any, *, now: float | None = None) -> list[str]:
        params: dict[str, Any] = {}
        deadline_expr = "now()"
        if now is not None:
            deadline_expr = "CAST(:current_time AS TIMESTAMPTZ)"
            params["current_time"] = datetime.fromtimestamp(now, tz=timezone.utc)
        rows = conn.execute(
            text(
                f"""
                SELECT * FROM labrastro_agent_run_activation_claims
                WHERE status='active' AND lease_deadline <= {deadline_expr}
                FOR UPDATE
                """
            ),
            params,
        ).mappings().all()
        recovered: list[str] = []
        for row in rows:
            task_id = str(row["task_id"])
            task = self._task_from_row(self._task_row(conn, task_id))
            if task.status in {
                AgentRunStatus.DISPATCHED,
                AgentRunStatus.RUNNING,
                AgentRunStatus.WAITING,
            }:
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='queued', updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id},
                )
                recovered.append(task_id)
                task.status = AgentRunStatus.QUEUED
                from labrastro_server.services.agent_runtime.control_plane import (
                    _activation_from_task,
                    _activation_with_runtime_state,
                )

                activation_id = str(row.get("activation_id") or task.current_activation_id or "")
                if activation_id:
                    task.current_activation_id = activation_id
                activation = _activation_with_runtime_state(
                    self._load_current_activation_with_conn(conn, task)
                    or _activation_from_task(task, activation_id=activation_id),
                    status=AgentRunActivationStatus.QUEUED,
                )
                self._upsert_activation_with_conn(conn, activation)
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_run_activation_steers
                        SET status='queued',
                            metadata=metadata
                                - 'delivering_request_id'
                                - 'delivering_worker_id'
                        WHERE activation_id=:activation_id
                          AND status='delivering'
                          AND metadata->>'delivering_request_id'=:request_id
                        """
                    ),
                    {
                        "activation_id": activation.id,
                        "request_id": str(row["request_id"]),
                    },
                )
                self._append_event(
                    conn,
                    task_id,
                    "lease_expired",
                    {
                        "activation_id": activation.id,
                        "request_id": row["request_id"],
                        "worker_id": row["worker_id"],
                        "peer_id": row["peer_id"],
                    },
                )
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_run_activation_claims
                    SET status='expired', released_at=now()
                    WHERE request_id=:request_id
                    """
                ),
                {"request_id": row["request_id"]},
            )
        return recovered

    def _pin_session_with_conn(
        self,
        conn: Any,
        task: AgentRun,
        session: TaskSessionRef,
        *,
        metadata: dict[str, Any],
    ) -> None:
        session_metadata = self._session_metadata(task, metadata)
        session_metadata.update(session.metadata)
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_runs
                SET status=CASE WHEN status='dispatched' THEN 'running' ELSE status END,
                    executor_session_id=COALESCE(:executor_session_id, executor_session_id),
                    workdir=COALESCE(:workdir, workdir),
                    started_at=COALESCE(started_at, now()),
                    updated_at=now()
                WHERE id=:task_id
                """
            ),
            {
                "task_id": task.id,
                "executor_session_id": session.executor_session_id,
                "workdir": session.workdir,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_sessions (
                    task_id, agent_id, executor, execution_location,
                    workdir, branch, executor_session_id, metadata
                ) VALUES (
                    :task_id, :agent_id, :executor, :execution_location,
                    :workdir, :branch, :executor_session_id, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    workdir=COALESCE(EXCLUDED.workdir, labrastro_agent_run_sessions.workdir),
                    branch=COALESCE(EXCLUDED.branch, labrastro_agent_run_sessions.branch),
                    executor_session_id=COALESCE(
                        EXCLUDED.executor_session_id,
                        labrastro_agent_run_sessions.executor_session_id
                    ),
                    metadata=labrastro_agent_run_sessions.metadata || EXCLUDED.metadata,
                    updated_at=now()
                """
            ),
            {
                "task_id": task.id,
                "agent_id": task.agent_id,
                "executor": (task.executor or ExecutorType.REULEAUXCODER).value,
                "execution_location": (
                    task.execution_location or ExecutionLocation.REMOTE_SERVER
                ).value,
                "workdir": session.workdir,
                "branch": session.branch,
                "executor_session_id": session.executor_session_id,
                "metadata": _json(session_metadata),
            },
        )
        self._append_event(
            conn,
            task.id,
            "session_pinned",
            {
                "executor_session_id": session.executor_session_id,
                "workdir": session.workdir,
                "branch": session.branch,
            },
        )

    def _upsert_session_with_conn(
        self,
        conn: Any,
        task: AgentRun,
        *,
        executor_session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session_metadata = self._session_metadata(task, metadata)
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_sessions (
                    task_id, agent_id, executor, execution_location,
                    workdir, branch, executor_session_id, metadata
                ) VALUES (
                    :task_id, :agent_id, :executor, :execution_location,
                    :workdir, :branch, :executor_session_id, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    workdir=COALESCE(EXCLUDED.workdir, labrastro_agent_run_sessions.workdir),
                    branch=COALESCE(EXCLUDED.branch, labrastro_agent_run_sessions.branch),
                    executor_session_id=EXCLUDED.executor_session_id,
                    metadata=labrastro_agent_run_sessions.metadata || EXCLUDED.metadata,
                    updated_at=now()
                """
            ),
            {
                "task_id": task.id,
                "agent_id": task.agent_id,
                "executor": (task.executor or ExecutorType.REULEAUXCODER).value,
                "execution_location": (
                    task.execution_location or ExecutionLocation.REMOTE_SERVER
                ).value,
                "workdir": task.workdir,
                "branch": session_metadata.get("branch"),
                "executor_session_id": executor_session_id,
                "metadata": _json(session_metadata),
            },
        )

    def _attach_artifact_with_conn(self, conn: Any, task_id: str, **kwargs: Any) -> TaskArtifact:
        artifact = TaskArtifact(
            id=str(kwargs.get("artifact_id") or _new_id("artifact")),
            task_id=task_id,
            type=ArtifactType(str(kwargs.get("type"))),
            status=ArtifactStatus(str(kwargs.get("status") or "generated")),
            branch_name=kwargs.get("branch_name"),
            pr_url=kwargs.get("pr_url"),
            content=kwargs.get("content"),
            path=kwargs.get("path"),
            metadata=dict(kwargs.get("metadata") or {}),
        )
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_artifacts (
                    id, task_id, type, status, branch_name, pr_url, content,
                    path, metadata, merge_status, merged_by
                ) VALUES (
                    :id, :task_id, :type, :status, :branch_name, :pr_url, :content,
                    :path, CAST(:metadata AS JSONB), :merge_status, :merged_by
                )
                """
            ),
            {
                **_artifact_to_dict(artifact),
                "type": artifact.type.value,
                "status": artifact.status.value,
                "merge_status": artifact.merge_status.value
                if artifact.merge_status
                else None,
                "metadata": _json(artifact.metadata),
            },
        )
        updates: dict[str, Any] = {"task_id": task_id}
        set_parts = ["updated_at=now()"]
        if artifact.branch_name:
            set_parts.append("branch_name=:branch_name")
            updates["branch_name"] = artifact.branch_name
        if artifact.pr_url:
            set_parts.append("pr_url=:pr_url")
            updates["pr_url"] = artifact.pr_url
        if artifact.type == ArtifactType.PULL_REQUEST:
            set_parts.append("issue_status='in_review'")
        conn.execute(
            text(f"UPDATE labrastro_agent_runs SET {', '.join(set_parts)} WHERE id=:task_id"),
            updates,
        )
        self._append_event(
            conn,
            task_id,
            "artifact_attached",
            artifact_attached_event_payload(artifact),
        )
        return artifact

    def _has_open_pr(self, conn: Any, task_id: str) -> bool:
        count = conn.execute(
            text(
                """
                SELECT count(*) FROM labrastro_agent_run_artifacts
                WHERE task_id=:task_id AND type='pull_request'
                  AND status NOT IN ('merged', 'closed')
                """
            ),
            {"task_id": task_id},
        ).scalar_one()
        return int(count) > 0

    def _release_claims(self, conn: Any, task_id: str, *, status: str) -> None:
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_run_activation_claims
                SET status=:status, released_at=now()
                WHERE task_id=:task_id AND status='active'
                """
            ),
            {"task_id": task_id, "status": status},
        )

    def _resolve_cancel(self, conn: Any, task_id: str) -> None:
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_run_cancel_requests
                SET resolved_at=now()
                WHERE task_id=:task_id AND resolved_at IS NULL
                """
            ),
            {"task_id": task_id},
        )
