"""Create Taskflow planning and dispatch tables."""

from __future__ import annotations

from alembic import op

revision = "0002_taskflow_control_plane"
down_revision = "0001_postgres_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_goals (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL,
            session_id TEXT,
            peer_id TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            next_event_seq BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_briefs (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL UNIQUE
                REFERENCES labrastro_taskflow_goals(id) ON DELETE CASCADE,
            summary TEXT NOT NULL DEFAULT '',
            decision_points JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL,
            version INT NOT NULL DEFAULT 1,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_issue_drafts (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_goals(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'proposed',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_task_drafts (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_goals(id) ON DELETE CASCADE,
            issue_draft_id TEXT
                REFERENCES labrastro_taskflow_issue_drafts(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
            preferred_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
            task_type TEXT,
            workspace_root TEXT,
            repo_url TEXT,
            execution_location TEXT,
            manual_agent_id TEXT,
            runtime_task_id TEXT REFERENCES labrastro_runtime_tasks(id) ON DELETE SET NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_dispatch_decisions (
            id TEXT PRIMARY KEY,
            task_draft_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_task_drafts(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            selected_agent_id TEXT,
            candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
            filtered JSONB NOT NULL DEFAULT '[]'::jsonb,
            score_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            manual_override BOOLEAN NOT NULL DEFAULT FALSE,
            reason TEXT NOT NULL DEFAULT '',
            runtime_task_id TEXT REFERENCES labrastro_runtime_tasks(id) ON DELETE SET NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_taskflow_events (
            goal_id TEXT NOT NULL
                REFERENCES labrastro_taskflow_goals(id) ON DELETE CASCADE,
            seq BIGINT NOT NULL,
            type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (goal_id, seq)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_goals_status_updated
            ON labrastro_taskflow_goals(status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_issues_goal
            ON labrastro_taskflow_issue_drafts(goal_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_tasks_goal_status
            ON labrastro_taskflow_task_drafts(goal_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_dispatch_draft
            ON labrastro_taskflow_dispatch_decisions(task_draft_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_taskflow_events_goal_seq
            ON labrastro_taskflow_events(goal_id, seq)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_events")
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_dispatch_decisions")
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_task_drafts")
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_issue_drafts")
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_briefs")
    op.execute("DROP TABLE IF EXISTS labrastro_taskflow_goals")
