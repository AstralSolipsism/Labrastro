from __future__ import annotations

from alembic import op

revision = "0016_agent_run_feedback_requires_activation"
down_revision = "0015_agent_call_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_feedback
        ADD COLUMN IF NOT EXISTS requires_activation BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_feedback
        DROP COLUMN IF EXISTS requires_activation
        """
    )
