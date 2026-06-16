from __future__ import annotations

from alembic import op

revision = "0014_agent_run_activation_steers"
down_revision = "0013_agent_run_artifact_sequence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_run_activation_steers (
            id TEXT PRIMARY KEY,
            activation_id TEXT NOT NULL REFERENCES labrastro_agent_run_activations(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            delivered_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'queued',
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_activation_steers_activation
            ON labrastro_agent_run_activation_steers(activation_id, created_at)
        """
    )

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_agent_run_activation_steers")
