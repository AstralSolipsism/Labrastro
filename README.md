# Labrastro Backend Foundation

[English](README_EN.md)

本仓库是 Labrastro 生态的后端基座，源自 [RC-CHN/ReuleauxCoder](https://github.com/RC-CHN/ReuleauxCoder) fork。项目保留 ReuleauxCoder 内核边界，同时新增面向 Labrastro 的远端 relay、会话持久化、Provider 管理、MCP 分发、环境清单、Agent Runtime 和任务控制面。

仓库地址：<https://github.com/AstralSolipsism/Labrastro>

## 命名边界

保留 ReuleauxCoder 上游命名：

- Python 内核包：`reuleauxcoder`
- CLI：`rcoder`
- 配置目录：`.rcoder`
- 本地 peer artifact：`rcoder-peer`
- Go worker 目录与 module：`reuleauxcoder-agent`
- Agent Runtime 原生执行器 id：`reuleauxcoder`
- HTTP header：`X-RC-*`

Labrastro 自有控制面使用新命名：

- Python 控制面包：`labrastro_server`
- Docker image/container 默认名：`labrastro-host`
- 数据库默认名、用户、volume：`labrastro`
- 数据库环境变量：`LABRASTRO_DATABASE_URL`、`LABRASTRO_AUTO_MIGRATE`、`LABRASTRO_TEST_DATABASE_URL`

## 能力概览

- **Labrastro 后端基座**：为 VS Code/Webview 入口提供远端会话、模型调用、任务状态、环境清单和工具执行入口。
- **远端 Host/Peer relay**：Host 以 `rcoder --server` 运行，peer 通过 bootstrap token 接入，把本地工作区、终端、MCP 和 IDE 能力暴露给当前会话。
- **平台化 Agent Runtime**：Agent 可以绑定 runtime profile、执行器、模型、MCP、skill、credential reference、工作区策略和审批边界。
- **任务与产物控制面**：支持 task、artifact、branch、PR、review/follow-up 等独立生命周期。
- **服务端持久化**：保留文件会话存储，同时引入 Postgres 控制面、迁移、runtime store、session store 和任务状态管理。
- **Go worker 执行面**：`reuleauxcoder-agent` 负责 CLI 子进程、worktree、执行环境、repo cache、publish 和长生命周期任务执行。

## 部署

推荐使用 Docker 部署自托管 Labrastro 后端。源码和运行状态应放在持久化目录中，不依赖容器内部临时文件保存配置、会话、MCP artifact 或后安装工具。

推荐宿主机目录：

```text
/data/labrastro/src              # 当前仓库 git clone
/data/labrastro/config           # host 配置文件；自定义 compose volume 时使用
/data/labrastro/sessions         # 持久化会话状态
/data/labrastro/mcp-artifacts    # 服务端托管的 MCP artifact
/data/labrastro/tools/npm-global # 持久化后安装的 npm CLI
/data/labrastro/cache/npm        # 持久化 npm cache
/data/labrastro/home             # 需要时作为容器 HOME
```

基础部署流程：

```bash
mkdir -p /data/labrastro
git clone https://github.com/AstralSolipsism/Labrastro.git /data/labrastro/src
cd /data/labrastro/src/docker
cp .env.example .env
```

编辑 `.env`，至少设置：

```text
RCODER_MODEL=
RCODER_BASE_URL=
RCODER_API_KEY=
LABRASTRO_AUTH_TOKEN_SECRET=
LABRASTRO_SUPERADMIN_USERNAME=admin
LABRASTRO_SUPERADMIN_PASSWORD_HASH=
```

密码哈希可用 `rcoder auth hash-password` 生成，并写入 `LABRASTRO_SUPERADMIN_PASSWORD_HASH`。

启动 host：

```bash
docker compose up -d --build
docker compose logs -f labrastro-host
```

默认 compose 会把宿主机 `../.rcoder` 挂载到容器 `/app/.rcoder`。生产环境必须确保这个宿主机目录位于持久化磁盘上；也可以在首次启动前把 volume 改成自己的 `/data/labrastro/config` 持久化路径。

### 生产暴露方式

生产环境推荐把 Labrastro 后端作为容器内 HTTP 服务运行，再由 Nginx、Caddy、Traefik 或 Cloudflare 等部署层组件终止 HTTPS 并反向代理到容器端口：

```text
https://labrastro.example.com -> Nginx/Caddy -> labrastro-host:8765
```

这是预期部署模型。Labrastro 应用内负责账号认证、权限、token 生命周期和审计；TLS 证书、HSTS、域名、公网端口、防火墙、IP allowlist 和反向代理日志属于部署层治理。未在应用内直接监听 HTTPS 不代表 Remote Auth、Remote Relay / Peer 或 Admin 控制面不完整。

### Postgres 控制面

需要数据库控制面时可使用仓库内 compose 文件：

```bash
cd /data/labrastro/src/docker
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

数据库迁移代码位于 `reuleauxcoder/infrastructure/persistence/migrations`。配置模板中的数据库连接读取 `LABRASTRO_DATABASE_URL`。

## 远端登录

先在 host 的 `.rcoder/config.yaml` 中配置 remote relay 和账号认证：

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
  store_backend: auto
  password_min_length: 10
  login_rate_limit_count: 5
  superadmins:
    - username: admin
      password_hash: <pbkdf2-password-hash>
```

`store_backend: auto` 会在 `persistence.database_url` 存在时使用 Postgres 表
`ez_auth_users`、`ez_auth_devices`、`ez_auth_refresh_tokens`、`ez_auth_audit_events`，
否则回退到文件存储 `.rcoder/auth.json`。首次管理员只能由后端配置提供；前端不做系统初始化。

生成和校验认证配置：

```bash
uv run rcoder auth hash-password
uv run rcoder auth verify-config --config .rcoder/config.yaml
```

启动 host：

```bash
rcoder --server
```

VS Code 插件连接时填写 Host URL、用户名和密码。插件登录后会自动申请一次性 peer bootstrap token，不需要用户手动执行 bootstrap 命令。

## Agent Runtime / Multi CLI backend

Agent Runtime 的多 CLI 执行底座运行在后端或受控 peer 容器中。用户插件不需要安装或登录 Codex、Claude、Gemini 等 CLI，也不需要后端控制台权限；CLI 安装、provider 登录态、MCP 凭据和 runtime HOME/config 隔离都由服务端部署维护。

Go worker 注册时会向 Host 上报 executor capability，包括 `installed`、`stream_json`、`session_discovery`、`resume_by_id`、`mcp_config`、`runtime_home_isolation` 和 `limitations`。`/remote/capabilities` 会汇总在线 peer 能力供插件展示。注册热路径只做快速 installed 探测，不同步运行外部 CLI `--version`；CLI 实际版本应在部署 smoke 和 fixture 升级记录中确认。

Resume 语义固定为：follow-up 在同 executor、同 agent、同 runtime profile、同 workdir/branch 且存在 `executor_session_id` 时继续同一 CLI 会话；retry 默认 fresh run，只有显式 `resume_session=true` 才复用原 session。Gemini 在真实 resume fixture 证明前保持 `resume_by_id=false`，前端会明确显示 fresh run 状态。

常用部署 smoke：

```bash
claude --version
gemini --version
codex --version
uv run pytest tests/labrastro_server/services/agent_runtime tests/labrastro_server/http
cd reuleauxcoder-agent && go test ./...
```

## 本地开发

```bash
git clone https://github.com/AstralSolipsism/Labrastro.git
cd Labrastro
uv sync
uv run rcoder --version
uv run rcoder --server
```

常用测试：

```powershell
uv run pytest -q

cd reuleauxcoder-agent
go test ./...
```

## 许可证

AGPL-3.0-or-later
