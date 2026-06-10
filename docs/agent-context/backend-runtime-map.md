# Labrastro 后端运行时百科

本文是后端架构百科，用来查找模块、事实源、运行边界和常见修改入口。
本文是描述性上下文，最高优先级开发约束以仓库根目录的 `AGENTS.md` 和
`AGENT.md` 为准。

最后审阅：2026-06-10。

使用本文时必须以当前代码为最终证据。本文允许落后于实现细节，不能替代代码、
测试、运行日志和部署验收。

## 阅读顺序

1. `AGENTS.md` / `AGENT.md`：后端开发宪法和硬约束。
2. 本文：后端百科和实现索引。
3. 规划与契约文档：
   - `docs/agent-run-session-run-contract.md`
   - `docs/taskflow-engineering-method-projection-design.md`
   - `docs/adr/0003-capability-environment-mcp-boundary.md`
   - `docs/adr/0006-lifecycle-hooks-and-capability-extension-contract.md`

## 仓库形态

```text
Labrastro/
  reuleauxcoder/
    domain/              执行器本地领域模型和运行时基础能力
    services/            执行器本地 LLM、provider、prompt、config 服务
    infrastructure/      本地文件系统、平台、持久化、诊断
    interfaces/          CLI/TUI/entrypoint 适配层
    extensions/          tools、commands、MCP、LSP、skills
    app/                 应用用例和本地 runtime bridge

  labrastro_server/
    services/            服务端控制面：AgentRun、admin、能力包等
    taskflow/            ProjectState、TaskflowState、compiler、runtime projection
    relay/               peer relay 基础设施
    adapters/            面向 ReuleauxCoder 的服务端适配器
    interfaces/http/     remote HTTP 协议和路由分发
    infrastructure/      服务端持久化、migration、store wiring

  reuleauxcoder-agent/
    cmd/                 Go peer 入口
    internal/client/     relay HTTP client
    internal/runner/     register、heartbeat、poll、execute loop
    internal/protocol/   peer protocol DTO
    internal/tools/      peer 侧 shell/file/LSP 执行

  docker/                后端部署资产
  tests/                 Python 测试
```

本文中的路径应写完整归属包。单独写 `domain/` 或 `services/` 容易混淆，
因为 `reuleauxcoder/` 和 `labrastro_server/` 都有同名层。

## 事实源表

| 领域 | 可写事实源 | 派生视图 | 边界 |
| --- | --- | --- | --- |
| 交互式聊天 | `SessionRun` 状态和 canonical transcript，位于 `labrastro_server.interfaces.http.remote.service` 与 chat protocol DTO | ChatView、session recovery、status events | AgentRun 原始输出进入用户正文前必须先投影为语义事件。 |
| 后台 Agent 执行 | `AgentRunRecord`、AgentRun queue/event/artifact/session store、runtime control plane | AgentRun detail、admin events、Taskflow runtime projection、SessionRun semantic projection | AgentRun raw event 是审计事实，不是 ChatView 主正文。 |
| Taskflow 计划 | `labrastro_server.taskflow` 下的 `TaskflowState` 和 `ProjectState` | TaskflowView、compiler review、runtime projection、trace/projector view | projector 是派生结果，不能反写 `TaskflowState` 或 `ProjectState`。 |
| Taskflow 运行视图 | `ProjectState` 中的 `TaskRun` trace/link，加 AgentRun control-plane detail | `TaskflowRuntimeProjectionService` 输出 | 运行所有权归 AgentRun；Taskflow 只把 traceability 和 live runtime detail 连接成视图。 |
| 能力包 | `Config.capability_packages`、capability components、validated draft、installer output | Settings 能力界面、capability ingest SessionRun、environment/MCP/Skill dashboard | 能力安装必须走后端服务和校验；Settings 不能维护独立安装进度事实源。 |
| 环境需求 | `Config.environment.requirements` / `EnvironmentRequirementConfig` | environment manifest、admin environment requirement dashboard | MCP 或 Skill 的运行前置条件用 requirement refs 表达。 |
| 生命周期 hooks | 声明式 hook config 加 `LifecycleHookRuntimeAdapterRegistry` | hook catalog、trust UI、runtime audit、SessionRun/Taskflow projection | adapter registry 是运行可用性的事实源，UI 的 executable 判断必须和真实执行一致。 |
| 权限和审批 | permission gateway、approval engine/provider、SessionRun approval events | ChatView approval、admin diagnostics、lifecycle audit | hook 只能建议、补充上下文或要求确认，不能绕过权限网关授权执行。 |
| 服务端配置 | `Config`、config loader/store、admin server-settings mutation path | Settings panels、manifest、runtime settings | runtime projection 不能变成独立配置写入点。 |
| 远端 peer | relay registry、peer token/session state、`/remote/*` 协议 | remote features、peer liveness、AgentRun claim/heartbeat/event stream | 服务端路径、本地 peer 路径、AgentRun worktree 路径是不同空间。 |
| prompt context | ReuleauxCoder runtime 的 `ProjectContextHook`；外部 executor prompt file 由 prompt renderer 生成 | runtime system message、executor prompt file | Codex 开发入口是 `AGENTS.md`；ReuleauxCoder 的 `ProjectContextHook` 读取 `AGENT.md` 和 `CLAUDE.md`，不读取 `AGENTS.md`。 |

## 当前运行主线

### SessionRun 与 Chat

`SessionRun` 是用户可观察的交互式会话边界。

主要代码：

- `labrastro_server/interfaces/http/remote/service.py`
- `labrastro_server/interfaces/http/remote/routes/chat.py`
- `labrastro_server/interfaces/http/remote/protocol/chat.py`
- `labrastro_server/services/agent_runtime/session_projection.py`

关键 endpoint：

- `POST /remote/session-runs/start`
- `POST /remote/session-runs/events`
- `POST /remote/session-runs/status`
- `POST /remote/session-runs/recover`
- `POST /remote/session-runs/cancel`
- `POST /remote/session-runs/follow-up`
- `POST /remote/session-runs/follow-up/cancel`
- `POST /remote/session-runs/user-input/reply`
- `POST /remote/approval/reply`

约束：

- ChatView 只消费 canonical SessionRun transcript。
- `SessionRun` 取消必须关闭关联 pending approval，并写入一个用户可见终态。
- AgentRun executor event 只能通过 semantic projection 进入 ChatView。
- raw exception、完整 stdout、工具诊断、prompt 细节、traceback 属于 audit/detail。

### AgentRun 控制面

`AgentRun` 是后台执行生命周期。chat、delegation、Taskflow、environment、
capability ingest、manual run 都收敛到同一套持久形态。

主要代码：

- `reuleauxcoder/domain/agent_runtime/models.py`
- `labrastro_server/services/agent_runtime/control_plane.py`
- `labrastro_server/services/agent_runtime/runtime_store.py`
- `labrastro_server/services/agent_runtime/model_bridge.py`
- `labrastro_server/interfaces/http/remote/routes/agent_runs.py`

关键 endpoint：

- `GET /remote/agent-runs/{agent_run_id}/events`
- `POST /remote/agent-runs/claim`
- `POST /remote/agent-runs/heartbeat`
- `POST /remote/agent-runs/session`
- `POST /remote/agent-runs/event`
- `POST /remote/agent-runs/model-request`
- `POST /remote/agent-runs/complete`
- `POST /remote/admin/agent-runs/submit`
- `POST /remote/admin/agent-runs/events`
- `POST /remote/admin/agent-runs/cancel`
- `POST /remote/admin/agent-runs/retry`
- `POST /remote/admin/agent-runs/list`
- `POST /remote/admin/agent-runs/load`

约束：

- AgentRun raw event 是审计事实。
- `ModelRequest` 是 AgentRun worker 请求模型能力的服务端边界。
- sandbox 或 peer worker 不能携带 provider secret，不能绕过服务端模型配置。
- timeout、cancel、retry、heartbeat、terminal state 必须能从 AgentRun
  metadata、profile 或 control-plane state 解释。

### Taskflow

Taskflow 是目标编译层，负责把用户目标编译成可追踪的工程工作项、验收义务、
dispatch contract 和运行入口。后台执行由 AgentRun 控制面负责。

主要代码：

- `labrastro_server/taskflow/domain/project_state.py`
- `labrastro_server/taskflow/domain/taskflow_state.py`
- `labrastro_server/taskflow/application/taskflow_service.py`
- `labrastro_server/taskflow/application/runtime_projection_service.py`
- `labrastro_server/interfaces/http/remote/routes/taskflow.py`

核心对象：

- `ProjectState`：长期项目记忆、决策、约束、WorkItem、TraceLink、projection。
- `TaskflowState`：单个 goal 的澄清、决策、编译、review、dispatch 准备。
- `PlanCompiler`：把 `TaskflowState` + `ProjectState` 编译成 WorkItem、
  obligation、trace link 和 dispatch decision。
- `TaskflowRuntimeProjectionService`：把 TaskRun traceability 与 live
  AgentRun detail 拼成运行视图。

关键 endpoint：

- `GET /remote/taskflow/{path}`
- `POST /remote/taskflow/{path}`
- `GET /remote/taskflow/taskflows/{taskflow_id}/runtime`

约束：

- Taskflow 消费项目理解快照；项目理解是 workspace 级后台层。
- Taskflow runtime projection 是派生视图，不拥有 AgentRun。
- TaskRun 与 AgentRun 必须通过 TraceLink、source metadata、
  `parent_run_id`、`parent_session_id` 或等价引用保持可追踪。
- Projector 不能反写 `ProjectState` 或 `TaskflowState`。

### 项目理解投影

这一块以 `docs/taskflow-engineering-method-projection-design.md` 为当前设计源。

目标形态：

```text
workspace code/docs/tests
  -> ProjectProjectionV1 snapshot
  -> stale/confidence/evidence records
  -> Taskflow Method Router and Question Planner
  -> TaskflowState
  -> PlanCompiler / artifact projectors / dispatch contract
  -> AgentRun runtime
```

边界：

- Project Understanding 是 workspace 级后台服务。
- 它可以收集证据、摘要、缓存、标记 stale、提出 Project Memory patch。
- 它不能静默自动写入 `ProjectState`。

### 能力包与环境需求

能力包是后端拥有的能力集合。能力包可以贡献 skills、MCP servers、
environment requirements、prompt fragments、credentials、memory provider
adapters、memory source connectors 和 lifecycle hooks。

主要代码：

- `reuleauxcoder/domain/config/models.py`
- `reuleauxcoder/domain/environment_requirements.py`
- `reuleauxcoder/domain/runtime_footprint.py`
- `labrastro_server/services/capability_packages.py`
- `labrastro_server/services/capability_package_ingest.py`
- `labrastro_server/interfaces/http/remote/routes/admin.py`

关键服务：

- `CapabilityPackageIngestService`：收集源材料，并编排 package-drafting
  AgentRun。
- `CapabilityPackageSessionRunService`：把能力包 ingest/install 暴露为用户
  可观察的 SessionRun。
- `CapabilityPackageInstaller`：把已确认 draft 安装进 config-backed
  capability packages 和 components。
- `CapabilityDraftValidator`：安装前校验 package shape、contribution、
  runtime footprint 和 hook 数据。

关键 endpoint：

- `POST /remote/admin/capability-packages/ingest/session/start`
- `POST /remote/admin/capability-packages/ingest/start`
- `POST /remote/admin/capability-packages/ingest/status`
- `POST /remote/admin/capability-packages/drafts/accept`
- `POST /remote/admin/capability-packages/delete`
- `POST /remote/admin/capability-packages/enable`
- `POST /remote/admin/environment-requirements/list`
- `POST /remote/admin/environment-requirements/dashboard`
- `POST /remote/admin/environment-requirements/record`
- `POST /remote/admin/environment-requirements/delete`
- `POST /remote/admin/environment-requirements/enable`
- `POST /remote/environment/manifest`

约束：

- 能力包安装必须按 contribution kind 分发。
- MCP server 的运行前置条件通过 `environment_requirement_refs` 表达。
- credential、env var、path、project file、container 等无命令需求可以进入
  environment manifest，不需要生成 `allowed_commands`。
- `managed_by=capability_package` 表示资源由能力包拥有，并约束清理行为。
- 能力包贡献的 lifecycle hook 默认进入 `pending_review`。

### 生命周期 hooks

生命周期 hooks 是声明式运行时动作。它和 ReuleauxCoder 内部旧 Python hook
class 系统分别承担不同职责。

主要代码：

- `reuleauxcoder/domain/hooks/lifecycle.py`
- `reuleauxcoder/domain/agent/agent.py`
- `reuleauxcoder/domain/config/models.py`
- `labrastro_server/services/admin/service.py`
- `labrastro_server/interfaces/http/remote/routes/admin.py`

核心对象：

- `LifecycleHookDeclaration`：归一化后的声明式 hook。
- `LifecycleHookRuntimeAdapter`：单个 handler type 的执行合同。
- `LifecycleHookRuntimeAdapterRegistry`：handler runtime availability 和
  dispatch 的唯一事实源。
- `LifecycleHookDispatcher`：通过 adapter 分发 trusted declaration。

handler type：

- `internal`：系统内置 adapter。
- `prompt`：通过受控模型请求生成结构化 hook output。
- `command`：命令执行，必须经过权限、placement、timeout、截断和审计。
- `http`：HTTP 调用，必须经过权限、timeout、响应大小限制、错误归一和审计。
- `mcp_tool`：通过 MCP/tool gateway 执行，并复用普通工具权限和 result 闭合。
- `agent`：通过受控 AgentRun 或子 agent 调度，绑定预算、取消、中断和终态。

trust state：

- `pending_review`
- `trusted`
- `disabled`
- `blocked`

关键 endpoint：

- `POST /remote/admin/lifecycle-hooks/trust`

约束：

- dashboard executable 状态和真实运行时执行能力必须来自同一个 adapter
  registry。
- `pending_review` hook 不执行高风险动作。
- hook output 对 prompt input、additional context、deny、ask、stop flow 的
  影响必须写入明确的 SessionRun 或 audit 行为。
- 权限网关仍是最终执行裁决点。

### ReuleauxCoder 本地运行时

`reuleauxcoder/` 是从 ReuleauxCoder 继承并扩展的执行器本地运行时。它仍然
重要，但它只是后端架构的一部分。

主要代码：

- `reuleauxcoder/domain/agent/agent.py`
- `reuleauxcoder/domain/agent/loop.py`
- `reuleauxcoder/domain/agent/tool_execution.py`
- `reuleauxcoder/domain/context/manager.py`
- `reuleauxcoder/domain/approval.py`
- `reuleauxcoder/domain/approval_engine.py`
- `reuleauxcoder/domain/session/models.py`
- `reuleauxcoder/infrastructure/persistence/session_store.py`
- `reuleauxcoder/app/runtime/session_state.py`

本地 session state：

- `SessionRuntimeState` 保存本地 mode、model profile、debug trace、approval
  override、fingerprint 和执行占位信息。
- 这个 saved-session overlay 是本地运行时状态，不能和远端 `SessionRun`
  canonical transcript 或 AgentRun 执行状态混淆。

本地 Python hook 系统：

- `reuleauxcoder/domain/hooks/types.py`
- `reuleauxcoder/domain/hooks/registry.py`
- `reuleauxcoder/domain/hooks/discovery.py`
- `reuleauxcoder/domain/hooks/builtin/`

内置本地 hook：

- `ToolPolicyGuardHook`
- `ToolOutputTruncationHook`
- `ProjectContextHook`
- `ProjectContextStartupNotifier`
- `LspEditObserverHook`
- `LspDiagnosticInjectorHook`

本地 hook point：

- `BEFORE_TOOL_EXECUTE`
- `AFTER_TOOL_EXECUTE`
- `BEFORE_LLM_REQUEST`
- `AFTER_LLM_RESPONSE`
- `RUNNER_STARTUP`
- `RUNNER_SHUTDOWN`
- `SESSION_START`
- `SESSION_SAVE`

边界：

- 本地 Python hooks 属于实现级拦截。
- 声明式 lifecycle hooks 属于产品级运行时动作。
- 新增产品可见生命周期行为应优先走 `lifecycle.py` 和 adapter registry。

### 工具执行

主要代码：

- `reuleauxcoder/domain/agent/tool_execution.py`
- `reuleauxcoder/extensions/tools/base.py`
- `reuleauxcoder/extensions/tools/registry.py`
- `reuleauxcoder/extensions/tools/backend.py`
- `reuleauxcoder/extensions/tools/builtin/`

工具执行管线：

1. 从 agent-local list 或全局 registry 解析 tool。
2. 构造 `BeforeToolExecuteContext`。
3. 执行 guard hooks。
4. 执行 preflight validation。
5. 检查 mode restrictions。
6. 按需发起 approval。
7. 执行 transform hooks。
8. 执行 observer hooks。
9. 通过本地或 backend handler 执行 tool。
10. 执行 post-processing 和 after-tool hooks。

约束：

- 多 backend tool 使用 `@backend_handler("backend_id")`。
- 本地执行使用 `LocalToolBackend`。
- 远端执行使用 `RemoteRelayToolBackend`。
- 改文件或执行命令的工具必须保留 policy、approval 和 audit 路径。

### Prompt 与上下文

主要代码：

- `reuleauxcoder/services/prompt/builder.py`
- `reuleauxcoder/domain/hooks/builtin/project_context.py`
- `labrastro_server/services/agent_runtime/prompt_renderer.py`

边界：

- `PromptAssembler` 负责构造 ReuleauxCoder system prompt，并保持稳定排序。
- `ProjectContextHook` 在 ReuleauxCoder runtime 中把本地项目上下文文件注入为
  单独 system message。
- `ProjectContextHook` 搜索 `AGENT.md`、`.agent.md`、`CLAUDE.md`、
  `.claude.md`。
- Codex 的项目开发入口是 `AGENTS.md`，这个入口由 Codex 自身加载。
- 外部 executor prompt renderer 按 executor 映射文件，例如 Codex
  `AGENTS.md`、Claude `CLAUDE.md`、Gemini `GEMINI.md`、ReuleauxCoder
  `AGENT_RUNTIME.md`。

### Remote Relay 与 Go Peer

主要代码：

- `labrastro_server/interfaces/http/remote/service.py`
- `labrastro_server/interfaces/http/remote/protocol/registry.py`
- `labrastro_server/interfaces/http/remote/routes/`
- `labrastro_server/relay/`
- `labrastro_server/adapters/reuleauxcoder/`
- `reuleauxcoder-agent/cmd/reuleauxcoder-agent/main.go`
- `reuleauxcoder-agent/internal/runner/runner.go`
- `reuleauxcoder-agent/internal/protocol/types.go`
- `reuleauxcoder-agent/internal/tools/execute.go`

peer 生命周期：

1. peer 使用 bootstrap token 注册。
2. server 返回 `peer_id` 和 `peer_token`。
3. peer 发送 heartbeat。
4. peer poll work。
5. peer claim AgentRun 或执行 relay work。
6. peer 上报 events、model requests、sessions 和 completion。

server route group：

- auth 与 user/device/audit routes
- peer register/heartbeat/poll/result/features routes
- session inventory routes
- SessionRun chat routes
- AgentRun worker/admin routes
- Taskflow routes
- capability package/admin settings routes
- environment/MCP/Skill routes
- lifecycle hook trust route
- artifact routes
- GitHub/collaboration/issue routes

### 配置

主要代码：

- `reuleauxcoder/domain/config/models.py`
- `reuleauxcoder/domain/config/schema.py`
- `reuleauxcoder/services/config/loader.py`
- `reuleauxcoder/infrastructure/persistence/workspace_config_store.py`
- `labrastro_server/services/admin/service.py`

配置文件：

- workspace config：`.rcoder/config.yaml`
- user config：`~/.rcoder/config.yaml`

关键 section：

- `models`
- `modes`
- `approval`
- `prompt`
- `skills`
- `mcp.servers`
- `environment.requirements`
- `capability_packages`
- `context`
- `session`
- `tool_output`
- `remote_exec`
- `lsp`
- `auth`
- `cli`

约束：

- Config 是基线。
- 本地 saved-session runtime overlay 不是 config。
- Admin Settings 修改必须走后端 config service。
- Runtime projection 不能变成 config 写入。

## 实现索引

### 新增或修改内置工具

1. 在 `reuleauxcoder/extensions/tools/builtin/` 新增或修改 tool class。
2. 使用现有 `Tool` base class 和 schema 约定。
3. 通过 `@register_tool` 注册。
4. 支持多 backend 时使用 `@backend_handler(...)`。
5. 文件系统和 shell 行为必须保留 policy、approval、diagnostics。
6. 增加聚焦验证、backend dispatch、permission behavior 的测试。

### 新增或修改 slash command

主要代码：

- `reuleauxcoder/extensions/command/builtin/`
- `reuleauxcoder/app/commands/`
- `reuleauxcoder/interfaces/cli/commands.py`

约束：

- session-scoped command 只影响当前 runtime state。
- global command 持久化 workspace defaults/config。
- local-only command 必须说明运行位置限制。
- 影响 remote/server state 的 command 必须走 server control plane。

### 新增或修改 lifecycle hook

产品可见生命周期行为使用这条路径：

1. 在 lifecycle models 中定义或修改声明式 hook schema。
2. 确认 handler type 由 `LifecycleHookRuntimeAdapterRegistry` 支持。
3. 定义 trust、placement、permission、timeout、audit、projection 行为。
4. 用户可管理的 hook 需要 admin/Settings 可见性。
5. 按影响验证 SessionRun、AgentRun、Taskflow projection。

只有严格属于 ReuleauxCoder 内部执行拦截的行为，才使用旧 Python hook class 路径。

### 新增或修改 Taskflow runtime 行为

1. 先判断修改写入 `TaskflowState`、`ProjectState`、`AgentRun` 还是 projection。
2. 计划和编译事实留在 Taskflow。
3. 执行事实留在 AgentRun。
4. 增加 trace/source metadata，保证 TaskRun、WorkItem、AgentRun、SessionRun
   可关联。
5. Taskflow runtime projection 只能做视图，不能成为写路径。

### 新增或修改能力包行为

1. 确定 installed data 属于哪一种 contribution kind。
2. 安装前验证 draft。
3. 用户可见安装进度通过 SessionRun 展示。
4. runtime prerequisites 写入 environment requirements。
5. 能力包拥有的资源使用 `managed_by=capability_package`。
6. lifecycle hook trust 必须显式，默认 pending/untrusted。

## 验证

通用后端检查：

```powershell
uv run python -m pytest tests/ -v
go test ./...
```

常用聚焦检查：

```powershell
uv run python -m pytest tests/labrastro_server/http/test_remote_service.py -q
uv run python -m pytest tests/labrastro_server/services/test_capability_packages.py -q
uv run python -m pytest tests/domain/hooks -q
uv run python -m pytest tests/domain/test_config_models.py -q
```

部署验收通常还需要：

- 本地 repo revision
- 已部署源码 revision
- container/image revision
- service health endpoint 或 `/remote/features`
- 相关 runtime import 或行为探针

## 已知限制

- 本文是架构摘要，不能替代源码证据。
- endpoint 清单会随着协议演进漂移；最新 endpoint 以
  `labrastro_server/interfaces/http/remote/protocol/registry.py` 为准。
- lifecycle hook 实现变化较快；精确运行合同以
  `reuleauxcoder/domain/hooks/lifecycle.py` 和 ADR 0006 为准。
- Taskflow 项目理解投影仍包含规划内容；该区域以
  `docs/taskflow-engineering-method-projection-design.md` 为当前设计源。
