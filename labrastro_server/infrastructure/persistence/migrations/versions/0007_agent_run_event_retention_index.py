"""Add agent run event retention index."""

from __future__ import annotations

from alembic import op

revision = "0007_agent_run_event_retention_index"
down_revision = "0006_auth_access_tokens_and_login_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_events_created_at
            ON labrastro_agent_run_events(created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_labrastro_agent_run_events_created_at")
