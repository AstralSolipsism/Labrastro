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
        CREATE TABLE IF NOT EXISTS labrastro_agent_runs (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'agent_run',
            owner_session_run_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            trigger_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            waiting_reason TEXT,
            resume_policy TEXT,
            runtime_profile_id TEXT,
            executor TEXT,
            execution_location TEXT,
            worktree_role TEXT,
            publish_policy TEXT,
            terminal_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            executor_session_id TEXT,
            current_activation_id TEXT,
            workdir TEXT,
            sandbox_id TEXT,
            sandbox_session_id TEXT,
            workspace_ref TEXT,
            retention_scope TEXT NOT NULL DEFAULT 'session',
            cleanup_policy TEXT NOT NULL DEFAULT 'delete_with_owner_session',
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
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_events (
            task_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
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
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_activations (
            id TEXT PRIMARY KEY,
            agent_run_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            seq INT NOT NULL,
            input_kind TEXT NOT NULL,
            input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            prompt TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            output TEXT,
            result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            worker_id TEXT,
            request_id TEXT,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(agent_run_id, seq)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_labrastro_agent_run_activations_active_run
            ON labrastro_agent_run_activations(agent_run_id)
            WHERE status IN ('queued', 'dispatched', 'running', 'waiting')
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_feedback (
            id TEXT PRIMARY KEY,
            agent_run_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            consumed_by_activation_id TEXT REFERENCES labrastro_agent_run_activations(id) ON DELETE SET NULL,
            visibility TEXT NOT NULL DEFAULT 'internal',
            requires_activation BOOLEAN NOT NULL DEFAULT FALSE,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_relations (
            id TEXT PRIMARY KEY,
            owner_agent_run_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            related_agent_run_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL,
            relation_scope TEXT NOT NULL DEFAULT 'session',
            created_by_activation_id TEXT REFERENCES labrastro_agent_run_activations(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'active',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(owner_agent_run_id, related_agent_run_id, relation_type)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_relations_owner
            ON labrastro_agent_run_relations(owner_agent_run_id, status)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_relations_related
            ON labrastro_agent_run_relations(related_agent_run_id, status)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_thread_bindings (
            id TEXT PRIMARY KEY,
            owner_session_run_id TEXT NOT NULL DEFAULT '',
            main_agent_run_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            target_agent_run_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            thread_key TEXT NOT NULL DEFAULT '',
            binding_lifetime TEXT NOT NULL DEFAULT 'session',
            workdir_policy TEXT NOT NULL DEFAULT 'inherit_main',
            visibility TEXT NOT NULL DEFAULT 'hidden_from_user_transcript',
            status TEXT NOT NULL DEFAULT 'active',
            cleanup_policy TEXT NOT NULL DEFAULT 'delete_with_owner_session',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_labrastro_agent_thread_bindings_active
            ON labrastro_agent_thread_bindings(
                owner_session_run_id, main_agent_run_id, agent_id, thread_key, binding_lifetime
            )
            WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_feedback_run_created
            ON labrastro_agent_run_feedback(agent_run_id, created_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_activation_claims (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            activation_id TEXT NOT NULL REFERENCES labrastro_agent_run_activations(id) ON DELETE CASCADE,
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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_labrastro_agent_run_activation_claims_active_task
            ON labrastro_agent_run_activation_claims(task_id)
            WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_labrastro_agent_run_activation_claims_active_activation
            ON labrastro_agent_run_activation_claims(activation_id)
            WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_sessions (
            task_id TEXT PRIMARY KEY REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            executor TEXT NOT NULL,
            execution_location TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_artifacts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
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
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_cancel_requests (
            task_id TEXT PRIMARY KEY REFERENCES labrastro_agent_runs(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            requested_by TEXT,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_locks (
            name TEXT PRIMARY KEY,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO labrastro_agent_run_locks(name)
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
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_runs_claim
            ON labrastro_agent_runs(status, created_at)
            WHERE status = 'queued'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_runs_status_updated
            ON labrastro_agent_runs(status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_runs_agent_status
            ON labrastro_agent_runs(agent_id, status)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_events_task_seq
            ON labrastro_agent_run_events(task_id, seq)
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
    op.execute("DROP TABLE IF EXISTS labrastro_sessions")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_locks")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_cancel_requests")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_artifacts")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_sessions")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_activation_claims")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_feedback")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_thread_bindings")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_relations")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_activations")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_events")
    op.execute("DROP TABLE IF EXISTS labrastro_agent_runs")

