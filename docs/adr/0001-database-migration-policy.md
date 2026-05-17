# ADR 0001: Database Migration Policy

## Status

Accepted.

## Context

Labrastro uses optional Postgres persistence for runtime, auth, session, collaboration, GitHub PR lifecycle, and related control-plane state. Taskflow tables exist in the migration set, but Taskflow state store wiring is still pending; the current Taskflow service uses the in-memory state store unless a future wiring change introduces a Postgres implementation. The current persistence layer is written around explicit SQL statements rather than SQLAlchemy ORM models.

Alembic is configured with `target_metadata = None`. This is intentional: migrations are authored by hand as SQL migration files, and the project does not use SQLAlchemy autogenerate as the schema authority.

The current development and test phase has no valuable historical database data that must be preserved. Development databases may be dropped and recreated when schema work changes.

## Decision

- Continue using hand-written Alembic SQL migrations.
- Keep `target_metadata = None` in the Alembic environment.
- Do not introduce SQLAlchemy metadata/autogenerate for the current control-plane schema.
- Do not write compatibility migrations for disposable development/test data.
- Treat `0006_auth_access_tokens_and_login_failures` as the migration for persisted access tokens and login failure windows.
- Treat `0007_agent_run_event_retention_index` as the migration for runtime event retention indexes.
- Treat `0011_session_documents` as the migration for authoritative session documents.
- Treat existing Taskflow tables as schema groundwork only until a Postgres-backed Taskflow state store is wired into the application service.
- Keep peer token state, peer registry state, and pending relay queue state in process memory for the current single-instance target. P2 does not add peer session tables.

## Consequences

- Store implementations and migration files must stay aligned by review and tests rather than by ORM autogenerate.
- Empty-database migration smoke tests are the preferred future guard against schema drift.
- Development/test operators can rebuild Postgres instead of preserving old access tokens, login failure records, runtime events, session documents, claims, auth sessions, or peer state.
- A future production/beta data-retention commitment must add a separate migration policy update before breaking or squashing historical migrations.
