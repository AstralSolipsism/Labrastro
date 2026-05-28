# ADR 0005: Memory Provider Contract Boundary

## Status

Proposed.

## Context

The project is still in development. The memory module has no compatibility
burden for old configuration or old development data. Architecture clarity is
more important than keeping transitional `memory.backend` behavior.

The current memory implementation exposes `MemoryProvider`, but that class is a
repository facade over SQLite or Postgres repositories. Hooks construct the
repository directly from `memory.backend`, which makes the hook layer know about
database storage and makes SQLite/Postgres look like memory providers.

This is the wrong boundary. A memory provider is a runtime memory capability
adapter. SQLite, Postgres, Markdown files, REST services, MCP servers, and SDKs
are implementation or transport choices behind adapters. Core memory code must
not depend on any built-in provider or any project-owned memory database schema.

This ADR applies only to the memory module. Postgres remains valid and required
for other Labrastro control-plane areas such as auth, session documents,
AgentRun, Taskflow, GitHub, collaboration, and future service persistence.

## References

The target model combines lessons from several projects:

- agentmemory: memory can be an independent shared service reached through REST,
  MCP, hooks, or native plugins. It should be treated as an external provider
  capability, not as a database backend.
- Hermes Agent: memory integration needs provider lifecycle events,
  capabilities, and plugin-like adapters.
- PilotDeck: memory injection belongs in context preparation, with diagnostics
  and fail-open behavior managed by the runtime.
- OpenHuman: memory orchestration, source sync, storage primitives, retrieval
  trees, and agent-visible tools are separate layers.

These references map to this design as follows:

- agentmemory informs the provider-as-service boundary, REST-first provider
  adapters, multi-agent scope mapping, and the rule that MCP memory can be a
  provider adapter without becoming an agent-visible tool.
- Hermes Agent informs the provider registry shape: a memory provider is loaded
  through an adapter, declares capabilities, and participates in lifecycle
  events instead of being hard-coded into hooks.
- PilotDeck informs the runtime boundary: memory injection is part of context
  preparation, diagnostics are first-class, and provider failure handling belongs
  in one runtime policy.
- OpenHuman informs the source/tool split: external sources, storage, retrieval,
  and explicit memory tools are separate concerns and must not collapse into a
  single provider abstraction.

## Product Rules

- Core memory does not ship a default provider.
- Core memory does not create SQLite or Postgres repositories.
- Core memory does not require project-owned memory tables.
- All memory capabilities are installed or registered as provider adapters.
- A provider adapter supplied by this project has the same status as a third
  party adapter. It is not special because it ships with the repository.
- `memory.backend` and `memory.store_path` are invalid target concepts.
- `sqlite`, `postgres`, and `memory` are not valid target values for a core
  memory backend.
- If `memory.enabled=true`, a configured `default_provider` must exist and its
  adapter must be registered.
- If no provider is configured, memory is disabled or configuration validation
  fails. There is no SQLite fallback.
- Automatic memory injection and agent-visible memory tools are separate
  surfaces.
- External source sync is separate from provider read/write.
- This ADR must not change non-memory Postgres persistence.

## Decisions

### Core Memory Boundary

Core memory owns only:

```text
MemoryProvider contract
MemoryRuntime
MemoryProviderRegistry
MemorySourceRegistry
MemoryToolSurface policy
MemoryScope / MemoryBundle / MemoryEvent models
```

Core memory must not own:

```text
SQLite memory provider
Postgres memory provider
Markdown memory provider
memory database schema as the provider contract
provider-specific prompt rendering
provider-specific source sync
```

### Provider Contract

Every provider adapter implements the same contract:

```python
class MemoryProvider:
    def health(scope): ...
    def provide(scope, request): ...
    def capture(scope, event): ...
    def remember(scope, item): ...
    def forget(scope, selector): ...
```

Providers declare capabilities:

```text
provide
capture
remember
forget
session_lifecycle
streaming_events
```

The runtime must check capabilities before invoking optional operations.

### Runtime

`MemoryRuntime` is the only layer that reads memory for model context or
captures runtime events.

It is responsible for:

- resolving Agent memory policy;
- selecting read and write providers;
- calling provider adapters;
- merging multi-provider results;
- applying token budgets;
- wrapping untrusted external content;
- producing diagnostics and UI trace data;
- preserving fail-open or fail-closed semantics.

Hooks, AgentRun, chat, and tool execution paths must call the runtime. They must
not construct providers or storage repositories.

### Provider Registry

Providers are configured by id and adapter name:

```yaml
memory:
  enabled: true
  default_provider: agentmemory

  providers:
    agentmemory:
      adapter: agentmemory_rest
      base_url: "http://127.0.0.1:3111"
      secret_env: AGENTMEMORY_SECRET
```

`providers.<id>.adapter` must resolve to an installed provider adapter. Unknown
adapters are configuration errors.

### Agent Memory Policy

Agents select how memory is used:

```yaml
agents:
  reviewer:
    memory:
      enabled: true
      primary_provider: agentmemory
      read_providers: [agentmemory]
      inject: true
      capture: true
      token_budget: 1200
      scope_mode: isolated
      expose_tools: false
```

`primary_provider` handles write-like operations. `read_providers` are merged by
the runtime.

### Source Connectors

Source connectors pull or watch external data and normalize it before writing to
a provider. They are not providers by default.

```yaml
memory:
  sources:
    github_project:
      adapter: github
      enabled: true
      target_provider: agentmemory
      sync_mode: scheduled
      interval_minutes: 30
      trust_tier: external
```

GitHub, Notion, Gmail, Obsidian, workspace folders, and MCP data drains belong
behind source connector adapters unless they directly implement the provider
contract.

### Tool Surface

Agent-visible memory tools are opt-in:

```yaml
memory:
  tools:
    enabled: false
    provider: agentmemory
    allowed_agents: [researcher]
    recall: true
    remember: true
    forget: false
    list: true
```

Automatic context injection must not make memory tools visible to the model.

### Legacy Memory Backend

The current SQLite/Postgres repository implementation is transitional code. It
must be removed from the core runtime path.

If a future SQLite, Postgres, or Markdown memory capability is useful, it must be
implemented as a provider adapter package and registered through the same
provider registry as any third party adapter.

Development data does not require migration. Remove invalid configuration and
tests instead of maintaining compatibility shims.

## Implementation Path

1. Add provider contract models for provider status, capabilities, provide
   request, bundle fragments, capture event, mutation result, diagnostics, and
   trace metadata.
2. Add `MemoryRuntime` and `MemoryProviderRegistry` with no built-in provider.
3. Change memory hooks to call `MemoryRuntime` instead of constructing
   `SQLiteMemoryRepository` or `PostgresMemoryRepository`.
4. Replace `memory.backend` / `memory.store_path` config with
   `memory.default_provider`, `memory.runtime`, `memory.providers`,
   `memory.sources`, and `memory.tools`.
5. Remove memory-specific SQLite/Postgres provider creation from core code.
6. Remove `memory.backend=memory` and any test or UI path that treats it as a
   real implementation.
7. Update frontend settings to manage provider adapter configuration rather than
   a backend dropdown.
8. Keep non-memory Postgres persistence untouched.
9. Add tests proving memory cannot silently fall back to SQLite/Postgres.

## Implementation Governance

Each implementation phase must follow the relevant ADR section:

- Contract and model work follows `Provider Contract`, `Core Memory Boundary`,
  and `Runtime`.
- Registry and adapter loading follows `Provider Registry`.
- Hook, AgentRun, chat, and tool-event plumbing follows `Runtime`.
- Agent-level configuration follows `Agent Memory Policy`.
- External data ingestion follows `Source Connectors`.
- Agent-visible recall or remember tools follow `Tool Surface`.
- Deleting SQLite/Postgres/Markdown memory assumptions follows `Legacy Memory
  Backend`.
- Frontend settings changes follow `Provider Registry`, `Agent Memory Policy`,
  `Source Connectors`, and `Tool Surface`.
- Test additions follow `Test Plan` and must prove the `Acceptance Criteria`.

Implementation must keep scope limited to memory. Any Postgres code used by
auth, session documents, AgentRun, Taskflow, GitHub, collaboration, or other
control-plane features is outside this ADR unless it imports the memory
provider/backend abstraction directly.

## Test Plan

- Config validation:
  - `memory.enabled=true` without `default_provider` fails;
  - unknown provider id fails;
  - unknown provider adapter fails;
  - `memory.backend`, `memory.store_path`, and backend values
    `sqlite/postgres/memory` are no longer accepted target config.
- Runtime tests:
  - hooks call `MemoryRuntime`;
  - hooks do not import SQLite/Postgres memory repositories;
  - provider capability checks guard optional operations;
  - provider failures obey `fail_mode`;
  - bundles are merged and token-budgeted by runtime;
  - external trust tier is wrapped before injection.
- Frontend tests:
  - settings no longer expose SQLite/Postgres/memory backend selection;
  - provider ids and adapter config are the visible model;
  - memory tools are configured separately from automatic injection.
- Static checks:
  - no core runtime path references `SQLiteMemoryRepository` or
    `PostgresMemoryRepository`;
  - no memory hook constructs storage;
  - non-memory Postgres persistence tests still pass.

## Acceptance Criteria

- Core memory has no default provider.
- Core memory does not create SQLite or Postgres memory repositories.
- `memory.backend` and `memory.store_path` are removed from target config and
  frontend settings.
- Enabling memory requires a valid `default_provider` and registered adapter.
- Missing provider configuration does not fall back to SQLite.
- `MemoryRuntime` is the only injection and capture orchestration layer.
- Provider adapters implement one contract and declare capabilities.
- Source connectors are separate from providers.
- Agent-visible memory tools are opt-in and separate from automatic injection.
- Postgres remains available for non-memory persistence.
- Tests cover no-fallback behavior and the new provider-registry path.
