"""Add snapshot retention and compression support."""

from __future__ import annotations

from alembic import op

revision = "0007_snapshot_retention_and_compression"
down_revision = "0006_auth_access_tokens_and_login_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            ALTER COLUMN snapshot DROP NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            ADD COLUMN IF NOT EXISTS snapshot_blob BYTEA
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            ADD COLUMN IF NOT EXISTS snapshot_encoding TEXT NOT NULL DEFAULT 'jsonb'
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            ADD COLUMN IF NOT EXISTS snapshot_bytes INT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE labrastro_session_snapshots
        SET snapshot_bytes = octet_length(snapshot::text)
        WHERE snapshot_bytes = 0 AND snapshot IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_session_snapshots_session_version
            ON labrastro_session_snapshots(session_id, version DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_session_snapshots_created_at
            ON labrastro_session_snapshots(created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_runtime_events_created_at
            ON labrastro_runtime_events(created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_labrastro_runtime_events_created_at")
    op.execute("DROP INDEX IF EXISTS idx_labrastro_session_snapshots_created_at")
    op.execute("DROP INDEX IF EXISTS idx_labrastro_session_snapshots_session_version")
    op.execute(
        """
        UPDATE labrastro_session_snapshots
        SET snapshot = '{}'::jsonb
        WHERE snapshot IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            ALTER COLUMN snapshot SET NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            DROP COLUMN IF EXISTS snapshot_bytes
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            DROP COLUMN IF EXISTS snapshot_encoding
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            DROP COLUMN IF EXISTS snapshot_blob
        """
    )
