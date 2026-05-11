"""Issue Assignment and Mention Agent control-plane service."""

from __future__ import annotations

import re
import uuid
from typing import Any

from reuleauxcoder.domain.issue_assignment.models import (
    AssignmentRecord,
    AssignmentStatus,
    IssueRecord,
    IssueStatus,
    MentionRecord,
    MentionStatus,
)
from labrastro_server.services.collaboration.in_memory_store import (
    InMemoryIssueAssignmentStore,
)
from labrastro_server.services.collaboration.store import IssueAssignmentStore
from labrastro_server.services.taskflow.service import TaskflowService
from labrastro_server.taskflow.domain.project_state import TaskRunStatus
from labrastro_server.taskflow.domain.taskflow_state import (
    ReadinessGate,
    TaskflowStatus,
    WorkItemCandidate,
)


_MENTION_RE = re.compile(r"@([A-Za-z0-9_.-]+)")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class IssueAssignmentService:
    """Facade for Issue, Assignment, and Mention Agent lifecycle.

    Issue Assignment owns the structured task-entry control state. It delegates
    actual Agent selection and AgentRun creation to Taskflow so capability
    dispatch, audit records, and user confirmation boundaries stay centralized.
    """

    def __init__(
        self,
        store: IssueAssignmentStore | None = None,
        *,
        taskflow_service: TaskflowService,
    ) -> None:
        self.store = store or InMemoryIssueAssignmentStore()
        self.taskflow_service = taskflow_service

    def _assert_issue_access(
        self, issue: IssueRecord, peer_id: str | None = None
    ) -> IssueRecord:
        if peer_id and issue.peer_id and issue.peer_id != peer_id:
            raise PermissionError("issue belongs to another peer")
        return issue

    def _get_issue_for_peer(
        self, issue_id: str, peer_id: str | None = None
    ) -> IssueRecord:
        return self._assert_issue_access(self.store.get_issue(issue_id), peer_id)

    def _get_assignment_for_peer(
        self, assignment_id: str, peer_id: str | None = None
    ) -> tuple[AssignmentRecord, IssueRecord]:
        assignment = self.store.get_assignment(assignment_id)
        issue = self._get_issue_for_peer(assignment.issue_id, peer_id)
        return assignment, issue

    def _assert_mention_access(
        self, mention: MentionRecord, peer_id: str | None = None
    ) -> MentionRecord:
        if peer_id and mention.peer_id and mention.peer_id != peer_id:
            raise PermissionError("mention belongs to another peer")
        return mention

    def create_issue(
        self,
        *,
        title: str,
        description: str = "",
        peer_id: str | None = None,
        source: str = "manual",
        taskflow_id: str | None = None,
        work_item_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        issue_id: str | None = None,
    ) -> IssueRecord:
        title = title.strip() or "Untitled issue"
        description = description or ""
        if taskflow_id:
            state = self.taskflow_service.get_taskflow_state(taskflow_id)
            if state.meta.status == TaskflowStatus.CANCELLED:
                raise ValueError("cannot create issue from cancelled Taskflow")
        else:
            state = self.taskflow_service.start_taskflow(
                project_id=f"peer-{peer_id or 'default'}",
                raw_goal=description or title,
                peer_id=peer_id,
                metadata={"source": "issue_assignment", **dict(metadata or {})},
            )
            taskflow_id = state.meta.taskflow_id
        issue = IssueRecord(
            id=issue_id or _new_id("issue"),
            title=title,
            description=description,
            peer_id=peer_id,
            source=source,
            taskflow_id=taskflow_id,
            work_item_id=work_item_id,
            metadata=dict(metadata or {}),
        )
        created = self.store.create_issue(issue)
        self.store.append_event(
            "issue", created.id, "issue_created", {"issue": created.to_dict()}
        )
        return created

    def get_issue(self, issue_id: str, *, peer_id: str | None = None) -> IssueRecord:
        return self._get_issue_for_peer(issue_id, peer_id)

    def load_issue_detail(
        self, issue_id: str, *, peer_id: str | None = None
    ) -> dict[str, Any]:
        issue = self._get_issue_for_peer(issue_id, peer_id)
        assignments = [
            assignment.to_dict()
            for assignment in self.store.list_assignments(issue.id)
        ]
        mentions = [
            mention.to_dict()
            for mention in self.store.list_mentions(issue_id=issue.id)
        ]
        taskflow_detail = None
        if issue.taskflow_id:
            try:
                taskflow_detail = self.taskflow_service.get_taskflow_state(
                    issue.taskflow_id
                ).to_dict()
            except Exception:
                taskflow_detail = None
        return {
            "issue": issue.to_dict(),
            "assignments": assignments,
            "mentions": mentions,
            "taskflow": taskflow_detail,
        }

    def list_assignments(
        self, issue_id: str, *, peer_id: str | None = None
    ) -> list[AssignmentRecord]:
        issue = self._get_issue_for_peer(issue_id, peer_id)
        return self.store.list_assignments(issue.id)

    def load_assignment_detail(
        self, assignment_id: str, *, peer_id: str | None = None
    ) -> dict[str, Any]:
        assignment, issue = self._get_assignment_for_peer(assignment_id, peer_id)
        payload: dict[str, Any] = {
            "assignment": assignment.to_dict(),
            "issue": issue.to_dict(),
            "work_item_id": assignment.work_item_id,
            "task_run_id": assignment.task_run_id,
        }
        return payload

    def create_assignment(
        self,
        issue_id: str,
        *,
        peer_id: str | None = None,
        target_agent_id: str | None = None,
        title: str | None = None,
        prompt: str | None = None,
        task_type: str | None = None,
        workspace_root: str | None = None,
        repo_url: str | None = None,
        execution_location: str | None = None,
        reason: str = "",
        source: str = "manual",
        metadata: dict[str, Any] | None = None,
        assignment_id: str | None = None,
    ) -> AssignmentRecord:
        issue = self._get_issue_for_peer(issue_id, peer_id)
        if issue.status == IssueStatus.CANCELLED:
            raise ValueError("cancelled issues cannot be assigned")
        taskflow_id = self._ensure_backing_goal(issue, peer_id=peer_id)
        assignment = AssignmentRecord(
            id=assignment_id or _new_id("assignment"),
            issue_id=issue.id,
            target_agent_id=_optional(target_agent_id),
            source=source,
            reason=reason,
            metadata=dict(metadata or {}),
        )
        candidate_metadata = {
            "issue_id": issue.id,
            "assignment_id": assignment.id,
            "assignment_source": source,
            "task_type": _optional(task_type),
            "workspace_root": _optional(workspace_root),
            "repo_url": _optional(repo_url),
            "execution_location": _optional(execution_location),
            **dict(metadata or {}),
        }
        acceptance_id = f"acceptance-{assignment.id}"
        self.taskflow_service.record_discovery_turn(
            taskflow_id,
            actor="issue_assignment",
            examples=[
                {
                    "id": acceptance_id,
                    "title": f"Assignment accepted: {title or issue.title}",
                    "then": ["The assignment creates a traceable WorkItem."],
                    "observable_outputs": ["work_item", "task_run"],
                }
            ],
            work_item_candidates=[
                WorkItemCandidate(
                    id=assignment.id,
                    title=(title or issue.title),
                    description=(prompt or issue.description or issue.title),
                    type="implementation",
                    acceptance_refs=[acceptance_id],
                    dedupe_key=f"{issue.id}:{assignment.id}",
                    metadata=candidate_metadata,
                )
            ],
        )
        self.taskflow_service.confirm_goal(taskflow_id, confirmed_by="issue_assignment")
        plan = self.taskflow_service.compile_goal(taskflow_id)
        work_item_id = plan.work_item_candidates[0].work_item_id
        assignment.work_item_id = work_item_id
        created = self.store.create_assignment(assignment)
        self.store.append_event(
            "issue",
            issue.id,
            "assignment_created",
            {"issue": issue.to_dict(), "assignment": created.to_dict()},
        )
        self.store.append_event(
            "assignment",
            created.id,
            "assignment_created",
            {"assignment": created.to_dict(), "work_item_id": work_item_id},
        )
        return created

    def dispatch_assignment(
        self, assignment_id: str, *, peer_id: str | None = None
    ) -> AssignmentRecord:
        assignment, issue = self._get_assignment_for_peer(assignment_id, peer_id)
        if assignment.status == AssignmentStatus.CANCELLED:
            raise ValueError("cancelled assignments cannot be dispatched")
        if not assignment.work_item_id:
            raise ValueError("assignment has no work item")
        dispatch_source = "mention" if assignment.source == "mention" else "assignment"
        taskflow_id = issue.taskflow_id or self._ensure_backing_goal(issue, peer_id=peer_id)
        dispatch_decision = self.taskflow_service.request_dispatch_decision(
            taskflow_id,
            work_item_ids=[assignment.work_item_id],
            actor=peer_id or dispatch_source,
            rationale=f"{dispatch_source} dispatch requested.",
            metadata={"dispatch_source": dispatch_source, "assignment_id": assignment.id},
        )
        self.taskflow_service.confirm_dispatch_decision(
            taskflow_id,
            decision_id=dispatch_decision.id,
            actor=peer_id or dispatch_source,
        )
        run = self.taskflow_service.dispatch_task_run(
            taskflow_id,
            work_item_id=assignment.work_item_id,
            dispatch_decision_id=dispatch_decision.id,
            executor_hint=assignment.target_agent_id,
            metadata={
                "issue_id": issue.id,
                "assignment_id": assignment.id,
                "dispatch_source": dispatch_source,
            },
        )
        assignment.task_run_id = run.id
        assignment.status = (
            AssignmentStatus.DISPATCHED
            if run.status == TaskRunStatus.DISPATCHED
            else AssignmentStatus.NEEDS_ASSIGNMENT
        )
        saved = self.store.update_assignment(assignment)
        event_type = (
            "assignment_dispatched"
            if saved.status == AssignmentStatus.DISPATCHED
            else "assignment_needs_assignment"
        )
        payload = {
            "issue": issue.to_dict(),
            "assignment": saved.to_dict(),
            "task_run": run.to_dict(),
        }
        self.store.append_event("issue", issue.id, event_type, payload)
        self.store.append_event("assignment", saved.id, event_type, payload)
        return saved

    def cancel_assignment(
        self,
        assignment_id: str,
        *,
        peer_id: str | None = None,
        reason: str = "user_cancelled",
    ) -> AssignmentRecord:
        assignment, issue = self._get_assignment_for_peer(assignment_id, peer_id)
        assignment.status = AssignmentStatus.CANCELLED
        assignment.metadata.setdefault("cancel_reason", reason)
        saved = self.store.update_assignment(assignment)
        payload = {"issue": issue.to_dict(), "assignment": saved.to_dict()}
        self.store.append_event("issue", issue.id, "assignment_cancelled", payload)
        self.store.append_event("assignment", saved.id, "assignment_cancelled", payload)
        return saved

    def reassign_assignment(
        self,
        assignment_id: str,
        *,
        agent_id: str,
        peer_id: str | None = None,
        reason: str = "manual_reassign",
    ) -> AssignmentRecord:
        assignment, issue = self._get_assignment_for_peer(assignment_id, peer_id)
        if assignment.status == AssignmentStatus.DISPATCHED:
            raise ValueError("dispatched assignments cannot be reassigned")
        if assignment.status == AssignmentStatus.CANCELLED:
            raise ValueError("cancelled assignments cannot be reassigned")
        previous_agent_id = assignment.target_agent_id
        assignment.target_agent_id = agent_id
        assignment.status = AssignmentStatus.READY
        assignment.reason = reason or assignment.reason
        assignment.metadata.setdefault("reassigned_from_agent_id", previous_agent_id)
        saved = self.store.update_assignment(assignment)
        payload = {
            "issue": issue.to_dict(),
            "assignment": saved.to_dict(),
            "previous_agent_id": previous_agent_id,
        }
        self.store.append_event("issue", issue.id, "assignment_reassigned", payload)
        self.store.append_event("assignment", saved.id, "assignment_reassigned", payload)
        return saved

    def get_mention(
        self, mention_id: str, *, peer_id: str | None = None
    ) -> MentionRecord:
        return self._assert_mention_access(self.store.get_mention(mention_id), peer_id)

    def parse_mention(
        self,
        *,
        raw_text: str,
        agent_ref: str | None = None,
        peer_id: str | None = None,
    ) -> MentionRecord:
        ref = _optional(agent_ref) or self._extract_mention_ref(raw_text)
        mention = self._mention_from_resolution(
            raw_text=raw_text,
            ref=ref,
            peer_id=peer_id,
            mention_id=_new_id("mention-parse"),
        )
        return mention

    def create_mention(
        self,
        *,
        raw_text: str,
        peer_id: str | None = None,
        agent_ref: str | None = None,
        issue_id: str | None = None,
        title: str | None = None,
        prompt: str | None = None,
        context_type: str = "chat",
        context_id: str | None = None,
        source: str = "manual",
        metadata: dict[str, Any] | None = None,
    ) -> MentionRecord:
        issue = self._get_issue_for_peer(issue_id, peer_id) if issue_id else None
        ref = _optional(agent_ref) or self._extract_mention_ref(raw_text)
        mention = self._mention_from_resolution(
            raw_text=raw_text,
            ref=ref,
            peer_id=peer_id,
            mention_id=_new_id("mention"),
        )
        mention.issue_id = issue.id if issue else None
        mention.context_type = context_type or "chat"
        mention.context_id = _optional(context_id)
        mention.source = source
        mention.metadata = dict(metadata or {})
        if mention.resolved_agent_id and issue is not None:
            assignment = self.create_assignment(
                issue.id,
                peer_id=peer_id,
                target_agent_id=mention.resolved_agent_id,
                title=title,
                prompt=prompt or raw_text,
                reason=f"mention:{mention.agent_ref}",
                source="mention",
                metadata={"mention_id": mention.id, **dict(metadata or {})},
            )
            mention.assignment_id = assignment.id
            mention.status = MentionStatus.READY
            mention.reason = "assignment_created"
        elif mention.resolved_agent_id:
            mention.status = MentionStatus.PARSED
            mention.reason = "agent_resolved"
        saved = self.store.create_mention(mention)
        payload = {"mention": saved.to_dict()}
        self.store.append_event("mention", saved.id, "mention_created", payload)
        if issue is not None:
            self.store.append_event("issue", issue.id, "mention_created", payload)
        return saved

    def list_events(
        self,
        scope: str,
        scope_id: str,
        *,
        after_seq: int = 0,
        timeout_sec: float = 0.0,
        peer_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._assert_scope_access(scope, scope_id, peer_id)
        events = self.store.wait_events(
            scope, scope_id, after_seq=after_seq, timeout_sec=timeout_sec
        )
        return [event.to_dict() for event in events]

    def _ensure_backing_goal(
        self, issue: IssueRecord, *, peer_id: str | None = None
    ) -> str:
        taskflow_id = issue.taskflow_id
        if taskflow_id:
            state = self.taskflow_service.get_taskflow_state(taskflow_id)
            if state.meta.status == TaskflowStatus.CANCELLED:
                raise ValueError("cancelled Taskflow cannot accept assignments")
            return taskflow_id
        state = self.taskflow_service.start_taskflow(
            project_id=f"peer-{peer_id or 'default'}",
            raw_goal=issue.description or issue.title,
            peer_id=peer_id,
            metadata={"source": "issue_assignment", "issue_id": issue.id},
        )
        issue.taskflow_id = state.meta.taskflow_id
        self.store.update_issue(issue)
        return state.meta.taskflow_id

    def _mention_from_resolution(
        self,
        *,
        raw_text: str,
        ref: str | None,
        peer_id: str | None,
        mention_id: str,
    ) -> MentionRecord:
        if not ref:
            return MentionRecord(
                id=mention_id,
                raw_text=raw_text,
                peer_id=peer_id,
                status=MentionStatus.NEEDS_ASSIGNMENT,
                reason="agent_ref_missing",
            )
        candidates = self._resolve_agent_ref(ref)
        if len(candidates) == 1:
            return MentionRecord(
                id=mention_id,
                raw_text=raw_text,
                peer_id=peer_id,
                status=MentionStatus.PARSED,
                agent_ref=ref,
                resolved_agent_id=str(candidates[0]["agent_id"]),
                candidates=candidates,
                reason="agent_resolved",
            )
        reason = "alias_ambiguous" if len(candidates) > 1 else "agent_not_found"
        return MentionRecord(
            id=mention_id,
            raw_text=raw_text,
            peer_id=peer_id,
            status=MentionStatus.NEEDS_ASSIGNMENT,
            agent_ref=ref,
            candidates=candidates,
            reason=reason,
        )

    def _extract_mention_ref(self, raw_text: str) -> str | None:
        match = _MENTION_RE.search(raw_text or "")
        return match.group(1) if match else None

    def _resolve_agent_ref(self, ref: str) -> list[dict[str, Any]]:
        normalized = ref.strip().lstrip("@").lower()
        dispatcher = getattr(self.taskflow_service, "dispatcher", None)
        runtime = getattr(dispatcher, "runtime_control_plane", None)
        snapshot = _dict(runtime.runtime_snapshot) if runtime is not None else {}
        agents = _dict(snapshot.get("agents"))
        candidates: list[dict[str, Any]] = []
        for agent_id, raw_agent in agents.items():
            raw = _dict(raw_agent)
            aliases = {str(agent_id).lower()}
            for key in ("alias", "name"):
                if raw.get(key) is not None:
                    aliases.add(str(raw[key]).lower())
            for key in ("aliases", "mention_aliases"):
                for alias in _string_list(raw.get(key)):
                    aliases.add(alias.lower().lstrip("@"))
            if normalized not in aliases:
                continue
            candidates.append(
                {
                    "agent_id": str(agent_id),
                    "name": str(raw.get("name") or ""),
                    "dispatch": _dict(raw.get("dispatch")),
                    "capability_refs": _string_list(raw.get("capability_refs")),
                    "runtime_profile": str(raw.get("runtime_profile") or ""),
                    "matched_ref": ref,
                }
            )
        return sorted(candidates, key=lambda item: str(item["agent_id"]))

    def _assert_scope_access(
        self, scope: str, scope_id: str, peer_id: str | None = None
    ) -> None:
        if scope == "issue":
            self._get_issue_for_peer(scope_id, peer_id)
            return
        if scope == "assignment":
            self._get_assignment_for_peer(scope_id, peer_id)
            return
        if scope == "mention":
            self.get_mention(scope_id, peer_id=peer_id)
            return
        raise ValueError(f"unsupported event scope: {scope}")
