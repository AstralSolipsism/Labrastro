"""Create Issue Assignment and Mention Agent tables."""

from __future__ import annotations

from alembic import op

revision = "0003_issue_assignment_mention"
down_revision = "0002_taskflow_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_issues (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            peer_id TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            taskflow_goal_id TEXT
                REFERENCES labrastro_taskflow_goals(id) ON DELETE SET NULL,
            taskflow_issue_draft_id TEXT
                REFERENCES labrastro_taskflow_issue_drafts(id) ON DELETE SET NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_assignments (
            id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL REFERENCES labrastro_issues(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'ready',
            target_agent_id TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            reason TEXT NOT NULL DEFAULT '',
            task_draft_id TEXT
                REFERENCES labrastro_taskflow_task_drafts(id) ON DELETE SET NULL,
            dispatch_decision_id TEXT
                REFERENCES labrastro_taskflow_dispatch_decisions(id) ON DELETE SET NULL,
            runtime_task_id TEXT REFERENCES labrastro_runtime_tasks(id) ON DELETE SET NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_mentions (
            id TEXT PRIMARY KEY,
            raw_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'parsed',
            peer_id TEXT,
            issue_id TEXT REFERENCES labrastro_issues(id) ON DELETE SET NULL,
            assignment_id TEXT
                REFERENCES labrastro_assignments(id) ON DELETE SET NULL,
            context_type TEXT NOT NULL DEFAULT 'chat',
            context_id TEXT,
            agent_ref TEXT NOT NULL DEFAULT '',
            resolved_agent_id TEXT,
            candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
            reason TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_assignment_events (
            scope TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (scope, scope_id, seq)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_issues_peer_status
            ON labrastro_issues(peer_id, status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_assignments_issue_status
            ON labrastro_assignments(issue_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_mentions_peer_issue
            ON labrastro_mentions(peer_id, issue_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_assignment_events_scope_seq
            ON labrastro_assignment_events(scope, scope_id, seq)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_assignment_events")
    op.execute("DROP TABLE IF EXISTS labrastro_mentions")
    op.execute("DROP TABLE IF EXISTS labrastro_assignments")
    op.execute("DROP TABLE IF EXISTS labrastro_issues")
