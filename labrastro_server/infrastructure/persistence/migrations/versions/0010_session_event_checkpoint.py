from __future__ import annotations

from alembic import op

revision = "0010_session_event_checkpoint"
down_revision = "0009_taskflow_event_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            ADD COLUMN IF NOT EXISTS event_seq BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ALTER COLUMN payload DROP NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS chat_id TEXT
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS chat_seq BIGINT
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'remote_chat'
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS replayable BOOLEAN NOT NULL DEFAULT TRUE
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS payload_blob BYTEA
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS payload_encoding TEXT NOT NULL DEFAULT 'jsonb'
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ADD COLUMN IF NOT EXISTS payload_bytes INT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE labrastro_session_trace_events
        SET payload_bytes = octet_length(payload::text)
        WHERE payload_bytes = 0 AND payload IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_session_trace_events_session_seq
            ON labrastro_session_trace_events(session_id, seq)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_session_trace_events_created_at
            ON labrastro_session_trace_events(created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_labrastro_session_trace_events_created_at")
    op.execute("DROP INDEX IF EXISTS idx_labrastro_session_trace_events_session_seq")
    op.execute(
        """
        UPDATE labrastro_session_trace_events
        SET payload = '{}'::jsonb
        WHERE payload IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            ALTER COLUMN payload SET NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS payload_bytes
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS payload_encoding
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS payload_blob
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS replayable
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS source
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS chat_seq
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_trace_events
            DROP COLUMN IF EXISTS chat_id
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_session_snapshots
            DROP COLUMN IF EXISTS event_seq
        """
    )
