"""Create Taskflow ProjectState and TaskflowState snapshot tables."""

from __future__ import annotations

from alembic import op

revision = "0002_taskflow_control_plane"
down_revision = "0001_postgres_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_projects (
            project_id TEXT PRIMARY KEY,
            state JSONB NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'taskflow.project_state.v1',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_states (
            taskflow_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_projects(project_id) ON DELETE CASCADE,
            goal_id TEXT NOT NULL,
            status TEXT NOT NULL,
            state JSONB NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'taskflow.state.v1',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_states_project_status
            ON labrastro_taskflow_states(project_id, status, updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_states")
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_projects")
