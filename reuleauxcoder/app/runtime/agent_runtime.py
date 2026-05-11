"""Interactive AgentRun slot limiter."""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator


RuntimeWaitCallback = Callable[[dict[str, Any]], None]
CancelPredicate = Callable[[], bool]


class AgentRunCancelled(RuntimeError):
    """Raised when a queued interactive run is cancelled before it starts."""


class InteractiveRunLimiter:
    """Coordinate in-process interactive AgentRun and shell slots."""

    def __init__(self, *, max_running_agents: int = 4, max_shells_per_agent: int = 1):
        self._max_running_agents = max(1, int(max_running_agents))
        self._max_shells_per_agent = max(1, int(max_shells_per_agent))
        self._cond = threading.Condition(threading.RLock())
        self._agent_queue: deque[str] = deque()
        self._running_agents: dict[str, dict[str, Any]] = {}
        self._shell_queues: dict[str, deque[str]] = defaultdict(deque)
        self._running_shells: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def configure(self, *, max_running_agents: int, max_shells_per_agent: int) -> None:
        with self._cond:
            self._max_running_agents = max(1, int(max_running_agents))
            self._max_shells_per_agent = max(1, int(max_shells_per_agent))
            self._cond.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            shell_usage = {
                agent_id: len(shells)
                for agent_id, shells in self._running_shells.items()
                if shells
            }
            queued_shells = {
                agent_id: len(queue)
                for agent_id, queue in self._shell_queues.items()
                if queue
            }
            return {
                "max_running_agents": self._max_running_agents,
                "max_shells_per_agent": self._max_shells_per_agent,
                "running_agents": len(self._running_agents),
                "queued_agents": len(self._agent_queue),
                "agent_ids": list(self._running_agents.keys()),
                "shell_usage": shell_usage,
                "queued_shells": queued_shells,
            }

    @contextmanager
    def agent_slot(
        self,
        agent_id: str,
        *,
        agent_type: str = "agent",
        label: str = "",
        is_cancelled: CancelPredicate | None = None,
        on_wait: RuntimeWaitCallback | None = None,
    ) -> Iterator[None]:
        self.acquire_agent_slot(
            agent_id,
            agent_type=agent_type,
            label=label,
            is_cancelled=is_cancelled,
            on_wait=on_wait,
        )
        try:
            yield
        finally:
            self.release_agent_slot(agent_id)

    def acquire_agent_slot(
        self,
        agent_id: str,
        *,
        agent_type: str = "agent",
        label: str = "",
        is_cancelled: CancelPredicate | None = None,
        on_wait: RuntimeWaitCallback | None = None,
    ) -> None:
        queued_at = time.time()
        queued_announced = False
        with self._cond:
            if agent_id not in self._agent_queue and agent_id not in self._running_agents:
                self._agent_queue.append(agent_id)
            while True:
                if is_cancelled is not None and is_cancelled():
                    self._remove_from_queue(self._agent_queue, agent_id)
                    self._cond.notify_all()
                    raise AgentRunCancelled("agent_run_cancelled")
                can_run = (
                    self._agent_queue
                    and self._agent_queue[0] == agent_id
                    and len(self._running_agents) < self._max_running_agents
                )
                if can_run:
                    self._agent_queue.popleft()
                    self._running_agents[agent_id] = {
                        "agent_type": agent_type,
                        "label": label,
                        "started_at": time.time(),
                    }
                    self._emit(
                        on_wait,
                        {
                            "phase": "agent_queue",
                            "status": "running",
                            "agent_id": agent_id,
                            "agent_type": agent_type,
                            "label": label,
                            "message": "Agent 已获得服务端运行槽位。",
                            "runtime": self.snapshot(),
                        },
                    )
                    return
                if not queued_announced:
                    queued_announced = True
                    self._emit(
                        on_wait,
                        {
                            "phase": "agent_queue",
                            "status": "queued",
                            "agent_id": agent_id,
                            "agent_type": agent_type,
                            "label": label,
                            "message": "等待服务端 Agent 运行槽位...",
                            "runtime": self.snapshot(),
                        },
                    )
                self._cond.wait(timeout=0.5)
                if time.time() - queued_at > 0.5:
                    queued_at = time.time()

    def release_agent_slot(self, agent_id: str) -> None:
        with self._cond:
            self._running_agents.pop(agent_id, None)
            self._running_shells.pop(agent_id, None)
            self._shell_queues.pop(agent_id, None)
            self._cond.notify_all()

    @contextmanager
    def shell_slot(
        self,
        agent_id: str,
        *,
        tool_call_id: str = "",
        is_cancelled: CancelPredicate | None = None,
        on_wait: RuntimeWaitCallback | None = None,
    ) -> Iterator[None]:
        token = f"{tool_call_id or 'shell'}:{uuid.uuid4().hex[:8]}"
        self.acquire_shell_slot(
            agent_id,
            token,
            tool_call_id=tool_call_id,
            is_cancelled=is_cancelled,
            on_wait=on_wait,
        )
        try:
            yield
        finally:
            self.release_shell_slot(agent_id, token)

    def acquire_shell_slot(
        self,
        agent_id: str,
        token: str,
        *,
        tool_call_id: str = "",
        is_cancelled: CancelPredicate | None = None,
        on_wait: RuntimeWaitCallback | None = None,
    ) -> None:
        queued_announced = False
        with self._cond:
            queue = self._shell_queues[agent_id]
            if token not in queue and token not in self._running_shells[agent_id]:
                queue.append(token)
            while True:
                if is_cancelled is not None and is_cancelled():
                    self._remove_from_queue(queue, token)
                    self._cond.notify_all()
                    raise AgentRunCancelled("shell_run_cancelled")
                can_run = (
                    queue
                    and queue[0] == token
                    and len(self._running_shells[agent_id])
                    < self._max_shells_per_agent
                )
                if can_run:
                    queue.popleft()
                    self._running_shells[agent_id][token] = {
                        "tool_call_id": tool_call_id,
                        "started_at": time.time(),
                    }
                    self._emit(
                        on_wait,
                        {
                            "phase": "shell_queue",
                            "status": "running",
                            "agent_id": agent_id,
                            "tool_call_id": tool_call_id,
                            "message": "shell 已获得当前 Agent 执行槽位。",
                            "runtime": self.snapshot(),
                        },
                    )
                    return
                if not queued_announced:
                    queued_announced = True
                    self._emit(
                        on_wait,
                        {
                            "phase": "shell_queue",
                            "status": "queued",
                            "agent_id": agent_id,
                            "tool_call_id": tool_call_id,
                            "message": "等待当前 Agent 的 shell 执行槽位...",
                            "runtime": self.snapshot(),
                        },
                    )
                self._cond.wait(timeout=0.5)

    def release_shell_slot(self, agent_id: str, token: str) -> None:
        with self._cond:
            shells = self._running_shells.get(agent_id)
            if shells is not None:
                shells.pop(token, None)
                if not shells:
                    self._running_shells.pop(agent_id, None)
            self._cond.notify_all()

    @staticmethod
    def _remove_from_queue(queue: deque[str], token: str) -> None:
        try:
            queue.remove(token)
        except ValueError:
            pass

    @staticmethod
    def _emit(callback: RuntimeWaitCallback | None, payload: dict[str, Any]) -> None:
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            pass

_GLOBAL_INTERACTIVE_RUN_LIMITER = InteractiveRunLimiter()


def get_interactive_run_limiter() -> InteractiveRunLimiter:
    return _GLOBAL_INTERACTIVE_RUN_LIMITER
