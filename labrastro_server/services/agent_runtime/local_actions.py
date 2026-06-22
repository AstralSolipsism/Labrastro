"""Control-plane service for AgentRun-bound local peer actions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
import threading
import time
import uuid
from typing import Any

from labrastro_server.interfaces.http.remote.protocol.local_actions import (
    LOCAL_ACTION_TERMINAL_STATUSES,
    LocalActionClaimResponse,
    LocalActionRecord,
)
from reuleauxcoder.domain.agent_runtime.models import WorkerKind


LocalActionEventSink = Callable[[str, str, dict[str, Any]], None]


class LocalActionError(ValueError):
    """Base error for local action service failures."""


class LocalActionLeaseError(LocalActionError):
    """Raised when a peer attempts to mutate an action without its lease."""


class LocalActionNotFoundError(LocalActionError):
    """Raised when a local action id is unknown."""


class LocalActionTerminalError(LocalActionError):
    """Raised when a terminal local action is mutated."""


class LocalActionService:
    """Store, dispatch, and complete AgentRun-bound local peer actions."""

    def __init__(
        self,
        *,
        event_sink: LocalActionEventSink | None = None,
        lease_sec: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._event_sink = event_sink
        self._lease_sec = max(1.0, float(lease_sec or 30.0))
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._actions: dict[str, LocalActionRecord] = {}

    def create_local_action(self, action: LocalActionRecord) -> LocalActionRecord:
        with self._lock:
            if action.local_action_id in self._actions:
                raise ValueError("local_action_id_conflict")
            now = self._clock()
            stored = self._clone(
                replace(
                    action,
                    status=action.status or "waiting_peer",
                    created_at=action.created_at if action.created_at is not None else now,
                    updated_at=now,
                )
            )
            self._actions[stored.local_action_id] = stored
            self._append_event_locked(stored, "local_action_requested")
            if stored.status != "requested":
                self._append_event_locked(stored, "local_action_waiting_peer")
            return self._clone(stored)

    def get_local_action(self, local_action_id: str) -> LocalActionRecord:
        with self._lock:
            return self._clone(self._action_locked(local_action_id))

    def list_local_actions(self) -> list[LocalActionRecord]:
        with self._lock:
            return [self._clone(action) for action in self._actions.values()]

    def claim_local_actions(
        self,
        *,
        peer_id: str,
        worker_kind: WorkerKind | str,
        features: Iterable[str],
        workspace_root: str | None = None,
        max_actions: int = 1,
    ) -> LocalActionClaimResponse:
        peer_id = str(peer_id or "").strip()
        if not peer_id:
            raise ValueError("peer_id_required")
        worker_kind = self._coerce_worker_kind(worker_kind)
        feature_set = {str(feature) for feature in features if str(feature).strip()}
        now = self._clock()
        claimed: list[LocalActionRecord] = []
        with self._lock:
            for action in self._actions.values():
                if len(claimed) >= max(1, int(max_actions or 1)):
                    break
                if not self._claim_match(
                    action,
                    peer_id=peer_id,
                    worker_kind=worker_kind,
                    features=feature_set,
                    workspace_root=workspace_root,
                    now=now,
                ):
                    continue
                lease_id = f"local-action-lease:{uuid.uuid4().hex}"
                action.peer_id = peer_id
                action.lease_id = lease_id
                action.lease_expires_at = now + self._lease_sec
                action.status = "started"
                action.updated_at = now
                self._append_event_locked(action, "local_action_started")
                claimed.append(self._clone(action))
        return LocalActionClaimResponse(actions=claimed)

    def progress_local_action(
        self,
        *,
        local_action_id: str,
        peer_id: str,
        lease_id: str,
        progress: dict[str, Any],
        status: str = "progress",
    ) -> LocalActionRecord:
        with self._lock:
            action = self._validated_lease_action_locked(
                local_action_id=local_action_id,
                peer_id=peer_id,
                lease_id=lease_id,
            )
            action.status = str(status or "progress")
            action.progress = dict(progress)
            action.updated_at = self._clock()
            action.lease_expires_at = action.updated_at + self._lease_sec
            self._append_event_locked(action, "local_action_progress")
            return self._clone(action)

    def complete_local_action(
        self,
        *,
        local_action_id: str,
        peer_id: str,
        lease_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> LocalActionRecord:
        terminal = str(status or "").strip()
        if terminal not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid_local_action_terminal_status")
        with self._lock:
            action = self._validated_lease_action_locked(
                local_action_id=local_action_id,
                peer_id=peer_id,
                lease_id=lease_id,
            )
            action.status = terminal
            action.result = dict(result or {})
            action.error = str(error).strip() if error else None
            action.updated_at = self._clock()
            event_type = {
                "completed": "local_action_completed",
                "failed": "local_action_failed",
                "cancelled": "local_action_cancelled",
            }[terminal]
            self._append_event_locked(action, event_type)
            return self._clone(action)

    def cancel_local_action(
        self,
        *,
        local_action_id: str,
        reason: str = "cancelled",
    ) -> LocalActionRecord:
        with self._lock:
            action = self._action_locked(local_action_id)
            if action.status in LOCAL_ACTION_TERMINAL_STATUSES:
                return self._clone(action)
            action.status = "cancelled"
            action.error = str(reason or "cancelled")
            action.updated_at = self._clock()
            self._append_event_locked(action, "local_action_cancelled")
            return self._clone(action)

    def _claim_match(
        self,
        action: LocalActionRecord,
        *,
        peer_id: str,
        worker_kind: WorkerKind,
        features: set[str],
        workspace_root: str | None,
        now: float,
    ) -> bool:
        if worker_kind != WorkerKind.LOCAL_PEER:
            return False
        assigned_peer = str(action.peer_id or "").strip()
        if assigned_peer and assigned_peer != str(peer_id or "").strip():
            return False
        if action.status in LOCAL_ACTION_TERMINAL_STATUSES:
            return False
        if action.lease_id and action.lease_expires_at and action.lease_expires_at > now:
            return False
        if "local_actions" not in features:
            return False
        if f"local_action:{action.action_kind}" not in features:
            return False
        if action.workspace_root and not _same_workspace(
            action.workspace_root,
            workspace_root,
        ):
            return False
        return True

    def _validated_lease_action_locked(
        self,
        *,
        local_action_id: str,
        peer_id: str,
        lease_id: str,
    ) -> LocalActionRecord:
        action = self._action_locked(local_action_id)
        if action.status in LOCAL_ACTION_TERMINAL_STATUSES:
            raise LocalActionTerminalError("local_action_terminal")
        expected_peer = str(action.peer_id or "").strip()
        if expected_peer and expected_peer != str(peer_id or "").strip():
            raise LocalActionLeaseError("local_action_peer_mismatch")
        if not action.lease_id or action.lease_id != str(lease_id or "").strip():
            raise LocalActionLeaseError("local_action_lease_mismatch")
        lease_expires_at = float(action.lease_expires_at or 0.0)
        if lease_expires_at <= self._clock():
            action.status = "waiting_peer"
            action.lease_id = None
            action.lease_expires_at = None
            action.updated_at = self._clock()
            self._append_event_locked(action, "local_action_waiting_peer")
            raise LocalActionLeaseError("local_action_lease_expired")
        return action

    def _action_locked(self, local_action_id: str) -> LocalActionRecord:
        action_id = str(local_action_id or "").strip()
        if not action_id:
            raise ValueError("local_action_id_required")
        action = self._actions.get(action_id)
        if action is None:
            raise LocalActionNotFoundError("local_action_not_found")
        return action

    def _append_event_locked(
        self,
        action: LocalActionRecord,
        event_type: str,
    ) -> None:
        if self._event_sink is None or not action.agent_run_id:
            return
        payload = action.to_dict()
        payload["event_type"] = event_type
        self._event_sink(action.agent_run_id, event_type, payload)

    @staticmethod
    def _clone(action: LocalActionRecord) -> LocalActionRecord:
        return LocalActionRecord.from_dict(action.to_dict())

    @staticmethod
    def _coerce_worker_kind(value: WorkerKind | str) -> WorkerKind:
        if isinstance(value, WorkerKind):
            return value
        return WorkerKind(str(value))


def _same_workspace(expected: str | None, actual: str | None) -> bool:
    expected_norm = _normalize_workspace(expected)
    actual_norm = _normalize_workspace(actual)
    return bool(expected_norm and actual_norm and expected_norm == actual_norm)


def _normalize_workspace(value: str | None) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").casefold()


__all__ = [
    "LocalActionError",
    "LocalActionLeaseError",
    "LocalActionNotFoundError",
    "LocalActionService",
    "LocalActionTerminalError",
]
