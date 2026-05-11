"""Background maintenance for Postgres persistence growth controls."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any


try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


@dataclass
class PersistenceMaintenanceResult:
    snapshot_versions_deleted: int = 0
    snapshot_retention_deleted: int = 0
    agent_run_events_deleted: int = 0


class PersistenceMaintenanceService:
    """Runs retention and bounded-history cleanup for Postgres persistence."""

    def __init__(
        self,
        engine: Any,
        *,
        retention_days: int = 0,
        snapshot_max_versions_per_session: int = 20,
        interval_sec: int = 3600,
    ) -> None:
        if text is None:
            raise RuntimeError("Persistence maintenance requires sqlalchemy.")
        self.engine = engine
        self.retention_days = max(0, int(retention_days or 0))
        self.snapshot_max_versions_per_session = max(
            1, int(snapshot_max_versions_per_session or 1)
        )
        self.interval_sec = max(1, int(interval_sec or 1))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.run_once()

        def loop() -> None:
            while not self._stop.wait(self.interval_sec):
                self.run_once()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def run_once(self) -> PersistenceMaintenanceResult:
        result = PersistenceMaintenanceResult()
        with self.engine.begin() as conn:
            result.snapshot_versions_deleted = self._delete_snapshot_overflow(conn)
            if self.retention_days > 0:
                result.snapshot_retention_deleted = self._delete_old_snapshots(conn)
                result.agent_run_events_deleted = self._delete_old_terminal_events(conn)
        return result

    def _delete_snapshot_overflow(self, conn: Any) -> int:
        query = text(
            """
            DELETE FROM labrastro_session_snapshots snapshots
            USING (
                SELECT session_id, version,
                    row_number() OVER (
                        PARTITION BY session_id
                        ORDER BY version DESC
                    ) AS rn
                FROM labrastro_session_snapshots
            ) ranked
            WHERE snapshots.session_id = ranked.session_id
              AND snapshots.version = ranked.version
              AND ranked.rn > :max_versions
            """
        )
        result = conn.execute(
            query,
            {"max_versions": self.snapshot_max_versions_per_session},
        )
        return int(result.rowcount or 0)

    def _delete_old_snapshots(self, conn: Any) -> int:
        query = text(
            """
            DELETE FROM labrastro_session_snapshots snapshots
            WHERE snapshots.created_at < now() - (:days * interval '1 day')
              AND EXISTS (
                  SELECT 1
                  FROM labrastro_session_snapshots newer
                  WHERE newer.session_id = snapshots.session_id
                    AND newer.version > snapshots.version
              )
            """
        )
        result = conn.execute(query, {"days": self.retention_days})
        return int(result.rowcount or 0)

    def _delete_old_terminal_events(self, conn: Any) -> int:
        query = text(
            """
            DELETE FROM labrastro_agent_run_events events
            USING labrastro_agent_runs tasks
            WHERE events.task_id = tasks.id
              AND tasks.status IN ('completed', 'failed', 'cancelled', 'blocked')
              AND events.created_at < now() - (:days * interval '1 day')
            """
        )
        result = conn.execute(query, {"days": self.retention_days})
        return int(result.rowcount or 0)
