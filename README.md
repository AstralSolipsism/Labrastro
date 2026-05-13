# Labrastro：自托管的 AI 编程星图中枢

[English](README_EN.md)

本项目当前为自用导向，主要目标是解决我个人开发过程遇到的问题。

Labrastro 像一只守在代码星图旁的可靠伙伴：能嗅到项目里的线索，陪你探索想法如何落地，也能把模型、工具、仓库、运行环境和协作记录从你的服务器上寻回。

Labrastro 是中心化自托管 Host，也是 AI 编程协作入口。账号、Provider、MCP、Agent Runtime、远端 Peer、任务状态、产物、开发生命周期和项目级记忆都由服务端统一管理。用户可以从 VS Code/Webview、`rcoder` CLI 或受控 peer 进入同一套控制面，入口、配置、凭据和运行环境集中在 Host 上。

VS Code/Webview 入口位于工作区根目录的 `dogcode` 仓库，当前已经具备 MVP 基础，包括登录、settings、chat、session、AgentRun、peer 编排和 Taskflow chat mode。本仓库聚焦 ReuleauxCoder/Labrastro server：后端控制面、协议契约和执行基座。

仓库地址：<https://github.com/AstralSolipsism/Labrastro>

## Labrastro 是什么

Labrastro 是一个面向 AI 编程协作的自托管控制面。它继承 ReuleauxCoder 的本地 agent 内核和 CLI 兼容边界，同时在服务端增加 Labrastro 控制面，让团队或个人把 AI 编程运行时集中部署、集中审计、集中维护。

它关注的是一次真实开发任务从“想法”到“执行”的完整路径：

- 用户提出目标，系统保留上下文和会话状态。
- 服务端根据账号、权限、Provider、MCP 和工具链配置决定能用什么。
- Agent Runtime 把任务分派给合适的执行器、模型、工作区和运行环境。
- Taskflow 把模糊目标整理为可确认的范围、假设、决策、WorkItem 和 TaskRun。
- 任务执行过程中的产物、分支、PR、review 和 follow-up 被记录回同一套控制面。

Labrastro 把 AI 编程推进为可追踪、可恢复、可分派、可审计的项目工作流。

## 它能帮你做什么

- **把 AI 编程入口集中到自己的 Host**：Host/Runtime 集中维护 AI 执行器、Provider 登录态、凭据引用和 runtime HOME 隔离，部署者在服务端管理可用运行入口。
- **让远端工作区安全接入**：Remote Host/Peer relay 通过 bootstrap token 和 peer token 接入受控工作区，把本地终端、文件、MCP 和 IDE 能力暴露给当前会话。
- **统一管理模型与 Provider**：服务端保存模型 profile、Provider 配置、启用状态、测试结果和默认模型选择，让前端只呈现可用能力。
- **管理任务生命周期**：Agent Runtime 记录任务、事件、产物、分支、PR、review comment 和 follow-up，让长任务沉淀为可追踪状态。
- **把目标编译成可执行工作**：Taskflow 将一次目标会话整理成 ProjectState、TaskflowState、WorkItem 和 TaskRun，为复用项目记忆、减少重复规划、提升多人协作一致性打基础。
- **保留自托管控制权**：账号、token 生命周期、审计、Postgres 控制面、反向代理暴露方式和持久化目录都由部署者掌握。

## 一次任务在 Labrastro 里怎么发生

1. 用户登录自托管 Host，前端自动申请一次性 peer bootstrap token。
2. 受控 peer 或服务端 worker 注册工作区、可用执行器、MCP、skills、环境 CLI 和 capability 清单，以及运行时限制。
3. 用户发起会话、Issue assignment、mention 或后台任务。
4. Labrastro 根据 runtime profile 选择执行位置、执行器、模型、capability 集合、凭据引用和审批边界。
5. Agent Runtime 创建任务并持续记录事件、产物和状态。
6. 如果任务进入 Taskflow，系统会先澄清目标、展示可确认的假设/范围/决策，再编译为 WorkItem 和 TaskRun。
7. 执行结果、PR、review comment、follow-up assignment 和后续任务继续回流到同一套项目状态里。

这个流程让 AI 成为可托管、可观察、可恢复的项目成员。

这里的几个概念有明确边界：`runtime profile` 是运行配置组合，描述执行位置、模型、审批、HOME 隔离、凭据引用和 MCP 配置；`执行器 / executor` 决定 Agent 如何调用 AI 并承载一次任务会话，例如 `reuleauxcoder`、Codex、Claude、Gemini；`CLI` 服务于 skills、环境检查和工具链；`capability` 是 MCP、skills、环境能力等可声明、可路由、可展示的能力集合。

## 当前已经实现

当前仓库已经具备以下基础能力：

- **Remote Host/Peer relay**：`rcoder --server` 可运行 Host；peer 通过 bootstrap token 接入，支持注册、心跳、poll、结果回传、远端 chat、审批回复和能力上报。
- **远端登录与账号控制面**：支持账号登录、refresh token、设备、审计、管理员配置和 Postgres auth store。
- **Provider 与模型管理**：服务端可管理 Provider、模型 profile、启用状态、模型列表和连接测试。
- **MCP 与环境清单**：支持 server/peer/both placement，服务端托管 MCP artifact，并向 peer 下发环境 manifest。
- **Agent Runtime**：支持 runtime profile、agent/capability 配置、任务提交、事件流、取消、重试、claim/heartbeat/complete、artifact、branch、PR 和 review/follow-up 相关状态。
- **Go worker 执行面**：`reuleauxcoder-agent` 负责 CLI 子进程、worktree、执行环境、repo cache、publish 和长生命周期任务执行。
- **VS Code/Webview MVP**：根目录 `dogcode` 提供插件入口，已覆盖登录、settings、chat、session、AgentRun、peer 编排和 Taskflow chat mode。
- **Postgres 控制面基础**：包含手写 Alembic 迁移、runtime/session/auth/collaboration/GitHub PR 生命周期相关 store，以及 Taskflow 表结构。Taskflow store wiring 仍在建设中。
- **Taskflow 核心骨架**：已实现 `ProjectState`、`TaskflowState`、复杂度估算、SBE/BDD 场景与验收示例字段、DDD-lite 领域增量、Review Card projection、PlanCompiler、WorkItem 复用/创建、GoalWorkLink、TraceLink、TaskRun 派发端口和 `/remote/taskflow/taskflows/...` HTTP 路由。

## Taskflow：从一次想法到可执行工作

Taskflow 是 Labrastro 里正在建设的项目记忆与任务编译层。它把“帮我做这个”拆成几个可确认的中间层：

- **ProjectState**：长期项目状态，保存背景知识、术语、约束、决策、可复用 WorkItem、Goal/WorkItem 关系、TraceLink 和投影产物。
- **TaskflowState**：一次目标会话的编译快照，保存目标、范围、假设、开放问题、方案、风险、接口、验收线索、候选 WorkItem 和 readiness gate。
- **PlanCompiler**：读取 TaskflowState 与 ProjectState，决定创建还是复用 WorkItem，并生成 GoalWorkLink、decision trace 和 acceptance trace。
- **WorkItem**：可复用的工作定义，表达“要做什么”。
- **TaskRun**：一次具体执行实例，表达“这次由谁、在什么运行时、以什么上下文去执行”。

Taskflow 把成熟工程方法压缩成可确认、可追踪、可复用的状态：

- **SBE（Specification by Example）**：用具体示例澄清规格和边界，让目标进入具体示例、边界和验收语境。
- **BDD（Behavior-Driven Development）**：把用户可观察行为整理成 Given/When/Then 场景和验收线索。
- **SDD（Specification-Driven Development，规格驱动开发）**：用规格固定目标、接口、非功能约束和产物投影，让任务具备稳定上下文。
- **TDD（Test-Driven Development）**：让 WorkItem 和 TaskRun 携带可验证的测试义务、scenario refs 和 acceptance refs。
- **DDD（Domain-Driven Design）**：把术语、领域模型、边界上下文、约束和长期决策沉淀到 ProjectState。

这些方法最终落在工程产物上。Labrastro 让工程产物以 TaskflowState、ProjectState、TraceLink 和 artifact projection 的形式落地：

| 阶段 | 典型产物 | Taskflow 中的落点 |
| --- | --- | --- |
| 需求阶段 | PRD | 提出业务问题、目标、范围和成功标准 |
| 提案阶段 | RFC / Design Doc | 论证多种方案、取舍、风险和开放问题 |
| 决策阶段 | ADR | 记录为什么选择方案 A，并形成 decision trace |
| 实现阶段 | Tech Spec / HLD / LLD | 拆解 WorkItem、依赖、实现约束和执行上下文 |
| 接口阶段 | API Spec | 固化 API、事件、数据结构等交互契约 |
| 运维阶段 | Runbook / Playbook | 指导部署、恢复、回滚、巡检和日常操作 |
| 复盘阶段 | Postmortem / Retrospective | 验证决策，记录事故、偏差和改进 |
| 治理阶段 | Tech Radar / Tech Debt Register | 宏观跟踪技术演进、债务和后续投资 |

当前 Taskflow 已有服务端状态模型、编译流程、派发端口和 HTTP 路由骨架，`dogcode` 侧已有 Taskflow chat mode 基础入口。完整产品化体验仍在建设中，包括 Review Cards 交互、自然澄清流程、Taskflow 详情页、项目级长期记忆可视化、Postgres-backed Taskflow store wiring，以及工程产物自动生成/回写。

## 中心化自托管部署

推荐使用 Docker 部署 Labrastro Host，并把源码、配置、会话、MCP artifact、工具缓存和 HOME 放在持久化目录中。典型目录：

```text
/data/labrastro/src              # 当前仓库 git clone
/data/labrastro/config           # host 配置文件；自定义 compose volume 时使用
/data/labrastro/sessions         # 持久化会话状态
/data/labrastro/mcp-artifacts    # 服务端托管的 MCP artifact
/data/labrastro/tools/npm-global # 持久化后安装的 npm CLI
/data/labrastro/cache/npm        # 持久化 npm cache
/data/labrastro/home             # 需要时作为容器 HOME
```

基础部署：

```bash
mkdir -p /data/labrastro
git clone https://github.com/AstralSolipsism/Labrastro.git /data/labrastro/src
cd /data/labrastro/src/docker
cp .env.example .env
```

`.env` 至少需要配置：

```text
LABRASTRO_AUTH_TOKEN_SECRET=
LABRASTRO_SUPERADMIN_USERNAME=admin
LABRASTRO_SUPERADMIN_PASSWORD_HASH=
LABRASTRO_SANDBOX_HOST_BASE_URL=http://labrastro-host:8765
```

密码哈希可通过 `rcoder auth hash-password` 生成；写入 Docker `.env` 时需要把 hash 里的 `$` 写成 `$$`，避免 Compose 当作变量插值。模型 Provider 与模型 Profile 可在前端 Admin 配置中维护，不需要作为 Docker 启动必填项。启动 Host：

```bash
docker compose up -d --build
docker compose logs -f labrastro-host
```

基础 compose 默认是无数据库兼容模式：`LABRASTRO_DATABASE_URL` 可以留空，`docker compose up -d --build` 仍应能启动。此模式适合本地、开发和单实例试用，但会有明确降级：

- Auth 使用 file store，依赖 `.rcoder` volume 保存账号和 refresh token。
- Session 使用文件 store，依赖 `.rcoder` / session volume 保存会话快照。
- AgentRun、Taskflow、ProjectState、Issue、Assignment、Mention 在当前实现中会使用进程内状态或能力降级，重启后不可恢复。
- GitHub PR lifecycle 和 review follow-up 需要 Postgres。
- peer registry、peer token、relay pending queue 仍是单实例内存态。
- Postgres overlay 当前不等于完整 Taskflow 生产持久化；Taskflow 表已存在，但 Taskflow store wiring 尚未完成。

需要 Postgres 控制面时：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

启用 Postgres 后，已 wiring 的 runtime/session/auth/collaboration/GitHub 控制面状态可进入 Postgres。Taskflow 的生产级状态恢复仍以后续 store wiring 为准。

生产环境推荐让 Labrastro 在容器内监听 HTTP，再由 Nginx、Caddy、Traefik 或 Cloudflare 等部署层组件终止 HTTPS：

```text
https://labrastro.example.com -> Nginx/Caddy -> labrastro-host:8765
```

Labrastro 应用内负责账号认证、权限、token 生命周期和审计；TLS 证书、HSTS、域名、公网端口、防火墙、IP allowlist 和反向代理日志属于部署层治理。

## 路线图

近期建设重点：

- **网络优化**：改善网络通信问题。
- **前端优化**：重构文字渲染为更高性能方式，继续美化布局、优化交互。
- **Taskflow 产品化**：补齐 Review Cards 前端交互、自然澄清流程、用户确认面板和 Taskflow 详情页。
- **项目级长期记忆**：让 ProjectState 的术语、决策、约束、WorkItem 和 TraceLink 能被用户查看、修正和复用。
- **Taskflow 持久化完善**：接入 Postgres-backed Taskflow state store，让 ProjectState/TaskflowState 在生产环境中可恢复。
- **工程产物投影**：让 PRD、RFC、ADR、Tech Spec、API Spec、Runbook 等从 Taskflow 状态生成、回写、版本化。
- **多执行器运行时成熟化**：继续完善 Codex、Claude、Gemini、ReuleauxCoder 等执行器的能力探测、session resume、MCP 配置隔离和部署 smoke。
- **协作闭环**：强化 Issue assignment、mention、GitHub review follow-up、PR 生命周期和 runtime task 之间的追踪关系。


## 快速开始

本地开发：

```bash
git clone https://github.com/AstralSolipsism/Labrastro.git
cd Labrastro
uv sync
uv run rcoder --version
uv run rcoder --server
```

常用验证：

```powershell
uv run pytest -q

cd reuleauxcoder-agent
go test ./...
```

Agent Runtime 部署 smoke 常用检查：

```bash
claude --version
gemini --version
codex --version
uv run pytest tests/labrastro_server/services/agent_runtime tests/labrastro_server/http
cd reuleauxcoder-agent && go test ./...
```

## 许可证

AGPL-3.0-or-later
