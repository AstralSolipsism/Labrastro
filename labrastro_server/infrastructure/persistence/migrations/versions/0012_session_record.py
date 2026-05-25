"""Add unified session record storage."""

from __future__ import annotations

from alembic import op

revision = "0012_session_record"
down_revision = "0011_session_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE labrastro_sessions
            ADD COLUMN IF NOT EXISTS record JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        UPDATE labrastro_sessions sessions
        SET record = jsonb_build_object(
            'schema_version', 2,
            'metadata', jsonb_build_object(
                'id', sessions.id,
                'model', sessions.model,
                'saved_at', sessions.saved_at::text,
                'preview', sessions.preview,
                'fingerprint', sessions.fingerprint
            ),
            'runtime_state', COALESCE(sessions.runtime_state, '{}'::jsonb),
            'history', jsonb_build_object(
                'messages', COALESCE(sessions.messages, '[]'::jsonb),
                'active_mode', sessions.active_mode,
                'total_prompt_tokens', sessions.total_prompt_tokens,
                'total_completion_tokens', sessions.total_completion_tokens
            ),
            'transcript', documents.document,
            'events', '[]'::jsonb
        )
        FROM labrastro_session_documents documents
        WHERE documents.session_id = sessions.id
          AND COALESCE(sessions.record->>'schema_version', '') <> '2'
        """
    )
    op.execute(
        """
        UPDATE labrastro_sessions sessions
        SET record = jsonb_build_object(
            'schema_version', 2,
            'metadata', jsonb_build_object(
                'id', sessions.id,
                'model', sessions.model,
                'saved_at', sessions.saved_at::text,
                'preview', sessions.preview,
                'fingerprint', sessions.fingerprint
            ),
            'runtime_state', COALESCE(sessions.runtime_state, '{}'::jsonb),
            'history', jsonb_build_object(
                'messages', COALESCE(sessions.messages, '[]'::jsonb),
                'active_mode', sessions.active_mode,
                'total_prompt_tokens', sessions.total_prompt_tokens,
                'total_completion_tokens', sessions.total_completion_tokens
            ),
            'transcript', NULL,
            'events', '[]'::jsonb
        )
        WHERE COALESCE(sessions.record->>'schema_version', '') <> '2'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE labrastro_sessions
            DROP COLUMN IF EXISTS record
        """
    )
