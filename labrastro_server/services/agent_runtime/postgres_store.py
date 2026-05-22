"""Postgres-backed AgentRun control-plane store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import json

from reuleauxcoder.domain.agent_runtime.models import (
    AgentRunSource,
    ArtifactStatus,
    ArtifactType,
    ExecutionLocation,
    ExecutorType,
    MergeStatus,
    TaskArtifact,
    AgentRunRecord,
    TaskSessionRef,
    TaskStatus,
    TriggerMode,
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
from labrastro_server.services.agent_runtime.lifecycle import IssueStatus
from labrastro_server.services.agent_runtime.prompt_renderer import (
    CanonicalAgentContext,
    ExecutorPromptRenderer,
)
from labrastro_server.services.agent_runtime.runtime_store import (
    DEFAULT_RUNTIME_EVENT_LIMIT,
    clamp_event_limit,
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


def _dict_from(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _workspace_key(value: str | None) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").lower()


def _can_resume_from_parent(request: Any, parent: AgentRunRecord) -> bool:
    if not parent.executor_session_id:
        return False
    return (
        request.agent_id == parent.agent_id
        and request.runtime_profile_id == parent.runtime_profile_id
        and request.executor == parent.executor
        and request.execution_location == parent.execution_location
        and _workspace_key(request.workdir) == _workspace_key(parent.workdir)
        and str(request.branch_name or "") == str(parent.branch_name or "")
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


def _jsonable_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def _agent_run_to_dict(task: AgentRunRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "agent_run_id": task.id,
        "issue_id": task.issue_id,
        "agent_id": task.agent_id,
        "source": task.source.value,
        "trigger_mode": task.trigger_mode.value,
        "status": task.status.value,
        "prompt": task.prompt,
        "runtime_profile_id": task.runtime_profile_id,
        "executor": task.executor.value if task.executor else None,
        "execution_location": (
            task.execution_location.value if task.execution_location else None
        ),
        "output": task.output,
        "parent_task_id": task.parent_task_id,
        "trigger_comment_id": task.trigger_comment_id,
        "branch_name": task.branch_name,
        "pr_url": task.pr_url,
        "worker_id": task.worker_id,
        "executor_session_id": task.executor_session_id,
        "workdir": task.workdir,
        "sandbox_id": task.sandbox_id,
        "sandbox_session_id": task.sandbox_session_id,
        "workspace_ref": task.workspace_ref,
        "delegated_by_run_id": task.delegated_by_run_id,
        "parent_run_id": task.parent_run_id,
        "metadata": dict(task.metadata),
    }


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

    def submit_agent_run(self, request: Any, *, task_id: str | None = None) -> AgentRunRecord:
        request = self._resolve_request(request)
        metadata = dict(request.metadata)
        metadata.setdefault("agent_run_source", request.source.value)
        if request.sandbox_id:
            metadata.setdefault("sandbox_id", request.sandbox_id)
        if request.sandbox_session_id:
            metadata.setdefault("sandbox_session_id", request.sandbox_session_id)
        if request.workspace_ref:
            metadata.setdefault("workspace_ref", request.workspace_ref)
        if request.delegated_by_run_id:
            metadata.setdefault("delegated_by_run_id", request.delegated_by_run_id)
        if request.parent_run_id:
            metadata.setdefault("parent_run_id", request.parent_run_id)
        if request.model is not None:
            metadata.setdefault("model", request.model)
        task = AgentRunRecord(
            id=task_id or _new_id("task"),
            issue_id=request.issue_id,
            agent_id=request.agent_id,
            source=request.source,
            trigger_mode=request.trigger_mode,
            status=TaskStatus.QUEUED,
            prompt=request.prompt,
            runtime_profile_id=request.runtime_profile_id,
            executor=request.executor,
            execution_location=request.execution_location,
            parent_task_id=request.parent_task_id,
            trigger_comment_id=request.trigger_comment_id,
            branch_name=request.branch_name,
            pr_url=request.pr_url,
            executor_session_id=request.executor_session_id,
            workdir=request.workdir,
            sandbox_id=request.sandbox_id,
            sandbox_session_id=request.sandbox_session_id,
            workspace_ref=request.workspace_ref,
            delegated_by_run_id=request.delegated_by_run_id,
            parent_run_id=request.parent_run_id or request.parent_task_id,
            metadata=metadata,
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_agent_runs (
                        id, issue_id, agent_id, trigger_mode, status, prompt,
                        runtime_profile_id, executor, execution_location,
                        parent_task_id, trigger_comment_id, branch_name, pr_url,
                        executor_session_id, workdir, metadata, runtime_snapshot
                    ) VALUES (
                        :id, :issue_id, :agent_id, :trigger_mode, :status, :prompt,
                        :runtime_profile_id, :executor, :execution_location,
                        :parent_task_id, :trigger_comment_id, :branch_name, :pr_url,
                        :executor_session_id, :workdir, CAST(:metadata AS JSONB),
                        CAST(:runtime_snapshot AS JSONB)
                    )
                    """
                ),
                {
                    **_agent_run_to_dict(task),
                    "trigger_mode": task.trigger_mode.value,
                    "status": task.status.value,
                    "executor": task.executor.value if task.executor else None,
                    "execution_location": (
                        task.execution_location.value if task.execution_location else None
                    ),
                    "metadata": _json(metadata),
                    "runtime_snapshot": _json(self.runtime_snapshot),
                },
            )
            self._append_event(conn, task.id, "queued", {"agent_run": _agent_run_to_dict(task)})
        return task

    def claim_agent_run(
        self,
        *,
        worker_id: str,
        executors: list[Any] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> Any | None:
        from labrastro_server.services.agent_runtime.control_plane import AgentRunClaim

        allowed = {_coerce_executor(executor) for executor in executors or []}
        features = (
            {str(feature) for feature in peer_features}
            if peer_features is not None
            else None
        )
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT name FROM labrastro_agent_run_locks WHERE name='global_claim' FOR UPDATE")
            ).first()
            self._recover_stale_with_conn(conn)
            running = conn.execute(
                text(
                    """
                    SELECT count(*) FROM labrastro_agent_runs
                    WHERE status IN ('dispatched', 'running', 'waiting_approval')
                    """
                )
            ).scalar_one()
            if int(running) >= self.max_running_tasks:
                return None
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
                    task, features=features, workspace_root=workspace_root
                ):
                    continue
                if not self._agent_concurrency_allows(conn, task):
                    continue
                request_id = _new_id("claim")
                now = datetime.now(timezone.utc)
                effective_lease = max(1, int(lease_sec or 15))
                metadata = self._executor_metadata(task)
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='dispatched', worker_id=:worker_id,
                            dispatched_at=COALESCE(dispatched_at, now()),
                            updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task.id, "worker_id": worker_id},
                )
                task.status = TaskStatus.DISPATCHED
                task.worker_id = worker_id
                conn.execute(
                    text(
                        """
                        INSERT INTO labrastro_agent_run_claims (
                            request_id, task_id, worker_id, peer_id, status,
                            lease_sec, lease_deadline, last_heartbeat_at,
                            runtime_snapshot, metadata
                        ) VALUES (
                            :request_id, :task_id, :worker_id, :peer_id, 'active',
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
                        "worker_id": worker_id,
                        "peer_id": peer_id or "",
                        "lease_sec": effective_lease,
                        "last_heartbeat_at": now,
                        "runtime_snapshot": _json(self.runtime_snapshot),
                        "metadata": _json({}),
                    },
                )
                self._append_event(
                    conn,
                    task.id,
                    "claimed",
                    {
                        "worker_id": worker_id,
                        "peer_id": peer_id,
                        "request_id": request_id,
                        "lease_sec": effective_lease,
                    },
                )
                return AgentRunClaim(
                    request_id=request_id,
                    worker_id=worker_id,
                    task=task,
                    executor_request=ExecutorRunRequest(
                        task_id=task.id,
                        agent_id=task.agent_id,
                        executor=task.executor or ExecutorType.REULEAUXCODER,
                        prompt=task.prompt,
                        execution_location=(
                            task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                        ),
                        issue_id=task.issue_id,
                        runtime_profile_id=task.runtime_profile_id,
                        workdir=task.workdir,
                        branch=task.branch_name,
                        model=str(task.metadata.get("model"))
                        if task.metadata.get("model") is not None
                        else None,
                        executor_session_id=task.executor_session_id,
                        metadata=metadata,
                    ),
                    runtime_snapshot=dict(self.runtime_snapshot),
                )
        return None

    def heartbeat_agent_run(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
        lease_sec: int | None = None,
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
            ok, reason = self._claim_owner_ok(row, task_id, worker_id, peer_id)
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
                    UPDATE labrastro_agent_run_claims
                    SET last_heartbeat_at=now(),
                        lease_deadline=now() + (:lease_sec * interval '1 second'),
                        lease_sec=:lease_sec
                    WHERE request_id=:request_id
                    """
                ),
                {"request_id": request_id, "lease_sec": effective_lease},
            )
            task = self.get_agent_run(task_id)
            if task.status == TaskStatus.DISPATCHED:
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
                self._append_event(conn, task_id, "status", {"status": "running"})
            cancel_reason = self._cancel_reason(conn, task_id)
            return {
                "ok": True,
                "cancel_requested": bool(cancel_reason),
                "reason": cancel_reason or "",
                "lease_sec": effective_lease,
            }

    def validate_claim_owner(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        with self.engine.begin() as conn:
            row = self._active_claim(conn, request_id)
            if row is None:
                return False, "claim_not_found"
            return self._claim_owner_ok(row, task_id, worker_id, peer_id)

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
                    WHERE status IN ('dispatched', 'running', 'waiting_approval')
                    FOR UPDATE
                    """
                )
            ).mappings().all()
            for row in rows:
                task_id = str(row["id"])
                recovered.append(task_id)
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='failed', failure_reason='host_restarted',
                            output=COALESCE(output, 'host restarted while task was in flight'),
                            completed_at=now(), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id},
                )
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_run_claims
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
            self._pin_session_with_conn(conn, task, session, metadata={})

    def pin_claimed_session(
        self,
        *,
        request_id: str,
        task_id: str,
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
            ok, reason = self._claim_owner_ok(row, task_id, worker_id, peer_id)
            if not ok:
                return False, reason
            task = self._task_from_row(self._task_row(conn, task_id))
            session = TaskSessionRef(
                agent_id=task.agent_id,
                executor=task.executor or ExecutorType.REULEAUXCODER,
                execution_location=(
                    task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                ),
                issue_id=task.issue_id,
                task_id=task_id,
                workdir=workdir if workdir else None,
                branch=branch if branch else None,
                executor_session_id=executor_session_id if executor_session_id else None,
            )
            self._pin_session_with_conn(conn, task, session, metadata=metadata or {})
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
        worker_id: str | None = None,
        peer_id: str | None = None,
    ) -> tuple[bool, str]:
        with self.engine.begin() as conn:
            if request_id or worker_id or peer_id:
                row = self._active_claim(conn, request_id or "")
                if row is None:
                    return False, "claim_not_found"
                ok, reason = self._claim_owner_ok(
                    row, task_id, worker_id or "", peer_id
                )
                if not ok:
                    return False, reason
            task = self._task_from_row(self._task_row(conn, task_id))
            self._append_event(conn, task_id, event.type.value, event.to_dict())
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
                        SET status='blocked', metadata=CAST(:metadata AS JSONB), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {
                        "task_id": task_id,
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
                mapped = {
                    "waiting_approval": "waiting_approval",
                    "running": "running",
                    "blocked": "blocked",
                }.get(status)
                if mapped:
                    conn.execute(
                        text(
                            """
                            UPDATE labrastro_agent_runs
                            SET status=:status, updated_at=now()
                            WHERE id=:task_id
                            """
                        ),
                        {"task_id": task_id, "status": mapped},
                    )
            return True, ""

    def complete_claimed_agent_run(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str, AgentRunRecord | None]:
        with self.engine.begin() as conn:
            row = self._active_claim(conn, request_id)
            if row is None:
                return False, "claim_not_found", None
            ok, reason = self._claim_owner_ok(row, task_id, worker_id, peer_id)
            if not ok:
                return False, reason, None
        return True, "", self.complete_agent_run(task_id, result, artifacts=artifacts)

    def complete_agent_run(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentRunRecord:
        with self.engine.begin() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
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
                status = "completed"
                issue_status = "in_review" if self._has_open_pr(conn, task_id) else "done"
                output = result.output
                failure_reason = None
            elif policy_error:
                status = "blocked"
                issue_status = "blocked"
                output = policy_error
                failure_reason = "blocked"
            elif result.status == "cancelled":
                status = "cancelled"
                issue_status = "blocked"
                output = result.output
                failure_reason = "cancelled"
            elif result.status == "blocked":
                status = "blocked"
                issue_status = "blocked"
                output = result.output or result.error
                failure_reason = "blocked"
            else:
                status = "failed"
                issue_status = "blocked"
                output = result.output
                failure_reason = result.error or "agent_error"
            metadata = dict(task.metadata)
            if policy_error:
                metadata["environment_policy_violation"] = policy_error
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status=:status, output=:output,
                        executor_session_id=COALESCE(:executor_session_id, executor_session_id),
                        issue_status=:issue_status,
                        failure_reason=COALESCE(:failure_reason, failure_reason),
                        metadata=CAST(:metadata AS JSONB),
                        completed_at=now(), updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "status": status,
                    "output": output,
                    "executor_session_id": result.executor_session_id,
                    "issue_status": issue_status,
                    "failure_reason": failure_reason,
                    "metadata": _json(metadata),
                },
            )
            if result.executor_session_id:
                self._upsert_session_with_conn(
                    conn,
                    task,
                    executor_session_id=result.executor_session_id,
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
            for artifact in artifacts or []:
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
            self._append_event(
                conn,
                task_id,
                status,
                {"result": result.to_dict(), "agent_run": _agent_run_to_dict(task)},
            )
            self._release_claims(conn, task_id, status="completed")
            self._resolve_cancel(conn, task_id)
            return task

    def retry_agent_run(
        self,
        task_id: str,
        *,
        new_agent_run_id: str | None = None,
        resume_session: bool = False,
    ) -> AgentRunRecord:
        task = self.get_agent_run(task_id)
        if not task.is_terminal:
            raise ValueError("only terminal AgentRuns can be retried")
        metadata = dict(task.metadata)
        metadata["retry_of"] = task.id
        metadata["attempt"] = int(metadata.get("attempt", 1) or 1) + 1
        from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest

        return self.submit_agent_run(
            AgentRunRequest(
                issue_id=task.issue_id,
                agent_id=task.agent_id,
                prompt=task.prompt,
                source=task.source,
                executor=task.executor or ExecutorType.REULEAUXCODER,
                execution_location=(
                    task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                ),
                trigger_mode=task.trigger_mode,
                runtime_profile_id=task.runtime_profile_id,
                parent_task_id=task.parent_task_id or task.id,
                trigger_comment_id=task.trigger_comment_id,
                branch_name=task.branch_name,
                pr_url=task.pr_url,
                workdir=task.workdir,
                sandbox_id=task.sandbox_id,
                sandbox_session_id=task.sandbox_session_id,
                workspace_ref=task.workspace_ref,
                delegated_by_run_id=task.delegated_by_run_id,
                parent_run_id=task.parent_run_id,
                executor_session_id=task.executor_session_id
                if resume_session
                else None,
                model=str(task.metadata.get("model"))
                if task.metadata.get("model") is not None
                else None,
                metadata=metadata,
            ),
            task_id=new_agent_run_id,
        )

    def fail_agent_run(self, task_id: str, *, error: str) -> AgentRunRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status='failed', output=:error, failure_reason='manual',
                        completed_at=now(), updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {"task_id": task_id, "error": error},
            )
            self._append_event(conn, task_id, "failed", {"error": error})
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
                        SET status='cancelled', cancel_reason=:reason,
                            completed_at=now(), updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id, "reason": reason},
                )
                self._append_event(conn, task_id, "cancelled", {"reason": reason})
                self._release_claims(conn, task_id, status="cancelled")
                self._resolve_cancel(conn, task_id)
                return True
            if task.status in {
                TaskStatus.DISPATCHED,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
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
                self._append_event(
                    conn,
                    task_id,
                    "cancel_requested",
                    {"reason": reason, "worker_id": task.worker_id},
                )
                return True
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET status='cancelled', cancel_reason=:reason,
                        completed_at=now(), updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {"task_id": task_id, "reason": reason},
            )
            self._append_event(conn, task_id, "cancelled", {"reason": reason})
            self._release_claims(conn, task_id, status="cancelled")
            return True

    def attach_artifact(self, task_id: str, **kwargs: Any) -> TaskArtifact:
        with self.engine.begin() as conn:
            return self._attach_artifact_with_conn(conn, task_id, **kwargs)

    def create_or_update_pr(self, task_id: str, *, diff: str = "") -> TaskArtifact:
        task = self.get_agent_run(task_id)
        pr = self.pr_flow.create_or_update(task, diff=diff)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_runs
                    SET branch_name=:branch_name, pr_url=:pr_url, updated_at=now()
                    WHERE id=:task_id
                    """
                ),
                {"task_id": task_id, "branch_name": pr.branch_name, "pr_url": pr.pr_url},
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
                text("SELECT * FROM labrastro_agent_run_artifacts WHERE task_id=:task_id"),
                {"task_id": task_id},
            ).mappings()
            return [self._artifact_from_row(row) for row in rows]

    def get_agent_run(self, task_id: str) -> AgentRunRecord:
        with self.engine.begin() as conn:
            return self._task_from_row(self._task_row(conn, task_id))

    def agent_run_to_dict(self, task_id: str) -> dict[str, Any]:
        return _agent_run_to_dict(self.get_agent_run(task_id))

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]:
        return [_artifact_to_dict(artifact) for artifact in self.list_artifacts(task_id)]

    def list_agent_runs(self, **filters: Any) -> list[dict[str, Any]]:
        clauses = ["deleted_at IS NULL" if False else "1=1"]
        params: dict[str, Any] = {"limit": max(1, min(500, int(filters.get("limit") or 50)))}
        for key in ("status", "agent_id", "issue_id"):
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
                    SELECT request_id, task_id, worker_id, peer_id, status,
                           lease_sec, lease_deadline, last_heartbeat_at, claimed_at,
                           released_at, metadata
                    FROM labrastro_agent_run_claims
                    WHERE task_id=:task_id
                    ORDER BY claimed_at DESC
                    LIMIT 1
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
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
            "events": events,
        }

    def _resolve_request(self, request: Any) -> Any:
        parent = self.get_agent_run(request.parent_task_id) if request.parent_task_id else None
        if parent is not None:
            if request.runtime_profile_id is None:
                request.runtime_profile_id = parent.runtime_profile_id
            if request.executor is None:
                request.executor = parent.executor
            if request.execution_location is None:
                request.execution_location = parent.execution_location
            if request.workdir is None:
                request.workdir = parent.workdir
            if request.branch_name is None:
                request.branch_name = parent.branch_name
            if request.pr_url is None:
                request.pr_url = parent.pr_url
        agents = _dict_from(self.runtime_snapshot.get("agents"))
        profiles = _dict_from(self.runtime_snapshot.get("runtime_profiles"))
        raw_agent = _dict_from(agents.get(request.agent_id))
        agent_profile_id = str(raw_agent.get("runtime_profile") or "").strip()
        profile_id = str(request.runtime_profile_id or agent_profile_id).strip()
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
            or ExecutionLocation.LOCAL_WORKSPACE
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

    def _task_from_row(self, row: Any) -> AgentRunRecord:
        metadata = _dict_from(row["metadata"])
        return AgentRunRecord(
            id=str(row["id"]),
            issue_id=str(row["issue_id"]),
            agent_id=str(row["agent_id"]),
            source=AgentRunSource(str(metadata.get("agent_run_source") or "manual")),
            trigger_mode=TriggerMode(str(row["trigger_mode"])),
            status=TaskStatus(str(row["status"])),
            prompt=str(row["prompt"] or ""),
            runtime_profile_id=row["runtime_profile_id"],
            executor=_optional_executor(row["executor"]),
            execution_location=_optional_location(row["execution_location"]),
            output=row["output"],
            parent_task_id=row["parent_task_id"],
            trigger_comment_id=row["trigger_comment_id"],
            branch_name=row["branch_name"],
            pr_url=row["pr_url"],
            worker_id=row["worker_id"],
            executor_session_id=row["executor_session_id"],
            workdir=row["workdir"],
            sandbox_id=metadata.get("sandbox_id"),
            sandbox_session_id=metadata.get("sandbox_session_id"),
            workspace_ref=metadata.get("workspace_ref"),
            delegated_by_run_id=metadata.get("delegated_by_run_id"),
            parent_run_id=metadata.get("parent_run_id") or row["parent_task_id"],
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
        task: AgentRunRecord,
        *,
        features: set[str] | None,
        workspace_root: str | None,
    ) -> bool:
        location = task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
        if features is None:
            return True
        if location == ExecutionLocation.LOCAL_WORKSPACE:
            if (
                "agent_runs" not in features
                and "agent_runs.local_workspace" not in features
            ):
                return False
            bound_workspace = str(task.metadata.get("workspace_root") or "").strip()
            if bound_workspace:
                return bool(workspace_root) and _workspace_key(
                    bound_workspace
                ) == _workspace_key(workspace_root)
            return True
        location_feature = f"agent_runs.{location.value}"
        if location_feature in features:
            return True
        return "agent_runs" in features

    def _agent_concurrency_allows(self, conn: Any, task: AgentRunRecord) -> bool:
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
                  AND status IN ('dispatched', 'running', 'waiting_approval')
                """
            ),
            {"agent_id": task.agent_id},
        ).scalar_one()
        return int(count) < limit

    def _executor_metadata(self, task: AgentRunRecord) -> dict[str, Any]:
        metadata = dict(task.metadata)
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
        overlay = _dict_from(resolved.get("capability_overlay"))
        if overlay:
            metadata.setdefault("capability_overlay", overlay)
        return metadata

    def _render_prompt_for_task(self, task: AgentRunRecord, executor: ExecutorType) -> Any:
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

    def _active_claim(self, conn: Any, request_id: str) -> Any | None:
        return conn.execute(
            text(
                """
                SELECT * FROM labrastro_agent_run_claims
                WHERE request_id=:request_id AND status='active'
                """
            ),
            {"request_id": request_id},
        ).mappings().first()

    def _claim_owner_ok(
        self, row: Any, task_id: str, worker_id: str, peer_id: str | None
    ) -> tuple[bool, str]:
        if str(row["task_id"]) != task_id:
            return False, "task_mismatch"
        if str(row["worker_id"]) != worker_id:
            return False, "worker_mismatch"
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
                SELECT * FROM labrastro_agent_run_claims
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
                TaskStatus.DISPATCHED,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_APPROVAL,
            }:
                conn.execute(
                    text(
                        """
                        UPDATE labrastro_agent_runs
                        SET status='queued', worker_id=NULL, updated_at=now()
                        WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id},
                )
                recovered.append(task_id)
                self._append_event(
                    conn,
                    task_id,
                    "lease_expired",
                    {
                        "request_id": row["request_id"],
                        "worker_id": row["worker_id"],
                        "peer_id": row["peer_id"],
                    },
                )
            conn.execute(
                text(
                    """
                    UPDATE labrastro_agent_run_claims
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
        task: AgentRunRecord,
        session: TaskSessionRef,
        *,
        metadata: dict[str, Any],
    ) -> None:
        conn.execute(
            text(
                """
                UPDATE labrastro_agent_runs
                SET status=CASE WHEN status='dispatched' THEN 'running' ELSE status END,
                    executor_session_id=COALESCE(:executor_session_id, executor_session_id),
                    workdir=COALESCE(:workdir, workdir),
                    branch_name=COALESCE(:branch, branch_name),
                    started_at=COALESCE(started_at, now()),
                    updated_at=now()
                WHERE id=:task_id
                """
            ),
            {
                "task_id": task.id,
                "executor_session_id": session.executor_session_id,
                "workdir": session.workdir,
                "branch": session.branch,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_sessions (
                    task_id, agent_id, executor, execution_location, issue_id,
                    workdir, branch, executor_session_id, metadata
                ) VALUES (
                    :task_id, :agent_id, :executor, :execution_location, :issue_id,
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
                    task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                ).value,
                "issue_id": task.issue_id,
                "workdir": session.workdir,
                "branch": session.branch,
                "executor_session_id": session.executor_session_id,
                "metadata": _json(metadata),
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
        task: AgentRunRecord,
        *,
        executor_session_id: str,
    ) -> None:
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_sessions (
                    task_id, agent_id, executor, execution_location, issue_id,
                    workdir, branch, executor_session_id, metadata
                ) VALUES (
                    :task_id, :agent_id, :executor, :execution_location, :issue_id,
                    :workdir, :branch, :executor_session_id, CAST('{}' AS JSONB)
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    workdir=COALESCE(EXCLUDED.workdir, labrastro_agent_run_sessions.workdir),
                    branch=COALESCE(EXCLUDED.branch, labrastro_agent_run_sessions.branch),
                    executor_session_id=EXCLUDED.executor_session_id,
                    updated_at=now()
                """
            ),
            {
                "task_id": task.id,
                "agent_id": task.agent_id,
                "executor": (task.executor or ExecutorType.REULEAUXCODER).value,
                "execution_location": (
                    task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
                ).value,
                "issue_id": task.issue_id,
                "workdir": task.workdir,
                "branch": task.branch_name,
                "executor_session_id": executor_session_id,
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
        self._append_event(conn, task_id, "artifact_attached", {"artifact": _artifact_to_dict(artifact)})
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
                UPDATE labrastro_agent_run_claims
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
