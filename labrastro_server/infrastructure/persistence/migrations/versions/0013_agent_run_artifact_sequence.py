"""Add stable append order for AgentRun artifacts."""

from __future__ import annotations

from alembic import op

revision = "0013_agent_run_artifact_sequence"
down_revision = "0012_session_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE IF NOT EXISTS labrastro_agent_run_artifacts_artifact_seq_seq
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_artifacts
            ADD COLUMN IF NOT EXISTS artifact_seq BIGINT
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_artifacts
            ALTER COLUMN artifact_seq
            SET DEFAULT nextval('labrastro_agent_run_artifacts_artifact_seq_seq')
        """
    )
    op.execute(
        """
        WITH existing_sequence AS (
            SELECT COALESCE(max(artifact_seq), 0) AS max_seq
            FROM labrastro_agent_run_artifacts
        ),
        ordered_artifacts AS (
            SELECT
                artifact.id,
                existing_sequence.max_seq
                    + row_number() OVER (ORDER BY created_at ASC, id ASC)
                    AS next_artifact_seq
            FROM labrastro_agent_run_artifacts artifact
            CROSS JOIN existing_sequence
            WHERE artifact.artifact_seq IS NULL
        )
        UPDATE labrastro_agent_run_artifacts artifact
        SET artifact_seq = ordered_artifacts.next_artifact_seq
        FROM ordered_artifacts
        WHERE artifact.id = ordered_artifacts.id
        """
    )
    op.execute(
        """
        SELECT setval(
            'labrastro_agent_run_artifacts_artifact_seq_seq',
            GREATEST(
                COALESCE((SELECT max(artifact_seq) FROM labrastro_agent_run_artifacts), 0),
                1
            ),
            true
        )
        """
    )
    op.execute(
        """
        ALTER SEQUENCE labrastro_agent_run_artifacts_artifact_seq_seq
            OWNED BY labrastro_agent_run_artifacts.artifact_seq
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_artifacts
            ALTER COLUMN artifact_seq SET NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_labrastro_agent_run_artifacts_task_seq
            ON labrastro_agent_run_artifacts(task_id, artifact_seq)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_labrastro_agent_run_artifacts_task_seq")
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_artifacts
            ALTER COLUMN artifact_seq DROP DEFAULT
        """
    )
    op.execute(
        """
        ALTER TABLE labrastro_agent_run_artifacts
            DROP COLUMN IF EXISTS artifact_seq
        """
    )
    op.execute("DROP SEQUENCE IF EXISTS labrastro_agent_run_artifacts_artifact_seq_seq")
