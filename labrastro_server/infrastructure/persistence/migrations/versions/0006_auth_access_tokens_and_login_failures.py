"""Persist auth access tokens and login failure windows."""

from __future__ import annotations

from alembic import op

revision = "0006_auth_access_tokens_and_login_failures"
down_revision = "0005_auth_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_auth_access_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES labrastro_auth_users(id) ON DELETE CASCADE,
            device_id TEXT NOT NULL REFERENCES labrastro_auth_devices(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at DOUBLE PRECISION NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            revoked_at DOUBLE PRECISION
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_auth_access_tokens_user_device
            ON labrastro_auth_access_tokens(user_id, device_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_auth_access_tokens_revoked
            ON labrastro_auth_access_tokens(revoked_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_auth_access_tokens_expires
            ON labrastro_auth_access_tokens(expires_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS labrastro_auth_login_failures (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            source TEXT NOT NULL,
            failed_at DOUBLE PRECISION NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_auth_login_failures_window
            ON labrastro_auth_login_failures(lower(username), source, failed_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS labrastro_auth_login_failures")
    op.execute("DROP TABLE IF EXISTS labrastro_auth_access_tokens")
