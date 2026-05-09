"""Create GitHub pull request lifecycle tables."""

from __future__ import annotations

from alembic import op

revision = "0004_github_pr_lifecycle"
down_revision = "0003_issue_assignment_mention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_github_pull_requests (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            artifact_id TEXT REFERENCES labrastro_runtime_artifacts(id) ON DELETE SET NULL,
            repository TEXT NOT NULL,
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            number INTEGER NOT NULL,
            node_id TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            api_url TEXT NOT NULL DEFAULT '',
            base_ref TEXT NOT NULL DEFAULT '',
            head_ref TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            review_state TEXT NOT NULL DEFAULT 'none',
            merge_status TEXT NOT NULL DEFAULT 'pending_user',
            draft BOOLEAN NOT NULL DEFAULT false,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_synced_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (repository, number)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_github_review_comments (
            id TEXT PRIMARY KEY,
            github_id TEXT NOT NULL UNIQUE,
            pr_record_id TEXT NOT NULL
                REFERENCES labrastro_github_pull_requests(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES labrastro_runtime_tasks(id) ON DELETE CASCADE,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            path TEXT,
            line INTEGER,
            side TEXT,
            url TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open',
            work_item_id TEXT,
            assignment_id TEXT REFERENCES labrastro_assignments(id) ON DELETE SET NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_github_webhook_deliveries (
            delivery_id TEXT PRIMARY KEY,
            event TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'processing',
            error TEXT NOT NULL DEFAULT '',
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_github_pull_requests_task
            ON labrastro_github_pull_requests(task_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_github_pull_requests_status
            ON labrastro_github_pull_requests(status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_github_review_comments_task
            ON labrastro_github_review_comments(task_id, created_at ASC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_github_review_comments_pr
            ON labrastro_github_review_comments(pr_record_id, created_at ASC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_github_webhook_deliveries")
    op.execute("DROP TABLE IF EXISTS labrastro_github_review_comments")
    op.execute("DROP TABLE IF EXISTS labrastro_github_pull_requests")
