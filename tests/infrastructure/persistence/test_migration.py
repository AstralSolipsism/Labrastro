from __future__ import annotations

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
