# ADR 0003: Capability, Environment Requirement, and MCP Boundary

## Status

Accepted.

## Context

The project is still in development. Capability package data has no historical compatibility burden, and backend semantics should be made explicit before the frontend is rebuilt.

The capability package model is moving away from a `CLI / MCP / Skill` split toward capability packages, contributions, and environment requirements. This ADR defines the backend boundary for the cleanup pass.

## Decisions

### Capability Package

A capability package is an installable capability unit. It declares contributions and metadata. It does not decide which Agent uses it.

The only effective Agent-to-package binding is:

```yaml
agent_registry:
  agents:
    <agent_id>:
      capability_refs:
        - <package_id>
```

The following concepts are invalid and must be removed:

- `suggested_agent_bindings`
- `AgentCapabilityBinding`
- any packager prompt/schema/test/doc text that says installing a package suggests attaching it to an Agent

### Capability Contributions

Capability package contributions install into their owning backend subsystem:

```text
environment_requirement -> environment.requirements
mcp_server              -> mcp.servers
builtin_tool            -> capability components / builtin grants
skill                   -> skills.items
prompt_fragment         -> prompt fragments
credential              -> credential refs
```

The installer must dispatch by contribution kind. MCP servers must not be stored through environment requirement paths.

Skill contributions materialize into `skills.items` with `path_hint` / `source_path`.
At runtime, `SkillsService.extra_paths` consumes those paths as explicit Skill roots or `SKILL.md` files.

### Environment Requirement

An environment requirement describes a runtime condition needed by an Agent, skill, MCP server, or package.

Canonical ID:

```text
envreq:<kind>:<name>
```

Accepted kinds:

```text
executable
runtime
sdk
service
env_var
credential
path
project_file
container
```

Placement:

```text
server | peer | both
```

`local` is not a canonical placement. The system has two execution locations: the host/server process and the remote peer.

Environment requirement command fields:

```text
command
check
install
configure
```

All executable command fields generated from a capability package draft must have evidence. Some requirement kinds can have no command fields, such as `credential`, `env_var`, `path`, `project_file`, and `container`; they still appear in environment manifests and produce no `allowed_commands`.

Environment requirement parsing belongs in one helper module:

```text
reuleauxcoder/domain/environment_requirements.py
```

### MCP Server

An MCP server is a capability contribution and belongs to the MCP subsystem:

```text
contributions.mcp_servers -> mcp.servers
```

An MCP server is not an environment requirement. Its runtime dependencies are environment requirements.

MCP server placement uses:

```text
server | peer | both
```

MCP server distribution uses:

```text
command | artifact
```

MCP server runtime prerequisites should be expressed with `environment_requirement_refs`, not a raw `requirements` map.

### Remote Protocols

Environment and MCP use separate peer protocols.

Environment protocol:

```text
POST /remote/environment/manifest
```

Returns only `environment_requirements`.

MCP peer protocol:

```text
POST /remote/mcp/manifest
GET  /remote/mcp/artifacts/{artifact_path}
POST /remote/mcp/tools
```

`/remote/mcp/manifest` returns only peer-runnable MCP servers:

```text
placement in {"peer", "both"}
```

Server-runnable MCP servers are started by the host runner from:

```text
placement in {"server", "both"}
```

### Admin API Naming

`toolchain(s)` is not a valid backend concept for this area.

Split the old admin toolchain surface into explicit resources:

```text
admin.environment_requirements.*
admin.mcp_servers.*
admin.skills.*
```

No old `toolchains` route, registry endpoint id, action id, response field, test name, or error code should remain for environment/MCP/Skill management.

## Acceptance Criteria

- No `suggested_agent_bindings`.
- No `AgentCapabilityBinding`.
- Agent/package binding is only `agent.capability_refs`.
- Environment requirement ID is always `envreq:<kind>:<name>`.
- Environment requirement placement is only `server | peer | both`.
- Requirement kind and command-field constants have one authoritative definition.
- `configure` command evidence is validated.
- No-command requirements appear in `/remote/environment/manifest`.
- `/remote/environment/manifest` does not include MCP servers.
- Peer MCP startup uses only `/remote/mcp/*`.
- `EnvironmentMCPServerManifest` is removed.
- `mcp.servers[].requirements` is replaced by `environment_requirement_refs`.
- Skill has first-class admin APIs: `list`, `dashboard`, `record`, `delete`, and `enable`.
- Capability package deletion only removes env/MCP/Skill resources with `managed_by=capability_package` and no remaining package references.
- User-managed MCP/Skill resources are not deleted when a capability package is deleted.
- Backend admin/API naming no longer uses `toolchain(s)` for environment/MCP/Skill management.
