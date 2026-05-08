"""Create Postgres-backed runtime and session control-plane tables."""

from __future__ import annotations

from alembic import op

revision = "0001_postgres_control_plane"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_tasks (
            id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            trigger_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            prompt TEXT NOT NULL,
            runtime_profile_id TEXT,
            executor TEXT,
            execution_location TEXT,
            output TEXT,
            parent_task_id TEXT REFERENCES labrastro_runtime_tasks(id) ON DELETE SET NULL,
            trigger_comment_id TEXT,
            branch_name TEXT,
            pr_url TEXT,
            worker_id TEXT,
            executor_session_id TEXT,
            workdir TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            runtime_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            issue_status TEXT NOT NULL DEFAULT 'open',
            failure_reason TEXT,
            cancel_reason TEXT,
            attempt INT NOT NULL DEFAULT 1,
            max_attempts INT NOT NULL DEFAULT 1,
            next_event_seq BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            dispatched_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_events (
            task_id TEXT NOT NULL REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            seq BIGINT NOT NULL,
            type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            source TEXT NOT NULL DEFAULT 'system',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (task_id, seq)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_claims (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            worker_id TEXT NOT NULL,
            peer_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            lease_sec INT NOT NULL,
            lease_deadline TIMESTAMPTZ NOT NULL,
            last_heartbeat_at TIMESTAMPTZ NOT NULL,
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            released_at TIMESTAMPTZ,
            runtime_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_labrastro_runtime_claims_active_task
            ON labrastro_runtime_claims(task_id)
            WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_sessions (
            task_id TEXT PRIMARY KEY REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            executor TEXT NOT NULL,
            execution_location TEXT NOT NULL,
            issue_id TEXT NOT NULL,
            workdir TEXT,
            branch TEXT,
            executor_session_id TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            pinned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_artifacts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            branch_name TEXT,
            pr_url TEXT,
            content TEXT,
            path TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            merge_status TEXT,
            merged_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_cancel_requests (
            task_id TEXT PRIMARY KEY REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            requested_by TEXT,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_runtime_locks (
            name TEXT PRIMARY KEY,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO labrastro_runtime_locks(name)
        VALUES ('global_claim')
        ON CONFLICT (name) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_sessions (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            model TEXT NOT NULL,
            saved_at TIMESTAMPTZ NOT NULL,
            preview TEXT NOT NULL DEFAULT '',
            messages JSONB NOT NULL DEFAULT '[]'::jsonb,
            runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb,
            active_mode TEXT,
            total_prompt_tokens INT NOT NULL DEFAULT 0,
            total_completion_tokens INT NOT NULL DEFAULT 0,
            has_history_content BOOLEAN NOT NULL DEFAULT TRUE,
            deleted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_session_snapshots (
            session_id TEXT NOT NULL REFERENCES labrastro_sessions(id) ON DELETE CASCADE,
            version BIGINT NOT NULL,
            snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            stats JSONB NOT NULL DEFAULT '{}'::jsonb,
            turn_count INT NOT NULL DEFAULT 0,
            trace_node_count INT NOT NULL DEFAULT 0,
            trace_edge_count INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (session_id, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_session_trace_events (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES labrastro_sessions(id) ON DELETE CASCADE,
            seq BIGINT NOT NULL,
            type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (session_id, seq)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_runtime_tasks_claim
            ON labrastro_runtime_tasks(status, created_at)
            WHERE status = 'queued'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_runtime_tasks_status_updated
            ON labrastro_runtime_tasks(status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_runtime_tasks_agent_issue
            ON labrastro_runtime_tasks(agent_id, issue_id, status)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_runtime_events_task_seq
            ON labrastro_runtime_events(task_id, seq)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_sessions_fingerprint_saved
            ON labrastro_sessions(fingerprint, saved_at DESC)
            WHERE deleted_at IS NULL AND has_history_content = TRUE
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_session_trace_events")
    op.execute("DROP TABLE IF EXISTS labrastro_session_snapshots")
    op.execute("DROP TABLE IF EXISTS labrastro_sessions")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_locks")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_cancel_requests")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_artifacts")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_sessions")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_claims")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_events")
    op.execute("DROP TABLE IF EXISTS labrastro_runtime_tasks")

