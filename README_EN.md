# Labrastro Backend Foundation

[中文](README.md)

This repository is the backend foundation for the Labrastro ecosystem. It is derived from the [RC-CHN/ReuleauxCoder](https://github.com/RC-CHN/ReuleauxCoder) fork lineage, keeps the ReuleauxCoder kernel boundary intact, and adds Labrastro-specific remote relay, session persistence, provider management, MCP distribution, environment manifests, Agent Runtime, and task control plane.

Repository: <https://github.com/AstralSolipsism/Labrastro>

## Naming Boundary

The upstream ReuleauxCoder boundary is preserved:

- Python kernel package: `reuleauxcoder`
- CLI: `rcoder`
- Config directory: `.rcoder`
- Local peer artifact: `rcoder-peer`
- Go worker directory and module: `reuleauxcoder-agent`
- Native Agent Runtime executor id: `reuleauxcoder`
- HTTP headers: `X-RC-*`

Labrastro-owned control-plane names use the new brand:

- Python control-plane package: `labrastro_server`
- Default Docker image/container: `labrastro-host`
- Default database name, user, and volume: `labrastro`
- Database environment variables: `LABRASTRO_DATABASE_URL`, `LABRASTRO_AUTO_MIGRATE`, `LABRASTRO_TEST_DATABASE_URL`

## Capabilities

- **Labrastro backend foundation** for remote sessions, model calls, task state, environment manifests, and tool execution entrypoints.
- **Remote Host/Peer relay** where the host runs as `rcoder --server` and peers join through bootstrap tokens.
- **Agent Runtime control plane** for runtime profiles, executors, models, MCP, skills, credentials, workspace policies, and approval boundaries.
- **Task and artifact lifecycle** for task, artifact, branch, PR, review, and follow-up states.
- **Server-side persistence** with file session storage plus Postgres migrations, runtime store, session store, and task state management.
- **Go worker execution surface** through `reuleauxcoder-agent` for CLI subprocesses, worktrees, repo cache, publishing, and long-running tasks.

## Deployment

Use Docker for a self-hosted Labrastro backend. Keep the source checkout and runtime state on persistent storage.

Recommended host layout:

```text
/data/labrastro/src              # git clone of this repository
/data/labrastro/config           # host config files, if compose volumes are customized
/data/labrastro/sessions         # persisted session state
/data/labrastro/mcp-artifacts    # server-hosted MCP artifacts
/data/labrastro/tools/npm-global # persistent post-installed npm CLIs
/data/labrastro/cache/npm        # persistent npm cache
/data/labrastro/home             # container HOME when needed
```

Basic deployment:

```bash
mkdir -p /data/labrastro
git clone https://github.com/AstralSolipsism/Labrastro.git /data/labrastro/src
cd /data/labrastro/src/docker
cp .env.example .env
```

Edit `.env` and set at least:

```text
RCODER_MODEL=
RCODER_BASE_URL=
RCODER_API_KEY=
LABRASTRO_AUTH_TOKEN_SECRET=
LABRASTRO_SUPERADMIN_USERNAME=admin
LABRASTRO_SUPERADMIN_PASSWORD_HASH=
```

Generate the password hash with `rcoder auth hash-password`, then place it in `LABRASTRO_SUPERADMIN_PASSWORD_HASH`.

Start the host:

```bash
docker compose up -d --build
docker compose logs -f labrastro-host
```

### Production Exposure

The expected production shape is to run Labrastro as an HTTP service inside the container and terminate HTTPS at the deployment layer, for example with Nginx, Caddy, Traefik, or Cloudflare:

```text
https://labrastro.example.com -> Nginx/Caddy -> labrastro-host:8765
```

This is the intended deployment model. Labrastro owns account authentication, authorization, token lifecycle, and audit behavior. TLS certificates, HSTS, DNS, public ports, firewall rules, IP allowlists, and reverse-proxy logs are deployment-layer responsibilities. Not listening for HTTPS directly inside the application does not make Remote Auth, Remote Relay / Peer, or the Admin control plane incomplete.

For Postgres-backed control-plane state:

```bash
cd /data/labrastro/src/docker
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

The config template reads `LABRASTRO_DATABASE_URL` for database connectivity.

## Remote Login

Configure remote relay and account authentication in `.rcoder/config.yaml` on the host:

```yaml
remote_exec:
  enabled: true
  host_mode: true
  relay_bind: 127.0.0.1:8765
  bootstrap_token_ttl_sec: 120
  peer_token_ttl_sec: 3600

auth:
  enabled: true
  token_secret: <long-random-secret>
  store_backend: postgres
  password_min_length: 6
  login_rate_limit_count: 5
  superadmins:
    - username: admin
      password_hash: <pbkdf2-password-hash>
```

`store_backend: postgres` uses Postgres tables
`labrastro_auth_users`, `labrastro_auth_devices`, `labrastro_auth_refresh_tokens`, and `labrastro_auth_audit_events`
The first administrator must come from backend config. The frontend does not initialize the system.

Generate and verify auth config:

```bash
uv run rcoder auth hash-password
uv run rcoder auth verify-config --config .rcoder/config.yaml
```

Start host mode:

```bash
rcoder --server
```

The VS Code extension connects with Host URL, username, and password. After login, it automatically requests one-time peer bootstrap tokens.

## Agent Runtime / Multi CLI Backend

The multi CLI execution backend runs on the server side or inside managed peer containers. The VS Code extension does not require local Codex, Claude, Gemini, or backend console access. CLI installation, provider login state, MCP credentials, and runtime HOME/config isolation are maintained by the deployment.

When a Go worker registers, it reports executor features to the Host, including `installed`, `stream_json`, `session_discovery`, `resume_by_id`, `mcp_config`, `runtime_home_isolation`, and `limitations`. `/remote/features` aggregates online peer features for the extension UI. Registration only performs fast installed detection and does not synchronously run external CLI `--version`; actual CLI versions should be recorded during deployment smoke tests and fixture upgrades.

Resume semantics are fixed: follow-up tasks continue the same CLI session only when executor, agent, runtime profile, workdir/branch, and `executor_session_id` all match. Retry defaults to a fresh run, and only explicit `resume_session=true` reuses the original session. Gemini keeps `resume_by_id=false` until a real resume fixture proves stable session extraction, so the UI shows fresh-run behavior.

Deployment smoke:

```bash
claude --version
gemini --version
codex --version
uv run pytest tests/labrastro_server/services/agent_runtime tests/labrastro_server/http
cd reuleauxcoder-agent && go test ./...
```

## Development

```bash
git clone https://github.com/AstralSolipsism/Labrastro.git
cd Labrastro
uv sync
uv run rcoder --version
uv run rcoder --server
```

Run tests:

```powershell
uv run pytest -q

cd reuleauxcoder-agent
go test ./...
```

## License

AGPL-3.0-or-later
