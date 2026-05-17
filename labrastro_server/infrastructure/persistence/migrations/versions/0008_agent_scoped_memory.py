"""Add agent-scoped private memory tables."""

from __future__ import annotations

from alembic import op

revision = "0008_agent_scoped_memory"
down_revision = "0007_agent_run_event_retention_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            type TEXT NOT NULL,
            abstract TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            fields JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            version INT NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            project_id TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT '',
            repo_id TEXT NOT NULL DEFAULT '',
            goal_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_item_versions (
            version_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            version INT NOT NULL,
            content TEXT NOT NULL,
            abstract TEXT NOT NULL DEFAULT '',
            fields JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            item_id TEXT PRIMARY KEY REFERENCES memory_items(id) ON DELETE CASCADE,
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            embedding_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN undefined_file OR feature_not_supported THEN
            RAISE NOTICE 'pgvector extension is unavailable; memory_embeddings.embedding will be omitted';
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vector') THEN
                ALTER TABLE memory_embeddings
                    ADD COLUMN IF NOT EXISTS embedding vector(1536);
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_sources (
            source_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            source JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_access_events (
            access_id TEXT PRIMARY KEY,
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            item_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
            query TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_capture_jobs (
            job_id TEXT PRIMARY KEY,
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_scope_versions (
            owner_agent_id TEXT NOT NULL,
            memory_namespace TEXT NOT NULL,
            version INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_agent_id, memory_namespace)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_scope_status_updated
            ON memory_items(owner_agent_id, memory_namespace, status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_items_scope_project
            ON memory_items(owner_agent_id, memory_namespace, project_id, workspace_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_item_versions_scope
            ON memory_item_versions(owner_agent_id, memory_namespace, item_id, version DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_access_events_scope
            ON memory_access_events(owner_agent_id, memory_namespace, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_capture_jobs_scope
            ON memory_capture_jobs(owner_agent_id, memory_namespace, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_capture_jobs_scope_idempotency
            ON memory_capture_jobs(owner_agent_id, memory_namespace, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vector')
               AND EXISTS (SELECT 1 FROM pg_am WHERE amname = 'hnsw')
               AND EXISTS (SELECT 1 FROM pg_opclass WHERE opcname = 'vector_cosine_ops') THEN
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_scope_hnsw
                    ON memory_embeddings
                    USING hnsw (embedding vector_cosine_ops)
                    WHERE embedding IS NOT NULL;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_embeddings_scope_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_memory_capture_jobs_scope_idempotency")
    op.execute("DROP INDEX IF EXISTS idx_memory_capture_jobs_scope")
    op.execute("DROP INDEX IF EXISTS idx_memory_access_events_scope")
    op.execute("DROP INDEX IF EXISTS idx_memory_item_versions_scope")
    op.execute("DROP INDEX IF EXISTS idx_memory_items_scope_project")
    op.execute("DROP INDEX IF EXISTS idx_memory_items_scope_status_updated")
    op.execute("DROP TABLE IF EXISTS memory_scope_versions")
    op.execute("DROP TABLE IF EXISTS memory_capture_jobs")
    op.execute("DROP TABLE IF EXISTS memory_access_events")
    op.execute("DROP TABLE IF EXISTS memory_sources")
    op.execute("DROP TABLE IF EXISTS memory_embeddings")
    op.execute("DROP TABLE IF EXISTS memory_item_versions")
    op.execute("DROP TABLE IF EXISTS memory_items")
