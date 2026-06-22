# Centralized AgentRun And Local Peer Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Independent worker delegation must stay inside the task boundaries in this document. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将普通聊天、AgentRun、local peer、本地动作、MCP 和旧 relay/poll 执行面收敛到中心化自托管架构，消除普通聊天依赖本地 peer、旧 `/remote/poll` 工具执行主干、独立 relay 结果语义和只显示 spinner 的等待状态。

**Architecture:** Labrastro 是中心化自托管控制面和主线执行环境。普通聊天默认由服务端 owned AgentRun 承载，模型 provider 请求、密钥、出网 IP、权限、审计和过程投影留在服务端；local peer 是受调度的本地资源执行者，只执行明确的本地动作、peer target facts、MCP 生命周期、能力包本地检查/安装或非主线后台本地任务。所有本地动作必须作为当前 AgentRun/SessionRun 的子事件进入统一投影，旧 `/remote/poll` 和 `RemoteRelayToolBackend` 不能继续作为并行执行语义存在。

**Tech Stack:** Python backend, AgentRun control plane/store/session projection, Postgres, Go local peer, VS Code extension Host TypeScript, Solid Webview, MCP runtime, pytest, Go test, Vitest, GitNexus.

---

## Review Gate

本文档是专项执行文档。审核通过前，不允许开始代码实施。

执行阶段必须以本文档为唯一对照标准。若旧聊天记录、临时修复、已提交止血补丁、旧计划、旧 ADR、局部测试或实现现状与本文档冲突，按本文档执行，并在提交说明中点明替换了哪个旧语义。

本文档仅在审核阶段修改。审核通过后，任何执行中发现的新产品语义冲突都必须暂停并回到本文档修订，不能在代码里临时发挥。

## Development-Period Rules

- 不做兼容、迁移、灰度和双轨过渡。
- 不保留临时止血路径。
- 不以“当前能跑”为目标牺牲架构统一。
- 不让普通聊天默认依赖本地 VS Code peer。
- 不让 local peer 持有 provider secret。
- 不让 local peer 默认从本机 IP 请求模型 provider。
- 不保留旧 `/remote/poll` 工具执行主干。
- 不保留 `RemoteRelayToolBackend` 作为长期 Agent 工具后端。
- 不让 relay result 成为 AgentRun/SessionRun 之外的第二结果流。
- 不允许只用普通 spinner 表达等待本地 peer、等待本地动作或等待本地审批。
- 不允许 UI、route handler、adapter 或 metadata 推断执行目标；所有执行目标必须来自明确 runtime profile、AgentRun binding、local action binding 或 task identity。

## Confirmed Product Decisions

这些决策已在本专项讨论中确认，执行时不得重新打开。

1. 旧 `/remote/poll` 不保留为兼容通道，也不保留为非公开执行通道。若未来需要非执行诊断轮询，必须使用新的 endpoint 名称和新的协议模型，不能沿用 `poll` / `RelayEnvelope` 概念。
2. MCP 能力保留，但必须迁入 AgentRun/local action 工具体系；旧 poll 不能因为 MCP 继续存在。
3. `RemoteRelayToolBackend` 不作为长期架构保留；可复用工具执行实现迁入正确的 server/local action 边界，命名和职责必须改掉。
4. 用户可见、有副作用、有过程的执行必须进入 AgentRun；非用户会话内的后台/管理执行必须进入明确受控 task；不允许无运行对象的 peer 工具执行。
5. 按破坏式架构收口做，不做双轨兼容，不保留旧测试语义，只保留必要能力价值。
6. 主线 AgentRun 的 LLM/provider 请求默认留在服务端；本地 peer 只承接显式分派的本地动作、资源事实或非主线后台本地任务。
7. 普通聊天默认由服务器跑主流程；本地 VS Code 插件只做明确的本地动作，不做普通聊天主引擎。
8. 本地 peer 不在线时，普通聊天仍应保持可用；只有明确本地动作进入等待或失败状态。
9. 等待本地 peer、等待本地审批、等待本地动作结果，都必须显示明确过程卡片和可操作状态，不能只用“处理中”spinner。
10. 所有本地 peer 动作必须作为当前 AgentRun/SessionRun 的子事件出现；前端只展示统一投影，不再有独立 relay 结果语义。
11. 不做短期止血；不把 `--agent-run-worker` 当作普通聊天可用性的临时补丁。
12. 系统性完整推进，不做“先临时修、后续再清理”的路线。

## Current Drift To Correct

当前最新故障暴露的直接问题：

- 普通聊天入口提交了 `execution_location=local_workspace` 和 `worker_kind=local_peer`。
- VSIX 侧之前没有启动 AgentRun worker，导致 peer 注册和心跳存在，但不 claim activation，前端只剩“处理中”。
- 后续加 `--agent-run-worker` 只能让错误路由下的任务被 local peer 领取，不能作为最终方案。

更深层漂移：

- 普通聊天不应默认提交给 local peer。
- local peer 不应成为普通聊天主线 Agent loop 宿主。
- server LLM loop 到 local peer 工具执行的旧 relay 形态不应保留。
- 本地动作需要明确建模为当前 AgentRun/SessionRun 的子事件，而不是作为旧 relay 队列里的 `exec_tool`/`preview_tool`/`cleanup` envelope。
- 前端不能再从“是否有事件”猜测进度；等待本地资源必须由后端投影成明确状态。

## Target User Experience

普通用户体验必须变成以下形态：

1. 用户在 ChatView 发普通消息。
2. 服务端创建或继续 server-owned AgentRun。
3. provider 请求从服务端发出，使用服务端配置和密钥。
4. 前端显示服务端统一投影出的过程卡片。
5. 如果任务不需要本地资源，即使 VS Code peer 离线，普通聊天仍能完成。
6. 如果某一步需要本地资源，前端显示明确卡片，例如“等待本地工作区连接”“需要读取本地文件”“需要本地 MCP 状态”“本地依赖安装中”。
7. 本地动作完成、失败、取消或超时后，结果回到同一 AgentRun/SessionRun 投影，服务端继续主流程。
8. 用户不会再看到只有“处理中”但没有原因、没有动作、没有可恢复路径的状态。

## Target Runtime Model

### Server-Owned Mainline Chat

普通聊天默认是 server-owned AgentRun：

- `execution_location=remote_server`
- `worker_kind=server_worker`
- `model_request_origin=server`

这适用于：

- 普通问答
- 规划
- 代码分析
- 文档生成
- Taskflow 规划/编译
- 服务端可访问仓库和服务端资源任务
- 不需要本地工作区事实的会话

### Local Peer As Scheduled Resource

local peer 是本地资源执行者，不是普通聊天主引擎。

它适用于：

- 读取、修改用户本机当前 VS Code 工作区文件。
- 执行用户本机安装的命令、SDK、语言服务。
- 启动、检查、调用用户本地 MCP server。
- 安装、检查本地能力包依赖。
- 回报 peer target facts。
- 执行明确标记为 local-only 的后台长任务。

local peer 不适用于：

- 普通聊天的默认 Agent loop。
- 默认 provider 请求。
- 持有服务端 provider secret。
- 替代 server worker 成为主线执行环境。

### Local Action Contract

本地动作是服务端主线 AgentRun/SessionRun 内的子事件。

本地动作只有三种封闭 scope，不允许半绑定动作：

- `activation_scoped`: 属于某个正在运行并可追踪的 AgentRun activation。必须携带 `agent_run_id`、`activation_id`、`local_action_id`、`action_kind`。ChatView/SessionRun 可见动作还必须携带 `session_run_id` 和 `branch_binding_id`。
- `run_scoped`: 属于某个 AgentRun，但不属于单个 activation，例如 run-level cleanup、workspace fact refresh。必须携带 `agent_run_id`、`local_action_id`、`action_kind`。ChatView/SessionRun 可见动作还必须携带 `session_run_id` 和 `branch_binding_id`。
- `admin_task_scoped`: 属于明确的 admin/operator task，例如本地能力包目标事实重检。必须携带 `admin_task_id`、`local_action_id`、`action_kind` 和发起 actor。它不能伪装成 SessionRun 事件；ChatView 展示必须通过显式关联的 AgentRun/SessionRun 重新投影。

禁止的 scope：

- 只有 `peer_id` 没有运行对象的动作。
- 只有 `workspace_root` 没有运行对象的动作。
- 只有 relay request id 的动作。
- 只有 capability action id 但没有 `local_action_id` / desired action identity 的动作。

最小状态：

- `local_action_requested`
- `local_action_waiting_peer`
- `local_action_started`
- `local_action_progress`
- `local_action_completed`
- `local_action_failed`
- `local_action_cancelled`
- `local_action_timed_out`

最小身份字段：

- `session_run_id`
- `agent_run_id`
- `activation_id` for `activation_scoped` actions
- `branch_binding_id` for all actions visible in SessionRun branch projection
- `local_action_id`
- `peer_id` when assigned
- `workspace_root` when required
- `action_kind`
- `requested_by`
- `created_at`
- `updated_at`

规则：

- 本地动作必须绑定当前运行对象。
- 本地动作创建时必须声明 scope，并在存储层校验必填身份字段。
- 本地动作结果不得通过独立 relay result 流绕过 AgentRun/SessionRun。
- 本地动作等待必须可投影到前端过程卡片。
- 本地动作失败不得让整个会话只剩 spinner。
- 本地动作的 cancel、retry、skip 支持由 action kind 明确声明。

### Local Action Dispatch Contract

本地动作控制面必须显式替代旧 `/remote/poll`，不能复刻旧 relay 队列。

服务端创建本地动作：

- 只能由 AgentRun control plane、SessionRun projection service、Capability/Admin service 中的明确 use case 创建。
- 创建时写入 local action store，并同时写入 `local_action_requested` / `local_action_waiting_peer` 事件。
- 创建时必须确定 action scope、action kind、desired target、workspace requirement、timeout、retry policy 和可见投影目标。
- 创建时不得直接发送工具 envelope 给 peer。

Peer claim endpoint：

- 新 endpoint 使用明确名称，例如 `POST /remote/local-actions/claim`。
- 请求必须携带 `peer_token`、`peer_id`、`worker_kind`、`features`、`workspace_root`、`max_actions`。
- 服务端只返回与 peer identity、workspace、features、action kind 匹配的 local actions。
- claim 响应必须包含 `local_action_id`、`lease_id`、`lease_expires_at`、`action_kind`、`scope`、`workspace_root`、`payload`。
- local peer 不能 claim server-owned model/provider request，也不能 claim ordinary `remote_server` AgentRun。

Peer progress/result endpoints：

- 新 endpoint 使用明确名称，例如 `POST /remote/local-actions/progress` 和 `POST /remote/local-actions/complete`。
- 请求必须携带 `peer_token`、`local_action_id`、`lease_id` 和 result/progress payload。
- 服务端必须校验 lease 当前有效、peer 匹配、action 未终结。
- progress 写入 `local_action_progress` 事件。
- complete 写入 `local_action_completed` 或 `local_action_failed` 事件，并更新 local action store。
- result payload 只能回到 local action store 和 AgentRun/SessionRun 投影，不得进入独立 relay result 流。

Lease and heartbeat:

- local action claim 必须有 lease。
- 长动作必须 heartbeat 或 progress 续约。
- lease 过期必须投影为 `local_action_waiting_peer` 或 `local_action_timed_out`，不能让前端只显示 spinner。
- cancel 必须写入 `local_action_cancelled`，并通知已 claim peer；若 peer 离线，服务端仍要终结可见状态。

## Files And Responsibilities

执行前必须先读取这些文件，并在实施中保持职责边界。

### Backend Runtime And Protocol

- `labrastro_server/interfaces/http/remote/routes/chat.py`
  - 普通聊天 start/continue 的 runtime profile 选择和提交入口。
  - 必须改回 server-owned AgentRun 默认语义。
- `labrastro_server/interfaces/http/remote/protocol/chat.py`
  - SessionRun request/response/projection DTO。
  - Chat/Session 可见的 local action 投影字段在这里建模。
- `labrastro_server/interfaces/http/remote/protocol/agent_runs.py`
  - AgentRun activation、heartbeat、event、complete 相关 DTO。
  - 只承载 AgentRun event envelope，不定义 local action claim/progress/complete 协议。
- `labrastro_server/interfaces/http/remote/protocol/local_actions.py`
  - 新建 local action record、claim、progress、complete、cancel DTO。
  - 这是 local action HTTP 协议唯一模型文件。
- `labrastro_server/interfaces/http/remote/protocol/registry.py`
  - 删除旧 `peer.poll` 契约。不得保留 `poll` / `RelayEnvelope` 作为执行或诊断协议名。
  - 新 local action endpoint 必须有 registry entry 和 contract fixture。
- `labrastro_server/interfaces/http/remote/routes/peer.py`
  - `_handle_poll` 旧工具执行入口清理点。
  - `_handle_poll` 必须删除。非执行型 heartbeat/register/status 使用明确命名 endpoint；本地动作使用 local action route。
- `labrastro_server/interfaces/http/remote/routes/agent_runs.py`
  - AgentRun claim/heartbeat/event/complete/control 路径。
  - AgentRun route 只暴露 AgentRun 控制面；local action request/result 使用 `routes/local_actions.py`。
- `labrastro_server/interfaces/http/remote/routes/local_actions.py`
  - 新建 local action claim/progress/complete/cancel endpoint。
  - 该 route 只处理 local action 协议，不处理通用工具 relay。
- `labrastro_server/interfaces/http/remote/service.py`
  - RemoteRelayHTTPService 当前承载 queues、poll、SessionRun projection、admin route dispatch。
  - 必须移除 `_queues` 作为工具执行事实源。
  - 统一投影需要从 AgentRun/SessionRun 事实生成。
- `labrastro_server/services/agent_runtime/control_plane.py`
  - AgentRun submit/claim/event/cancel/status 主事实源。
- server-owned chat runtime 的 AgentRun 事实由 control plane 持有；local action 事实由 `services/agent_runtime/local_actions.py` 持有，并通过明确服务边界 append 到 AgentRun/SessionRun 投影。
- `labrastro_server/services/agent_runtime/local_actions.py`
  - 新建 local action store/service。
  - 负责 action scope 校验、claim matching、lease、progress、complete、cancel 和 projection event append。
- `labrastro_server/services/agent_runtime/runtime_policy.py`
  - worker claim matching。
  - 必须防止 local peer claim `remote_server` run。
  - 必须防止普通聊天默认匹配 local peer。
- `labrastro_server/services/agent_runtime/session_projection.py`
  - AgentRun event 到 SessionRun 可见过程卡片的投影。
- local action waiting/progress/failure 必须通过命名的 local action projection mapper 进入 SessionRun 可见过程卡片。
- `reuleauxcoder/domain/agent_runtime/models.py`
  - `ExecutionLocation`, `WorkerKind`, `ModelRequestOrigin`, runtime profile 默认值。
  - 必须保持 server default，不允许通过 local workspace 默认值把主线聊天推给 peer。

### Backend Legacy Relay To Remove And Rehome Helpers Under Local Actions

- `labrastro_server/relay/server.py`
  - `send_exec_request`, `send_preview_request`, `request_cleanup`, `cancel_pending_requests` 是旧 relay 工具执行主干。
  - 执行完成后不应再作为 Agent 工具执行入口。
- `labrastro_server/relay/cleanup.py`
  - 旧 peer cleanup 入口。
- 有副作用的清理必须改为 local action；仅状态上报的清理事实进入 peer lifecycle 管理事实；两者都不走旧 tool queue。
- `labrastro_server/adapters/reuleauxcoder/remote_backend.py`
  - `RemoteRelayToolBackend` 类和 production import 必须删除。
  - 可复用 preview/save-candidate helper 必须移动到 local-action 命名模块。
  - 不得保留为 Agent 的长期 `ToolBackend`。
- `labrastro_server/adapters/reuleauxcoder/mcp_tools.py`
  - 当前依赖 `RemoteRelayToolBackend` 的 MCP tool adapter。
  - server-owned MCP 使用服务端工具体系；local peer MCP 的 lifecycle、status、invocation 使用 local action。server-managed registry 只保存元数据、期望状态和可用性事实，不执行工具。
- `reuleauxcoder/interfaces/entrypoint/runner.py`
  - 当前可能将 `RemoteRelayToolBackend` 注入 Agent tools。
  - 普通 server-owned chat 不得再注入旧远端 relay backend。
- `reuleauxcoder/interfaces/entrypoint/remote_relay.py`
  - 旧 remote relay interactive/session path。
  - 只能作为待删除实现证据，不能作为新架构基础。

### Go Local Peer

- `reuleauxcoder-agent/internal/runner/runner.go`
  - `runPollLoop` 是旧工具执行队列。
  - `mcp.NewSupervisor` 当前绑定旧 poll route。
  - AgentRun worker 路径不能作为普通聊天主线止血。
  - 新 local action runner 应是明确 action claim/result/report 语义。
- `reuleauxcoder-agent/internal/client/http.go`
- `/remote/poll` client method 必须删除；替代能力使用 typed local action client method。
  - 新 local action endpoint 需要 typed client method。
- `reuleauxcoder-agent/internal/protocol/types.go`
  - 删除 active production 里的旧 `RelayEnvelope` 工具执行模型。
  - 新 local action request/result/progress type 在这里定义。
- `reuleauxcoder-agent/internal/mcp/*`
  - MCP supervisor 可复用，但必须从旧 poll 执行路径解绑。
- MCP lifecycle/status/tool invocation 必须通过 local action events 进入 AgentRun/SessionRun projection。
- `reuleauxcoder-agent/internal/tools/*`
  - 本地工具实现可复用。
  - 工具调用入口不得继续依赖旧 relay envelope。

### VS Code Extension Host And Webview

- `Labrastro-vscode-extension/src/LabrastroRemoteClient.ts`
  - 当前 `--agent-run-worker` 启动参数不得作为普通聊天止血保留。
  - 新 local action client/peer lifecycle 调用应在这里集中。
- `Labrastro-vscode-extension/src/LabrastroRemoteClient.test.ts`
  - 现有 `starts the peer with the AgentRun local worker loop enabled` 测试必须删除。仍需覆盖的 VS Code 启动行为必须用 server-owned chat 与 local action client 语义重建测试，不能继续证明普通聊天靠 peer worker。
- `Labrastro-vscode-extension/src/LabrastroController.ts`
  - Chat/session command routing。
  - 需要保证普通聊天不依赖 peer worker 连接。
  - 本地动作等待、重试、取消、跳过需要通过统一投影进入 webview。
- `Labrastro-vscode-extension/src/coordinators/*`
  - SessionRun coordinator 不得把普通聊天路由到 local peer execution。
  - Local action coordinator 可新建，但不能成为并行 SessionRun runtime。
- `Labrastro-vscode-extension/src/sessionRuntime/*`
  - 统一投影模型应承载 local action 状态。
  - 禁止用 selected UI state 推断后端执行目标。
- `Labrastro-vscode-extension/webview-ui/src/components/chat/SessionTurn.tsx`
  - local action waiting/progress/failure cards。
- `Labrastro-vscode-extension/webview-ui/src/components/chat/transcript-presentation.ts`
  - local action event 到可读过程项的投影。
- `Labrastro-vscode-extension/webview-ui/src/components/ChatView.tsx`
  - 不再只显示“处理中”作为无法解释的等待。
  - local peer 缺失时显示明确原因和操作。

## Implementation Tasks

### Task 1: Contract Guard For Server-Owned Default Chat

**Files:**
- Modify: `tests/labrastro_server/http/test_remote_service.py`
- Modify: `tests/labrastro_server/services/agent_runtime/test_control_plane.py`
- Modify: `labrastro_server/interfaces/http/remote/routes/chat.py`
- Modify: `labrastro_server/services/agent_runtime/runtime_policy.py`

- [ ] **Step 1: Write failing HTTP test for ordinary chat default runtime**

Add a test that starts a normal remote chat/session run without local-only flags and asserts the submitted AgentRun is server-owned.

Expected assertions:

```python
assert agent_run.execution_location == ExecutionLocation.REMOTE_SERVER
assert agent_run.worker_kind == WorkerKind.SERVER_WORKER
assert agent_run.model_request_origin == ModelRequestOrigin.SERVER
assert agent_run.metadata.get("worker_kind") in (None, WorkerKind.SERVER_WORKER.value, "server_worker")
```

Run:

```powershell
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py -k "session_run and server_owned" --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected before implementation: FAIL because current chat path submits `LOCAL_WORKSPACE` / `LOCAL_PEER`.

- [ ] **Step 2: Write failing claim test proving local peer cannot claim ordinary chat**

Add a control-plane/store-level test that submits an ordinary chat AgentRun and attempts claim with:

```python
worker_kind=WorkerKind.LOCAL_PEER
peer_features={"agent_runs", "worker_kind:local_peer", "agent_runs.local_workspace"}
workspace_root="D:\\AboutDEV\\vika_mcp"
```

Expected:

```python
assert claim is None
```

Then claim with server worker:

```python
worker_kind=WorkerKind.SERVER_WORKER
peer_features={"agent_runs", "worker_kind:server_worker", "agent_runs.remote_server"}
```

Expected:

```python
assert claim is not None
```

- [ ] **Step 3: Change ordinary chat submission to server-owned runtime**

In `routes/chat.py`, remove ordinary chat defaulting to:

```python
execution_location=ExecutionLocation.LOCAL_WORKSPACE
worker_kind=WorkerKind.LOCAL_PEER
```

and set ordinary chat default to:

```python
execution_location=ExecutionLocation.REMOTE_SERVER
worker_kind=WorkerKind.SERVER_WORKER
model_request_origin=ModelRequestOrigin.SERVER
```

When the code resolves through runtime profiles, make the test pass by selecting the server default profile instead of hardcoding local peer fields.

- [ ] **Step 4: Preserve local context without making it a peer claim target**

Ordinary chat that carries workspace/project context stores it only as a server-side project/worktree reference. Read-only context snapshots are metadata attached to that server-side reference. Ordinary chat metadata must not include a local peer claim target. Explicit local workspace actions must be created through the local action contract in Task 3, not by switching ordinary chat to `local_workspace`.

- [ ] **Step 5: Run focused tests**

```powershell
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py tests\labrastro_server\services\test_agent_runtime_control_plane.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS for new server-owned chat and local-peer-not-claimable cases.

- [ ] **Step 6: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro add labrastro_server tests
git -C D:\AboutDEV\Labrastro\Labrastro commit -m "fix(agent-runtime): restore server-owned chat mainline"
```

### Task 2: Remove `--agent-run-worker` As Chat Availability Path

**Files:**
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\LabrastroRemoteClient.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\LabrastroRemoteClient.test.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\LabrastroController.ts`

- [ ] **Step 1: Replace the existing peer-worker startup test**

Replace the test named:

```ts
starts the peer with the AgentRun local worker loop enabled
```

New test intent:

```ts
it("does not require the local peer AgentRun worker for ordinary chat readiness", async () => {
  // Arrange an authenticated ready server connection.
  // Arrange peer unavailable.
  // Assert ordinary chat control remains available.
})
```

The assertion must not check for `--agent-run-worker`.

- [ ] **Step 2: Remove ordinary peer startup args**

In `LabrastroRemoteClient.ts`, remove these arguments from the default peer start path:

```ts
"--agent-run-worker",
"--agent-run-worker-kind",
"local_peer",
```

Do not replace them with another default worker mode. A future local action runner must have its own explicit switch and tests.

- [ ] **Step 3: Run focused extension tests**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
npx vitest run src/LabrastroRemoteClient.test.ts src/LabrastroController.chat-stream.test.ts
```

Expected: PASS. No test should assert default peer AgentRun worker mode for ordinary chat.

- [ ] **Step 4: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension add src
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension commit -m "fix(remote): decouple chat readiness from peer worker"
```

### Task 3: Define Local Action Protocol And Projection

**Files:**
- Modify: `labrastro_server/interfaces/http/remote/protocol/agent_runs.py`
- Create: `labrastro_server/interfaces/http/remote/protocol/local_actions.py`
- Modify: `labrastro_server/interfaces/http/remote/protocol/chat.py`
- Modify: `labrastro_server/interfaces/http/remote/protocol/registry.py`
- Create: `labrastro_server/interfaces/http/remote/routes/local_actions.py`
- Modify: `labrastro_server/services/agent_runtime/control_plane.py`
- Create: `labrastro_server/services/agent_runtime/local_actions.py`
- Modify: `labrastro_server/services/agent_runtime/session_projection.py`
- Modify: `tests/labrastro_server/http/test_remote_service.py`
- Modify: `tests/labrastro_server/services/agent_runtime/test_control_plane.py`
- Create: `tests/labrastro_server/services/agent_runtime/test_local_actions.py`

- [ ] **Step 1: Add scope-closure tests first**

Write tests proving half-bound local actions are rejected.

Invalid examples:

```python
{"scope": "activation_scoped", "agent_run_id": "agent-run-1", "local_action_id": "local-action-1"}
{"scope": "run_scoped", "peer_id": "peer-1", "workspace_root": "D:\\AboutDEV\\vika_mcp", "local_action_id": "local-action-1"}
{"scope": "admin_task_scoped", "local_action_id": "local-action-1", "action_kind": "install_python_packages"}
```

Expected:

```python
with pytest.raises(ValueError):
    LocalActionRecord.from_dict(payload)
```

Valid activation-scoped visible action:

```python
payload = {
    "scope": "activation_scoped",
    "local_action_id": "local-action-1",
    "agent_run_id": "agent-run-1",
    "activation_id": "activation-1",
    "session_run_id": "session-run-1",
    "branch_binding_id": "branch-main",
    "action_kind": "read_workspace_file",
    "status": "waiting_peer",
    "workspace_root": "D:\\AboutDEV\\vika_mcp",
}
record = LocalActionRecord.from_dict(payload)
assert record.scope == "activation_scoped"
```

- [ ] **Step 2: Add protocol model tests**

Write tests proving these payloads round-trip:

```python
{
    "scope": "activation_scoped",
    "local_action_id": "local-action-1",
    "agent_run_id": "agent-run-1",
    "activation_id": "activation-1",
    "session_run_id": "session-run-1",
    "branch_binding_id": "branch-main",
    "action_kind": "read_workspace_file",
    "status": "waiting_peer",
    "workspace_root": "D:\\AboutDEV\\vika_mcp",
}
```

and result:

```python
{
    "local_action_id": "local-action-1",
    "status": "completed",
    "result": {"summary": "read 120 lines"},
}
```

Expected:

```python
assert model.local_action_id == "local-action-1"
assert model.status == "waiting_peer"
```

- [ ] **Step 3: Add local action claim and lease tests**

Create tests for the new local action endpoint/service:

```python
claim = service.claim_local_actions(
    peer_id="peer-1",
    worker_kind=WorkerKind.LOCAL_PEER,
    features={"local_actions", "local_action:read_workspace_file"},
    workspace_root="D:\\AboutDEV\\vika_mcp",
    max_actions=1,
)
assert claim.actions[0].local_action_id == "local-action-1"
assert claim.actions[0].lease_id
```

Then verify wrong workspace or missing feature returns no action:

```python
claim = service.claim_local_actions(
    peer_id="peer-2",
    worker_kind=WorkerKind.LOCAL_PEER,
    features={"local_actions"},
    workspace_root="D:\\Other",
    max_actions=1,
)
assert claim.actions == []
```

Complete must require a valid lease:

```python
with pytest.raises(LocalActionLeaseError):
    service.complete_local_action(
        local_action_id="local-action-1",
        peer_id="peer-1",
        lease_id="wrong-lease",
        status="completed",
        result={"summary": "read 120 lines"},
    )
```

- [ ] **Step 4: Add projection test**

Given an AgentRun event:

```python
{
    "type": "local_action_waiting_peer",
    "local_action_id": "local-action-1",
    "action_kind": "read_workspace_file",
    "workspace_root": "D:\\AboutDEV\\vika_mcp",
}
```

Session projection must produce a visible process item with:

```python
assert item["kind"] == "local_action"
assert item["status"] == "waiting_peer"
assert item["message"]
```

The message must be user-comprehensible and must not be raw JSON.

- [ ] **Step 5: Implement protocol, store/service, route, and projection**

Add typed models in `protocol/local_actions.py` for local action record/claim/progress/result. Add the local action service with scope validation, claim matching, lease, progress, complete, cancel, and timeout state transitions. Add `/remote/local-actions/claim`, `/remote/local-actions/progress`, and `/remote/local-actions/complete`. Map local action events to SessionRun projection. Use snake_case on HTTP/public payloads.

- [ ] **Step 6: Run focused tests**

```powershell
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http tests\labrastro_server\services\agent_runtime --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

- [ ] **Step 7: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro add labrastro_server tests
git -C D:\AboutDEV\Labrastro\Labrastro commit -m "feat(agent-runtime): model local peer actions as run events"
```

### Task 4: Replace Legacy Relay Tool Execution With Local Actions

**Files:**
- Modify: `labrastro_server/relay/server.py`
- Modify: `labrastro_server/interfaces/http/remote/routes/peer.py`
- Modify: `labrastro_server/interfaces/http/remote/service.py`
- Modify: `labrastro_server/adapters/reuleauxcoder/remote_backend.py`
- Modify: `labrastro_server/adapters/reuleauxcoder/mcp_tools.py`
- Modify: `reuleauxcoder/interfaces/entrypoint/runner.py`
- Modify: `reuleauxcoder/interfaces/entrypoint/remote_relay.py`
- Delete tests that only prove old relay execution. Replace them with local action tests when the behavior still has product value.

- [ ] **Step 1: Add structure guard test**

Add both a structure guard test and a contract scan asserting no production code uses:

```text
RemoteRelayToolBackend
send_exec_request(
send_preview_request(
request_cleanup(
cancel_pending_requests(
```

as Agent tool execution paths.

During the same task, legacy class names can appear only in files being deleted in that task. Final expected grep:

```powershell
rg -n "RemoteRelayToolBackend|send_exec_request|send_preview_request|request_cleanup|cancel_pending_requests" D:\AboutDEV\Labrastro\Labrastro\labrastro_server D:\AboutDEV\Labrastro\Labrastro\reuleauxcoder -g "*.py"
```

Expected after task: no production execution references. Test-only references that prove old relay execution must be deleted. Product behavior still covered by those tests must be replaced with local action tests in the same task.

- [ ] **Step 2: Remove old backend injection**

In `runner.py` and `remote_relay.py`, remove `RemoteRelayToolBackend` injection as Agent `ToolBackend`.

Do not replace it with another backend that uses `/remote/poll`.

- [ ] **Step 3: Delete remote backend and move reusable helpers under local action names**

Delete `remote_backend.py` after moving any reusable preview/save-candidate helper into a local-action-specific module. The old backend class name and old relay backend file must not remain in production code.

Allowed new responsibility names:

```text
LocalActionPreviewBinder
LocalActionSaveCandidateBinder
LocalPeerActionClient
```

Disallowed names:

```text
RemoteRelayToolBackend
RelayToolBackend
RemoteToolBackend
```

- [ ] **Step 4: Rewrite MCP adapter dependency**

`mcp_tools.py` must not depend on `RemoteRelayToolBackend`. Server-owned MCP tools run through the service-side tool system. Local peer MCP lifecycle, status, and invocation must run through local action events. A server-managed registry is limited to metadata, desired state, and availability facts; it must not execute tools or return tool results outside the local action projection.

- [ ] **Step 5: Run tests and grep**

```powershell
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server tests\interfaces tests\extensions tests\domain --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
rg -n "RemoteRelayToolBackend|send_exec_request|send_preview_request|request_cleanup|cancel_pending_requests" D:\AboutDEV\Labrastro\Labrastro\labrastro_server D:\AboutDEV\Labrastro\Labrastro\reuleauxcoder -g "*.py"
```

Expected: tests pass; grep has no production execution path hits.

- [ ] **Step 6: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro add labrastro_server reuleauxcoder tests
git -C D:\AboutDEV\Labrastro\Labrastro commit -m "refactor(remote): remove relay tool execution backend"
```

### Task 5: Remove Go Peer Poll Loop Execution Path

**Files:**
- Modify: `reuleauxcoder-agent/internal/runner/runner.go`
- Modify: `reuleauxcoder-agent/internal/client/http.go`
- Modify: `reuleauxcoder-agent/internal/protocol/types.go`
- Modify: `reuleauxcoder-agent/internal/runner/runner_test.go`
- Modify: `reuleauxcoder-agent/internal/mcp/*`
- Modify: `reuleauxcoder-agent/internal/tools/*` only to adapt entrypoints

- [ ] **Step 1: Replace poll loop tests**

Delete tests whose only value is:

```go
runPollLoopForTest(...)
```

New local action tests must target claim/result/progress. New peer lifecycle tests must target peer status reporting. No new test may target `/remote/poll`.

- [ ] **Step 2: Remove `runPollLoop` normal path**

In `runner.go`, remove normal startup routes that call:

```go
return r.runPollLoop(...)
```

Do not keep poll for unsupported modes.

- [ ] **Step 3: Remove `/remote/poll` client method**

In `client/http.go`, delete:

```go
func (c *HTTPClient) Poll(...)
```

No `Poll` method, `/remote/poll` route, or `RelayEnvelope` tool execution model may remain active. Any future diagnostic heartbeat must use an explicitly named non-poll endpoint and a non-relay protocol type.

- [ ] **Step 4: Reattach reusable tools to local action runner**

Keep local tool implementations where useful, but invoke AgentRun-visible work only from the local action runner. Explicit local-only background tasks must use `admin_task_scoped` local actions and write task projection events.

- [ ] **Step 5: Run Go tests**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro\reuleauxcoder-agent
go test ./...
```

Expected: PASS. No test depends on `/remote/poll`.

- [ ] **Step 6: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro add reuleauxcoder-agent
git -C D:\AboutDEV\Labrastro\Labrastro commit -m "refactor(peer): remove poll-loop tool execution"
```

### Task 6: Webview Process Cards For Local Action Waiting And Failure

**Files:**
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\sessionRuntime\*`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\LabrastroController.ts`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\components\chat\SessionTurn.tsx`
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\webview-ui\src\components\chat\transcript-presentation.ts`
- Modify: webview and extension tests near these files

- [ ] **Step 1: Add reducer/presentation tests**

Create tests for visible local action states:

```ts
{
  kind: "local_action",
  status: "waiting_peer",
  actionKind: "read_workspace_file",
  workspaceRoot: "D:\\AboutDEV\\vika_mcp"
}
```

Expected visible label:

```ts
expect(rendered.text()).toContain("等待本地工作区连接")
expect(rendered.text()).toContain("D:\\AboutDEV\\vika_mcp")
```

For failure:

```ts
expect(rendered.text()).toContain("本地动作失败")
expect(rendered.text()).toContain("重试")
expect(rendered.text()).toContain("取消")
```

- [ ] **Step 2: Add no-spinner-only regression**

Add a test asserting a waiting local action produces a process card and not only the generic busy indicator.

Expected:

```ts
expect(screen.getByText(/等待本地工作区连接/)).toBeTruthy()
expect(screen.queryByText(/^处理中$/)).toBeNull()
```

If generic busy text remains elsewhere in the page, scope the assertion to the turn/process card container.

- [ ] **Step 3: Implement projection rendering**

Map local action projection events to readable process cards. Use action-specific messages. Do not render raw payload JSON.

- [ ] **Step 4: Run focused tests**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
npx vitest run src webview-ui/src --run
npm run typecheck
```

- [ ] **Step 5: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension add src webview-ui
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension commit -m "feat(chat): render local peer action states"
```

### Task 7: MCP And Capability Local Peer Rebinding

**Files:**
- Modify: backend capability package local peer install endpoints/tests
- Modify: `D:\AboutDEV\Labrastro\Labrastro-vscode-extension\src\CapabilityPackageLocalPeerRunner.ts`
- Modify: MCP server dashboard/status code under backend and extension
- Modify: `reuleauxcoder-agent/internal/mcp/*`

- [ ] **Step 1: Preserve target-fact contract**

Tests must still prove:

```text
Server cannot mark local peer installed/verified without peer result.
User-scoped local secrets are never sent to LLM or server logs.
MCP runtime truth distinguishes active, connected, failed, and unavailable.
```

- [ ] **Step 2: Remove any dependency on old relay execution**

Capability local peer checks/install and MCP lifecycle must use local action. They must not use `/remote/poll`, `RemoteRelayToolBackend`, relay tool envelope, or independent typed peer result endpoints as a second execution/result path. A peer target fact endpoint is allowed only when it requires `local_action_id`; desired action identity is additional matching metadata, not a substitute for `local_action_id`. Completion must append `local_action_completed` / `local_action_failed` to the unified projection.

- [ ] **Step 3: Run capability/MCP focused tests**

```powershell
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\http\test_remote_service.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
npx vitest run src/CapabilityPackageLocalPeerRunner.test.ts src/LabrastroRemoteClient.test.ts
```

- [ ] **Step 4: Commit**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro add labrastro_server reuleauxcoder-agent tests
git -C D:\AboutDEV\Labrastro\Labrastro commit -m "refactor(capability): bind peer facts to local actions"
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension add src
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension commit -m "refactor(capability): report local peer action facts"
```

### Task 8: End-To-End Verification And Packaging

**Files:**
- Modify tests that still assert removed legacy paths.
- No implementation shortcuts allowed in this task.

- [ ] **Step 1: Backend full relevant regression**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro
D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server tests\interfaces tests\domain --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex
```

Expected: PASS.

- [ ] **Step 2: Go peer tests**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro\reuleauxcoder-agent
go test ./...
```

Expected: PASS.

- [ ] **Step 3: Extension tests and typecheck**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
npx vitest run src webview-ui/src --run
npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Structure scans**

```powershell
rg -n "/remote/poll|RemoteRelayToolBackend|send_exec_request|send_preview_request|request_cleanup|cancel_pending_requests|runPollLoop|--agent-run-worker" D:\AboutDEV\Labrastro\Labrastro D:\AboutDEV\Labrastro\Labrastro-vscode-extension -g "*.py" -g "*.go" -g "*.ts" -g "*.tsx"
```

Expected:

- No production hit proving old relay/poll execution.
- `--agent-run-worker` is allowed only in non-mainline explicit local worker tests/docs for an approved local-only worker feature. Any appearance in default VSIX peer startup fails this task.

- [ ] **Step 5: VSIX packaging**

```powershell
cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
npm run package:vsix
```

Expected: `labrastro-vscode.vsix` generated successfully.

- [ ] **Step 6: Manual smoke after deployment**

Manual smoke must verify:

1. VS Code local peer offline, normal chat still produces process cards and final response.
2. VS Code local peer online, normal chat still runs server-owned mainline.
3. A task requiring local workspace shows explicit local action waiting card.
4. Local peer offline during local action shows recoverable waiting/failure state.
5. No session remains forever in queued/processing without visible reason.

- [ ] **Step 7: Final commits and branch parity**

```powershell
git -C D:\AboutDEV\Labrastro\Labrastro status --short --branch
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension status --short --branch
git -C D:\AboutDEV\Labrastro\Labrastro diff --check
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension diff --check
```

Expected:

- No uncommitted implementation changes after final commits.
- No whitespace errors.
- Main branches aligned with origin after push when publishing is requested.

## Acceptance Criteria

The work is not complete until all criteria are true.

- Ordinary chat submits server-owned AgentRun by default.
- Ordinary chat remains usable when local peer is offline.
- local peer cannot claim ordinary `remote_server` AgentRuns.
- local peer is never required for model provider requests in ordinary chat.
- provider secrets remain server-side.
- No default VSIX startup path uses `--agent-run-worker` to make ordinary chat work.
- Old `/remote/poll` no longer carries tool execution.
- `RemoteRelayToolBackend` no longer exists as a production Agent tool backend.
- MCP no longer depends on old poll tool execution.
- Capability local peer install/check facts do not use old relay tool execution.
- Local peer actions are modeled as AgentRun/SessionRun child events.
- Frontend shows explicit process cards for local action waiting/progress/failure.
- Frontend does not show only “处理中” for local-peer waiting states.
- Tests prove no ordinary chat dependency on local peer.
- Structure scans show no production old execution path.
- Deployment smoke proves server-owned chat and local-action waiting behavior.

## Explicit Non-Goals

- Do not build a migration path for old DB records.
- Do not preserve old relay endpoints for compatibility.
- Do not create a second local peer execution API outside AgentRun/SessionRun projection.
- Do not use local peer as normal chat LLM host.
- Destructive branch deletion is out of scope for this plan.
- Do not hide incomplete local action semantics behind generic busy UI.

## Commands For Plan Self-Review

Run before asking for implementation approval:

```powershell
$patterns = @("TB" + "D", "TO" + "DO", "implement" + " later", "fill in" + " details", "if" + " possible", "tempor" + "ary")
Select-String -LiteralPath D:\AboutDEV\Labrastro\Labrastro\docs\superpowers\plans\2026-06-22-centralized-agentrun-local-peer-convergence.md -Pattern ($patterns -join "|")
git -C D:\AboutDEV\Labrastro\Labrastro status --short --branch
git -C D:\AboutDEV\Labrastro\Labrastro-vscode-extension status --short --branch
```

Expected:

- No placeholder language requiring later interpretation.
- Only this plan document changed before implementation approval.

## Review Checklist For The User

Please review these closed-principle checks before approving execution:

- The document states “中心化自托管” as the main product positioning.
- Ordinary chat is server-owned by default.
- local peer responsibilities are limited to explicit local actions, target facts, MCP lifecycle/status/invocation, capability checks/install, and non-mainline local tasks.
- MCP and local capability package facts are preserved without keeping old poll execution or independent result semantics.
- The no-compatibility and no-short-term-stopgap rules are hard requirements, not preferences.
- local action process cards and action states are sufficient to prevent the “only spinner” failure mode.

Execution must not start until this document is explicitly approved.
