from __future__ import annotations

from alembic import op

revision = "0015_agent_call_grants"
down_revision = "0014_agent_run_activation_steers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_agent_call_grants (
            user_id TEXT NOT NULL,
            grant_scope TEXT NOT NULL,
            main_agent_id TEXT NOT NULL,
            target_agent_id TEXT NOT NULL,
            conversation_scope TEXT NOT NULL,
            capability_scope_hash TEXT NOT NULL,
            capability_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
            target_config_version TEXT NOT NULL DEFAULT '',
            granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (
                user_id, grant_scope, main_agent_id, target_agent_id, conversation_scope,
                capability_scope_hash, target_config_version
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_call_grants_active
            ON labrastro_agent_call_grants(
                user_id, grant_scope, main_agent_id, target_agent_id, conversation_scope
            )
            WHERE revoked_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_agent_call_grants")
