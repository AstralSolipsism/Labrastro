"""Add Taskflow append-only event ledger."""

from __future__ import annotations

from alembic import op

revision = "0009_taskflow_event_ledger"
down_revision = "0008_agent_scoped_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_events (
            event_id TEXT PRIMARY KEY,
            taskflow_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_states(taskflow_id) ON DELETE CASCADE,
            project_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_projects(project_id) ON DELETE CASCADE,
            goal_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_events_taskflow_created
            ON labrastro_taskflow_events(taskflow_id, created_at ASC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_events_project_type
            ON labrastro_taskflow_events(project_id, event_type, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_events")
