# ADR 0002: Naming Boundary

## Status

Accepted.

## Context

Labrastro is built from the ReuleauxCoder codebase and still exposes several ReuleauxCoder-compatible names. Renaming all public entrypoints at once would break local workflows, extension integration, peer startup, and existing configuration paths.

At the same time, new backend control-plane code should use Labrastro naming so future modules do not expand the legacy name surface unnecessarily.

## Decision

The following names are public compatibility names and must remain stable unless a later ADR defines a compatibility window:

- Python distribution/package name: `reuleauxcoder`
- CLI command: `rcoder`
- Config directory: `.rcoder`
- Local peer artifact name: `rcoder-peer`
- Go worker directory and module: `reuleauxcoder-agent`
- Agent Runtime native executor id: `reuleauxcoder`
- Existing `X-RC-*` HTTP headers

The following names are Labrastro control-plane names and should be used for new backend-facing surfaces:

- Python control-plane package: `labrastro_server`
- Postgres tables and indexes: `labrastro_*`
- Environment variables: `LABRASTRO_*`
- Docker image/container defaults: `labrastro-host`
- Default database, user, and volume names: `labrastro`

Internal legacy names may remain where they are already coupled to existing modules, but they should not be used as the default naming source for new external APIs, tables, environment variables, or documentation.

## Consequences

- New backend modules should prefer `labrastro_*` naming for persistent and deploy-facing resources.
- Existing user-facing names such as `rcoder`, `.rcoder`, and `rcoder-peer` are compatibility commitments, not accidental leftovers.
- Renaming package names, CLI names, config directories, or peer artifact names requires a separate ADR and an explicit compatibility window.
