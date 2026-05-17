"""Replace UI snapshots with authoritative session documents."""

from __future__ import annotations

from alembic import op

revision = "0011_session_documents"
down_revision = "0010_session_event_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_session_snapshots")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_session_documents (
            session_id TEXT PRIMARY KEY REFERENCES labrastro_sessions(id) ON DELETE CASCADE,
            document JSONB NOT NULL DEFAULT '{}'::jsonb,
            revision BIGINT NOT NULL DEFAULT 0,
            last_event_seq BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_session_documents_updated
            ON labrastro_session_documents(updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_labrastro_session_documents_updated")
    op.execute("DROP TABLE IF EXISTS labrastro_session_documents")
