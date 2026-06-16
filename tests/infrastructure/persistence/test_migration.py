from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from labrastro_server.infrastructure.persistence import migration


class _FakeConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        self.statements.append(str(statement))


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def begin(self):
        return self

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_run_migrations_widens_alembic_version_column(monkeypatch) -> None:
    connection = _FakeConnection()
    engine = _FakeEngine(connection)

    monkeypatch.setattr("sqlalchemy.create_engine", lambda *args, **kwargs: engine)

    migration._ensure_alembic_version_capacity("postgresql://user:pass@host/db")

    statements = "\n".join(connection.statements)
    assert "CREATE TABLE IF NOT EXISTS alembic_version" in statements
    assert "VARCHAR(255)" in statements
    assert "ALTER TABLE alembic_version" in statements
    assert "ALTER COLUMN version_num TYPE VARCHAR(255)" in statements


def test_agent_run_activation_schema_is_folded_into_initial_baseline() -> None:
    versions_dir = (
        Path(__file__).resolve().parents[3]
        / "labrastro_server"
        / "infrastructure"
        / "persistence"
        / "migrations"
        / "versions"
    )
    baseline_sql = (versions_dir / "0001_postgres_control_plane.py").read_text(
        encoding="utf-8"
    )
    next_revision = (versions_dir / "0002_taskflow_control_plane.py").read_text(
        encoding="utf-8"
    )

    for removed_incremental in (
        "0014_agent_run_activation_steers.py",
        "0015_agent_call_grants.py",
        "0016_agent_run_feedback_requires_activation.py",
    ):
        assert not (versions_dir / removed_incremental).exists()

    assert 'down_revision = "0001_postgres_control_plane"' in next_revision
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_run_activations" in baseline_sql
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_run_feedback" in baseline_sql
    assert "requires_activation BOOLEAN NOT NULL DEFAULT FALSE" in baseline_sql
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_run_relations" in baseline_sql
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_thread_bindings" in baseline_sql
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_run_activation_claims" in baseline_sql
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_run_activation_steers" in baseline_sql
    assert "CREATE TABLE IF NOT EXISTS labrastro_agent_call_grants" in baseline_sql
