"""Create remote auth control-plane tables."""

from __future__ import annotations

from alembic import op

revision = "0005_auth_control_plane"
down_revision = "0004_github_pr_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ez_auth_users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT true,
            configured BOOLEAN NOT NULL DEFAULT false,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            last_login_at DOUBLE PRECISION
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ez_auth_users_username_lower
            ON ez_auth_users (lower(username))
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ez_auth_devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES ez_auth_users(id) ON DELETE CASCADE,
            label TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL,
            last_seen_at DOUBLE PRECISION,
            revoked_at DOUBLE PRECISION
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ez_auth_devices_user
            ON ez_auth_devices(user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ez_auth_refresh_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES ez_auth_users(id) ON DELETE CASCADE,
            device_id TEXT NOT NULL REFERENCES ez_auth_devices(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at DOUBLE PRECISION NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            revoked_at DOUBLE PRECISION
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ez_auth_refresh_tokens_user_device
            ON ez_auth_refresh_tokens(user_id, device_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ez_auth_refresh_tokens_revoked
            ON ez_auth_refresh_tokens(revoked_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ez_auth_audit_events (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            device_id TEXT NOT NULL DEFAULT '',
            source_ip TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ez_auth_audit_events_created
            ON ez_auth_audit_events(created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ez_auth_audit_events_user
            ON ez_auth_audit_events(user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ez_auth_audit_events_type
            ON ez_auth_audit_events(type, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ez_auth_audit_events")
    op.execute("DROP TABLE IF EXISTS ez_auth_refresh_tokens")
    op.execute("DROP TABLE IF EXISTS ez_auth_devices")
    op.execute("DROP TABLE IF EXISTS ez_auth_users")
