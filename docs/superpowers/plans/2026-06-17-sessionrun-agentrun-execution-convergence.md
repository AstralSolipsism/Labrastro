# SessionRun/AgentRun Execution Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `SessionRun` 直接执行面系统性收敛为 `SessionRun` 交互投影 + `AgentRun` mainline/activation 唯一执行事实源，消除旧 follow-up 链路、双接口、双状态模型和投影漂移。

**Architecture:** 一个 ChatView 会话分支绑定一个长期存在的 `AgentRun` mainline。首轮创建 mainline；普通后续发言、任务推进、能力包修订和候选修复都在同一 mainline 上创建后续 activation。运行中明确要影响当前执行的输入进入当前 activation 的 `ActivationSteer` mailbox。`SessionRun` 只拥有 transcript、projection、用户输入、审批与反馈交互语义，不拥有模型执行、工具执行或 executor 写入权。

**Tech Stack:** Python backend, Postgres runtime store, Go peer executor, VSCode TypeScript/Solid webview, pytest, Go test, Vitest, GitNexus.

---

## Read First

本文档是后续执行的准则。若旧聊天记录、临时分析、局部补丁说明与本文档冲突，按本文档执行。

本次工作处在开发期，按以下原则推进：

- 架构统一：一个执行事实源，一个交互投影面，一个公开功能只保留一个 HTTP 入口。
- 逻辑闭环：接口、协议模型、route、service、store、projection、Go peer、VSCode client/reducer/renderer 和测试必须同步收敛。
- 边界清晰：`SessionRun` 不执行，`AgentRun` 执行；branch/fork 创建新 mainline，普通继续发言不创建新 mainline。
- 不保留冗余兼容：删除旧路径，不在旧 follow-up 与新 steer 之间保留 fallback、alias、双写或双读。
- 用户体验优先但不牺牲语义：ChatView 可以让编辑历史消息自动进入新 branch；后端必须用显式 branch binding 表达，不隐式改写原分支。

## Non-Negotiable Decisions

这些是已确认边界，不再作为执行中的开放决策。

- 不存在独立于 `AgentRun` 的普通聊天执行路径。当前 `SessionRun` 直连执行链路是旧执行面，必须收敛。
- `SessionRun` 是交互事实源和投影入口；`AgentRun` 是执行事实源。
- `POST /remote/session-runs/start` 只用于首轮：创建或绑定 `SessionRun` transcript、`AgentRun` mainline 和首个 activation。它不是 start-or-continue 混合入口。
- `session-runs/start` 的 `client_request_id` 只服务首轮幂等；后续普通发言不得复用 start，也不得让 start 在后端隐式判断“有 binding 就 continue”。
- `POST /remote/session-runs/continue` 是空闲态后续用户消息入口；它必须要求已有 `SessionRun` -> `AgentRun` binding，并在同一 mainline 上创建下一 activation。
- 用户在 ChatView 空闲时输入，是同一 `AgentRun` mainline 上的下一 activation，不是 steer，也不是新建独立 `AgentRun`。
- 用户明确要影响当前正在运行的 activation 时，输入进入 user/peer-facing `ActivationSteer`。
- 当前 activation 已结束时，输入必须转为同一 mainline 的 `session-runs/continue`；UI 不得报警后丢失输入，也不得回退到旧 follow-up，后端 steer endpoint 也不得偷偷创建下一 activation。
- `SessionRun` -> `AgentRun` mainline binding 必须是一等持久化事实。所有执行相关操作都通过这条 binding 解析目标 `AgentRun`。
- 不得从 event payload、metadata、`session_hint`、`client_request_id`、UI state 或最近创建关系推断执行目标。
- `branch` 和 `fork` 是创建新 `AgentRun` mainline 的唯一会话关系操作。普通发言、任务步骤推进、能力包修订、候选修复都不得新开 mainline。
- 编辑历史用户消息是一种显式 branch 用户动作：提交编辑后创建 derived `AgentRun` mainline，自动选择 derived branch，并从被编辑消息之后重新执行。
- 只有用户消息能执行 edit-and-branch。assistant 输出、tool 结果、approval 结果、runtime event、system/developer 指令都是不可原地编辑的事实记录。
- 编辑历史用户消息创建新 branch 不取消源 branch 的 active activation。
- 新 branch 如果 executor 容量可用就运行；容量不足时排队。不得因为 runtime slot 满而拒绝 branch 结构创建。
- `/remote/admin/agent-runs/steer` 是 admin/operator 控制面，不作为 VSCode ChatView 的普通用户输入接口。
- 必须新增 user/peer-facing steer 入口，例如 `POST /remote/agent-runs/{agent_run_id}/steer`。
- `/remote/session-runs/follow-up` 和 `/remote/session-runs/follow-up/cancel` 必须删除。
- `Agent.queue_follow_up()`, `Agent.cancel_follow_up()`, `Agent.consume_follow_ups()`, `AgentLoop._inject_pending_follow_ups()` 必须删除或改造成命名明确的 AgentRun steer 消费实现。
- 旧 follow-up 不是把文字注入正在输出的 LLM 回复。当前代码只是保存 pending ticket，在下一次 LLM 调用前插入 synthetic guidance；如果没有下一次调用，会出现 unconsumed。目标架构必须用 activation steer 的安全消费边界表达这类运行中指导。
- 本计划不实现“中断当前 token stream 并立即重发 LLM 请求”的新能力。`ActivationSteer` 只在 executor 安全边界消费；未来若做 stream abort/retry，必须作为独立能力建模，不能借旧 follow-up 名义混入。
- 能力包 ingest、draft revision、candidate repair 复用同一 package-bound `AgentRun` mainline，通过 activation/continue 推进。
- 能力包 draft/approval 阶段的草稿修订不是 same-activation steer；它发生在等待用户反馈之后，走 feedback/continue 或新 activation。
- 公开接口字段统一 snake_case。开发期不保留 camelCase alias，例如 `clientRequestId`、`sessionId`。
- Capability ingest 公开入口只保留 `/remote/admin/capability-packages/ingest/session/start`；旧 `/remote/admin/capability-packages/ingest/start` 和 `/remote/admin/capability-packages/ingest/status` 不保留为可调用 HTTP 面。
- 独立 activation event 上报和 `complete.events` 内嵌 event 是两种契约。前者必须携带 claim 身份；后者只携带 executor 产出的 event 内容。

## Closed Drift Anchors

这些锚点已在本计划执行中收敛；后续如果扫描重新出现，按回归处理，不重新作为开放决策讨论。

- Registry 已移除 `session_run.follow_up` 和 `session_run.follow_up_cancel`，并暴露 `session_run.continue`。
- `service.py` 和 `routes/chat.py` 已删除旧 follow-up 分支，`RemoteSessionRun` follow-up ticket 与 `session_run_follow_up_*` 事件链已移除。
- ChatView 和 VSCode client 已删除 `followUpSessionRun()` 可执行路径，运行中指导只走 user-facing AgentRun steer。
- `session-runs/start/events/continue/status/recover/cancel/user-input/reply` 已收敛为 AgentRun-backed projection/binding 读写，不再通过 `_RemoteSessionRun` 或 `_stream_session_run` 驱动模型执行。
- Go heartbeat 已携带、消费并确认 `activation_steers` / `delivered_steer_ids`。
- AgentRun protocol 已集中在 `protocol/agent_runs.py`，heartbeat、steer、branch、fork、activation event、activation complete 均有 canonical model。
- standalone activation event 和 completion nested event 已分离，claim 身份只属于 standalone event 上报契约。
- Capability ingest public surface 已收敛到 `/remote/admin/capability-packages/ingest/session/start`。
- Convergence surface public request parser 已按 snake_case 收敛，旧 camelCase public alias 由 contract scan 防回归。
- Branch/fork 已形成 branch binding、structural-sharing transcript projection、selected branch partition 和 sibling summary。

## Target Contracts

### SessionRun Remote API

- `POST /remote/session-runs/start`
  - 仅首轮使用。
  - 创建或绑定 `SessionRun` transcript。
  - 创建 `AgentRun` mainline。
  - 启动首个 activation。
  - `client_request_id` 只做首轮幂等。
  - 不得在已有 binding 时隐式执行 continue。
- `POST /remote/session-runs/continue`
  - 后续普通用户消息使用。
  - 要求已有 `SessionRun` -> `AgentRun` binding。
  - 在同一 mainline 上创建下一 activation。
  - 不得创建新 mainline。
- `POST /remote/session-runs/events`
  - 只读 transcript/projection。
  - 不触发模型、工具或 executor 执行。
  - 若继续保持流式读取形态，也只能读取 AgentRun activation 投影或事件，不能调用 `session_run_events_handler` 来执行 prompt。
- `POST /remote/session-runs/status`
  - 只读 projection/status。
  - 不拥有执行状态写入权。
- `POST /remote/session-runs/recover`
  - 映射到绑定 `AgentRun` 的 recovery/continue 语义。
  - 不得恢复独立 `SessionRun` executor。
- `POST /remote/session-runs/cancel`
  - 映射到 selected branch binding 对应 `AgentRun` 的当前 activation 或 run cancel。
  - 不广播到 sibling branch。
- `POST /remote/session-runs/user-input/reply`
  - 保留为审批、反馈、用户输入请求 reply。
  - 目标必须是 selected branch binding 发出的 pending request。
- `POST /remote/session-runs/follow-up`
  - 删除。
- `POST /remote/session-runs/follow-up/cancel`
  - 删除。

### AgentRun Remote API

- 新增 user/peer-facing `POST /remote/agent-runs/{agent_run_id}/steer`。
- 请求携带 steer item、idempotency key、session/peer binding 身份、期望 activation 身份。
- 服务端通过 binding 校验当前 peer/session 是否能 steer 目标 `AgentRun`。
- 服务端通过 current activation 校验目标是否 still steerable。
- 通过校验后写入 `append_activation_steer()`。
- 响应必须表达可操作结果：`accepted`, `duplicate`, `agent_run_not_steerable`, `activation_mismatch`, `forbidden`, `not_found`。
- 没有 active activation 时返回 HTTP 409 + `agent_run_not_steerable`；ChatView 将同一输入转为 branch-local `pending_next_turn` 或在空闲态直接送入 `session-runs/continue`。
- Steer endpoint 不创建下一 activation，不保存 next-turn 草稿，不降级调用 SessionRun follow-up。
- Admin steer 与 user steer 可以共用 store mailbox，但 HTTP 权限、审计字段和调用面分开。

### Binding Fact Source

- `SessionRun` -> `AgentRun` mainline binding 是唯一写入事实。
- `session-runs/continue`, `session-runs/status`, `session-runs/events`, `session-runs/recover`, `session-runs/cancel`, user-facing steer 都必须通过 binding 解析目标。
- `session_hint`, `client_request_id`, event payload、transcript metadata、UI state 只能是请求、审计或投影数据。
- 缺失 binding 时，continue、recover、cancel、user-facing steer 显式失败。
- 缺失 binding 时不得扫描 events、推断 metadata、自动创建 binding。
- `branch` 和 `fork` 创建新的 `AgentRun` mainline relation，并为需要呈现的新 transcript 创建新的 branch binding。
- branch/fork 不覆盖源 `SessionRun` binding。
- read model 可以携带 binding id 方便展示，但写操作和 ownership check 必须回到 persisted binding fact。

### User Input Routing

- 没有 binding 的首轮输入：`session-runs/start`。
- 有 binding 且 selected branch 没有 active activation 的普通输入：`session-runs/continue`。
- 有 binding 且 selected branch 正在运行，用户明确要影响当前执行：user-facing `agent-runs/{agent_run_id}/steer`。
- 有 binding 且 selected branch 正在运行，用户提交普通后续对话：记录 branch-local `pending_next_turn`，当前 activation 结束后用 `session-runs/continue` 发送。
- `pending_next_turn` 是未来一轮普通对话，不是 old follow-up。
- `pending_next_turn` 不发 `session_run_follow_up_*`，不调用 `Agent.queue_follow_up()`，不插入 synthetic guidance。
- `pending_next_turn` 只属于创建它的 selected branch；切换 branch 不携带它，不广播给 sibling branch。
- 用户可以取消或编辑未发送的 `pending_next_turn`。
- 运行中 steer 若撞上已结束 activation，UI/controller 将同一输入转成 `pending_next_turn` 或下一轮 continue，不显示成终止性错误。
- 一个 branch 同时最多一个 active activation。`pending_next_turn` 用来维护这个约束，不是并行 activation 入口。

### Branch UX

- 编辑历史用户消息是显式 branch action。
- 只有 user-authored conversation message 可编辑成新 branch。
- assistant output、tool result、approval result、runtime event、system/developer instruction 不可原地编辑。
- 非用户事实最多提供 "fork from here"，并在该点之后追加新的用户输入；纠正事实输出只能通过新输入或新事件表达。
- UI 可以在编辑提交后自动选择 derived branch，让用户直接看到新方向执行。
- 自动选择 derived branch 是 UI 对编辑动作的响应，不是后端根据最近创建 relation 隐式改写 source binding。
- 编辑历史用户消息不取消 source branch 的 active activation。
- source branch 继续运行、完成、等待、失败或由用户单独取消。
- 新 branch 可立即运行或排队；branch binding、relation 和编辑后的 user-message delta 必须先持久化，再进入 scheduler。
- executor capacity、调度公平性和排队策略属于 runtime/control plane，不由 ChatView 另建策略。
- selected branch 是当前 ChatView input target。
- 普通输入、审批 reply、用户输入 reply、cancel、recover、continue 只作用于 selected branch binding。
- sibling branch 状态可见，但 sibling transcript 不混入 selected branch transcript。
- UI 在被编辑消息附近提供左右切换和数字指示，表达 sibling branch alternatives。
- 切换 branch 只改变 selected binding，不 stop、不 delete、不 cleanup。
- hide branch 只影响当前 UI 可见性，不 cancel、不 cleanup。
- stop branch 是显式动作，取消该 branch active activation/run，保留 transcript、relation、binding 历史。
- delete branch 是显式破坏性动作；若 branch 正在运行，先 cancel，再 close binding，并只清理该 branch 自有 worktree/artifact/resource。
- 后端命令必须区分 select/switch、hide、stop branch run、close binding、delete/cleanup resources，不能合并成一个含糊的 branch close 接口。
- parent 和 sibling branch 不受 stop/delete 派生操作影响。

### Branch Transcript Storage

- branch transcript 使用 structural sharing。
- derived branch 不复制分叉点之前的完整 transcript。
- branch binding 记录 parent branch binding id、`base_session_item_id`、source `AgentRun` id、target `AgentRun` id、branch-local deltas。
- 渲染 selected branch = parent prefix up to `base_session_item_id` + selected branch delta。
- LLM context 使用同一 composed projection：shared parent prefix + selected branch delta。
- materialized projection/cache 可以存在，但只能由 branch DAG 重建，不能成为第二套历史事实。
- 存储增长来自 branch-local messages、branch-local artifacts、runtime events、worktree checkout，不来自每个 branch 复制全量历史。
- 现有 `base_session_item_id` relation metadata 必须进入可执行投影逻辑，不能只停留在 relation 记录。

### Branch Event Subscription

- `session-runs/events` 读取 selected branch binding/session id。
- ChatView 只渲染 selected branch transcript events。
- sibling branch 状态用轻量 projection metadata 表达。
- sibling metadata 至少包括 branch binding id、agent run id、status、`has_updates`、`last_event_at`、current branch index、total sibling count。
- non-selected branch 的 streaming text/tool/runtime events 不插入 selected branch transcript。
- non-selected branch 更新只改变 branch controls、running indicator、unread/update state。
- 切换 branch 后重新加载或 resume selected branch projection，并改变 future input target。
- 后端可以观察多个 AgentRun；UI 输出必须按 selected branch binding 分区。

### Approval And Pending Input

- pending approval/user-input 属于发出它的 branch binding。
- reply 只能在对应 branch 被 selected 时提交，或由 UI 显式携带目标 branch binding 提交。
- sibling branch 有 pending approval/input 时，UI 可显示 branch-level attention。
- sibling pending state 不得锁住当前 selected branch 的普通输入。
- selected branch status 与 sibling summaries 必须分开。
- 后端 ownership check 必须包含 selected binding/session id 与目标 `AgentRun`/activation 身份。

### Capability Packages

- Public ingest start 只保留 `/remote/admin/capability-packages/ingest/session/start`。
- 删除 public handling for `/remote/admin/capability-packages/ingest/start`。
- 删除 public handling for `/remote/admin/capability-packages/ingest/status`。
- package ingest、draft revision、candidate repair 复用 package-bound `AgentRun` mainline。
- 每次需要模型继续处理的修订创建下一 activation。
- 只有真实运行中指导使用 `ActivationSteer`。
- draft/approval feedback 后的修订走 feedback/continue 或新 activation。
- capability package 代码不得依赖 `SessionRun` follow-up queue。

### Protocol And Field Style

- AgentRun request/response models 放入 `labrastro_server/interfaces/http/remote/protocol/agent_runs.py`。
- SessionRun chat/projection models 保留在 `protocol/chat.py`，并加入 `SessionRunContinueRequest/Response`。
- route handler 通过 protocol model parse，不直接从公开 payload 上散落 `payload.get(...)`。
- registry 和 `contracts.json` 由 canonical protocol models 驱动。
- 本计划 convergence surface 内的任何非 `none` request/response shape 都必须有 Python protocol model、contracts fixture 和对应 parser test。范围包括 `session_run.*`、`agent_runs.*`、`agent_run_activations.*`、`admin.agent_runs.*` 和 capability ingest session start。不得出现这些范围内的 registry-only endpoint 或 route-only public endpoint。
- 独立 activation event report 与 completion nested event 分成两个模型：
  - standalone event report: event content + claim identity。
  - completion nested event: event content only。
- 公开 request 只接受 snake_case。
- 删除 `clientRequestId`, `sessionId` 等 camelCase input alias。
- 测试 fixture、Go struct json tag、VSCode payload 字段必须同步 snake_case。

### Peer Heartbeat

- Python heartbeat request 继续接收 `delivered_steer_ids`。
- Python heartbeat response 继续返回 `activation_steers`。
- Go `reuleauxcoder-agent/internal/protocol/types.go` 增加 heartbeat steer fields。
- Go client 在 heartbeat 中提交 delivered steer ids。
- Go runner 消费 response `activation_steers`。
- executor 只在安全边界消费 steer。
- steer delivery 状态必须闭环：accepted -> delivered -> consumed 或 rejected/expired/cancelled。
- 未 delivered 的 steer 允许在 heartbeat 后续响应中重送。

## File Map

Backend protocol and route files:

- `labrastro_server/interfaces/http/remote/protocol/chat.py`
- `labrastro_server/interfaces/http/remote/protocol/agent_runs.py`
- `labrastro_server/interfaces/http/remote/protocol/registry.py`
- `labrastro_server/interfaces/http/remote/protocol/contracts.json`
- `labrastro_server/interfaces/http/remote/routes/chat.py`
- `labrastro_server/interfaces/http/remote/routes/agent_runs.py`
- `labrastro_server/interfaces/http/remote/routes/capability_packages.py`
- `labrastro_server/interfaces/http/remote/service.py`

Backend runtime and projection files:

- `labrastro_server/services/agent_runtime/control_plane.py`
- `labrastro_server/services/agent_runtime/postgres_store.py`
- `labrastro_server/services/agent_runtime/worktree.py`
- `labrastro_server/services/capability_packages.py`
- `reuleauxcoder/domain/agent/loop.py`
- `reuleauxcoder/interfaces/entrypoint/remote_relay.py`

Go peer files:

- `reuleauxcoder-agent/internal/protocol/types.go`
- `reuleauxcoder-agent/internal/client/http.go`
- `reuleauxcoder-agent/internal/runner/runner.go`
- `reuleauxcoder-agent/internal/protocol/contracts_test.go`
- `reuleauxcoder-agent/internal/runner/runner_test.go`

VSCode files:

- `../Labrastro-vscode-extension/src/LabrastroRemoteClient.ts`
- `../Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.ts`
- `../Labrastro-vscode-extension/src/sessionRunEventViews.ts`
- `../Labrastro-vscode-extension/src/LabrastroRemoteClient.test.ts`
- `../Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.test.ts`
- `../Labrastro-vscode-extension/src/sessionRunEventViews.test.ts`

Backend tests:

- `tests/labrastro_server/http/test_protocol.py`
- `tests/labrastro_server/http/test_remote_service.py`
- `tests/labrastro_server/services/agent_runtime/test_contract_scan.py`
- `tests/labrastro_server/services/agent_runtime/test_control_plane.py`
- `tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py`
- `tests/labrastro_server/services/test_capability_packages.py`
- `tests/domain/agent/test_loop.py`
- `tests/interfaces/entrypoint/test_runner_remote_exec.py`

## Execution Tasks

### Task 1: Add Contract Guards First

Purpose: make the old paths fail tests before implementation removes them.

- [x] Update `tests/labrastro_server/services/agent_runtime/test_contract_scan.py`.
- [x] Add source guard failures for `/remote/session-runs/follow-up`, `/remote/session-runs/follow-up/cancel`, `SessionRunFollowUpRequest`, `SessionRunFollowUpCancelRequest`, `session_run_follow_up_`, `queue_follow_up`, `cancel_follow_up`, `consume_follow_ups`, `_inject_pending_follow_ups`.
- [x] Add source guard failures for public `/remote/admin/capability-packages/ingest/start` and `/remote/admin/capability-packages/ingest/status`.
- [x] Add guard for camelCase public request aliases `clientRequestId` and `sessionId`.
- [x] Add guard that `session_run.continue` exists in registry and contracts.
- [x] Add guard that AgentRun protocol models live in `protocol/agent_runs.py`.
- [x] Add registry guard: every runtime convergence endpoint with a request/response shape has a Python model, contract fixture, and parser test.
- [x] Add behavior guard that `session-runs/start/events/continue` do not execute prompts through `session_run_events_handler` or `_RemoteSessionRun`; they must create/read AgentRun-backed projection.

Acceptance:

- [x] Guard tests fail on current old follow-up code before deletion.
- [x] Guard tests pass after later tasks remove old paths.

### Task 2: Canonicalize Protocol Models

Purpose: stop route-level ad hoc parsing from becoming a second protocol.

- [x] Create `labrastro_server/interfaces/http/remote/protocol/agent_runs.py`.
- [x] Move or define AgentRun steer, heartbeat, branch, fork, activation event, activation complete models in `agent_runs.py`.
- [x] Split standalone activation event report from completion nested event item.
- [x] Update `protocol/chat.py` with `SessionRunContinueRequest/Response`.
- [x] Remove SessionRun follow-up protocol models.
- [x] Update `protocol/__init__.py`, `registry.py`, and `contracts.json`.
- [x] Update `tests/labrastro_server/http/test_protocol.py` for registry, contracts and snake_case-only payloads.
- [x] Remove registry-only and route-only public endpoints inside the runtime convergence surface by either adding the canonical model/fixture or deleting the endpoint.

Acceptance:

- [x] Registry exposes `session_run.start`, `session_run.continue`, `session_run.events`, `session_run.status`, `session_run.recover`, `session_run.cancel`, `session_run.user_input_reply`.
- [x] Registry does not expose `session_run.follow_up` or `session_run.follow_up_cancel`.
- [x] AgentRun heartbeat and steer contracts are represented by canonical models.
- [x] `AgentRunActivationEventReport` is no longer reused for completion nested event content.

### Task 3: Persist And Enforce SessionRun-AgentRun Binding

Purpose: make binding the only way SessionRun execution operations find an AgentRun.

- [x] Add or normalize persisted branch/session binding schema in `PostgresAgentRunStore`.
- [x] Ensure binding stores session id, branch binding id, selected state, mainline `agent_run_id`, parent branch binding id, `base_session_item_id`, source `AgentRun` id, target `AgentRun` id.
- [x] Update in-memory control plane equivalent for tests.
- [x] Update `routes/chat.py` so start creates binding + mainline + first activation.
- [x] Remove direct prompt execution from `session_run_events_handler`/`_RemoteSessionRun`; any remaining streaming adapter reads AgentRun-backed projection only.
- [x] Update `reuleauxcoder/interfaces/entrypoint/remote_relay.py` so ChatView execution enters AgentRun activation flow, not local SessionRun -> `peer_agent` execution.
- [x] Add `session-runs/continue` route and service dispatch.
- [x] Make continue fail when binding is missing.
- [x] Make status/events/recover/cancel/user-input reply resolve through selected binding.
- [x] Ensure branch/fork creates new relation and derived binding without overwriting source binding.
- [x] Add tests in `test_control_plane.py`, `test_postgres_runtime_store.py`, and `test_remote_service.py`.

Acceptance:

- [x] First ChatView message uses start and creates one mainline.
- [x] Second ordinary ChatView message uses continue and creates a new activation on the same mainline.
- [x] Continue never creates a new mainline.
- [x] Start never acts as implicit continue.
- [x] SessionRun events/status read AgentRun-backed projection and do not drive model/tool execution.
- [x] Branch/fork creates a derived mainline relation and preserves source binding.
- [x] Missing binding returns explicit error for continue, recover, cancel and user-facing steer.

### Task 4: Implement User-Facing AgentRun Steer

Purpose: replace SessionRun follow-up with activation mailbox semantics.

- [x] Add user/peer-facing route in `routes/agent_runs.py` for `POST /remote/agent-runs/{agent_run_id}/steer`.
- [x] Register route in `service.py` and `registry.py`.
- [x] Parse request with `protocol/agent_runs.py` model.
- [x] Check session/peer ownership through binding.
- [x] Check current activation and expected activation id.
- [x] Call `append_activation_steer()` with idempotency key.
- [x] Return `accepted`, `duplicate`, `agent_run_not_steerable`, `activation_mismatch`, `forbidden`, `not_found`.
- [x] Return HTTP 409 for `agent_run_not_steerable`.
- [x] Add tests for accepted, duplicate, no-active-activation, mismatched activation, forbidden session/peer, and missing binding.

Acceptance:

- [x] Running current-activation guidance uses user-facing AgentRun steer.
- [x] No old SessionRun follow-up endpoint is needed for running guidance.
- [x] Steer endpoint never creates a next activation and never writes `pending_next_turn`.
- [x] Ended activation routes ordinary input to continue rather than failed steer UX.

### Task 5: Complete Heartbeat Steer Delivery In Go Peer

Purpose: make accepted steer reach executor and close delivery acknowledgements.

- [x] Update `reuleauxcoder-agent/internal/protocol/types.go` heartbeat request/response fields.
- [x] Update `internal/client/http.go` to send `delivered_steer_ids`.
- [x] Update `internal/runner/runner.go` to receive `activation_steers`.
- [x] Apply steer only at safe execution boundaries.
- [x] Record delivered steer ids and include them in later heartbeats.
- [x] Update `internal/protocol/contracts_test.go`, `internal/client/http_test.go`, and `internal/runner/runner_test.go`.

Acceptance:

- [x] Go contracts match Python contracts and snake_case JSON fields.
- [x] Accepted steer is delivered to the active executor.
- [x] Delivery acknowledgement prevents unbounded duplicate delivery.
- [x] No Go peer field uses camelCase public JSON tags.

### Task 6: Delete SessionRun Follow-Up Chain

Purpose: remove the old second input path completely.

- [x] Delete follow-up routes from `routes/chat.py`.
- [x] Delete follow-up dispatch from `service.py`.
- [x] Delete follow-up entries and protocol models from registry/contracts.
- [x] Delete `RemoteSessionRun.submit_follow_up()` and cancel/consume follow-up ticket methods.
- [x] Delete `Agent.queue_follow_up()`, `Agent.cancel_follow_up()`, `Agent.consume_follow_ups()`.
- [x] Delete `AgentLoop._inject_pending_follow_ups()`.
- [x] Replace tests that asserted follow-up behavior with start/continue/steer behavior tests.
- [x] Run source scan for `follow_up`, `follow-up`, `session_run_follow_up`, `queue_follow_up`, `_inject_pending_follow_ups`.

Acceptance:

- [x] No HTTP route can submit old follow-up.
- [x] No domain agent method stores out-of-band follow-up tickets.
- [x] No frontend event path listens for `session_run_follow_up_*`.
- [x] Any remaining `follow` text is documentation or unrelated domain language, not executable API.

### Task 7: Converge Capability Package Interfaces

Purpose: keep package workflows on AgentRun mainline semantics and remove old admin ingest surface.

- [x] Update `routes/capability_packages.py` to expose only `/remote/admin/capability-packages/ingest/session/start`.
- [x] Remove public handling for old `/ingest/start` and `/ingest/status`.
- [x] Update `labrastro_server/services/capability_packages.py` so ingest/revision/repair reuse package-bound `AgentRun` mainline.
- [x] Route draft/approval feedback revisions through feedback/continue or new activation.
- [x] Use `ActivationSteer` only for true runtime guidance while activation is active.
- [x] Remove dependencies on SessionRun follow-up queue.
- [x] Update `tests/labrastro_server/services/test_capability_packages.py` and related capability package tests.

Acceptance:

- [x] Public capability ingest has one start endpoint.
- [x] Package revision does not call old follow-up APIs.
- [x] Package draft revision after approval feedback creates the correct next activation.

### Task 8: Implement Branch Transcript DAG And Projection

Purpose: make branch UX real without copying full transcript history.

- [x] Persist branch binding relation fields: parent branch binding id, `base_session_item_id`, source `AgentRun` id, target `AgentRun` id.
- [x] Store branch-local transcript deltas separately from shared parent prefix.
- [x] Build selected branch projection from parent prefix plus branch-local delta.
- [x] Build LLM context from the same composed projection.
- [x] Keep materialized projections rebuildable from branch DAG.
- [x] Add branch sibling summaries to status/events projection.
- [x] Partition event streams by selected branch binding.
- [x] Add tests for edit-message branch, switch branch, sibling updates, pending approval on sibling, and non-copying storage behavior.

Acceptance:

- [x] Editing a user message creates derived branch and selects it.
- [x] Source branch active activation remains running when branch is created.
- [x] Switching branch changes input target only.
- [x] Sibling output does not appear in selected transcript.
- [x] Shared history before `base_session_item_id` is not duplicated as full transcript rows.

### Task 9: Migrate VSCode ChatView

Purpose: align user-facing interaction with target contracts.

- [x] Update `LabrastroRemoteClient.ts` with `continueSessionRun()` and user-facing `steerAgentRun()`.
- [x] Remove `followUpSessionRun()` and follow-up cancel methods.
- [x] Route first ChatView message through `session-runs/start`.
- [x] Route later idle ChatView input through `session-runs/continue`.
- [x] Route current-run guidance through user-facing AgentRun steer.
- [x] Route ordinary input submitted while selected branch is running into branch-local `pending_next_turn`, then continue after active activation ends.
- [x] Add branch edit flow: edit user message -> create derived branch -> select derived binding -> run from edited point.
- [x] Restrict edit-and-branch to user-authored messages.
- [x] Expose non-user facts only as immutable records or "fork from here" actions that require a new user input after that point.
- [x] Persist derived branch binding/relation and edited message delta before scheduler capacity is checked.
- [x] Add branch navigation state and controls near the affected user message.
- [x] Keep sibling branch indicators separate from selected transcript stream.
- [x] Replace old follow-up event/reducer state with `pending_next_turn`, `steer_pending`, `steer_delivered`, sibling branch summaries.
- [x] Keep `pending_next_turn` branch-local; branch switching does not carry it to another branch, and the user can edit or cancel it before it is sent.
- [x] Convert stale steer result `agent_run_not_steerable` into next-turn routing without surfacing it as a terminal chat error.
- [x] Update `sessionRunEventViews.ts` to stop rendering follow-up events and to render branch alternatives as conversation controls.
- [x] Update VSCode tests.

Implementation note: branch alternatives are rendered by the ChatView branch summary controls (`branchSummaries`, `SessionTurn`, `ChatView` message handling). `sessionRunEventViews.ts` remains the event sanitization layer and must not grow a second branch rendering model.

Acceptance:

- [x] VSCode never calls `/remote/session-runs/follow-up`.
- [x] Idle follow-on message never overloads start.
- [x] Running guidance uses ActivationSteer.
- [x] Ordinary input while running becomes branch-local pending next turn, not a second active activation.
- [x] Stale running guidance becomes next-turn input when the activation has already ended.
- [x] Editing previous user message creates and selects derived branch without cancelling source branch.
- [x] Assistant/tool/runtime/approval/system/developer facts cannot be edited in place.
- [x] Branch navigation never stop/delete/cleanup implicitly.

### Task 10: Projection, Documentation And Final Cleanup

Purpose: remove remaining names and docs that would teach future agents the old split.

- [x] Update backend docs and protocol contract docs with start/continue/steer/branch routing.
- [x] Update frontend docs or comments that mention follow-up.
- [x] Update capability package docs to point to session ingest start only.
- [x] Update release or operator docs to separate admin steer from user-facing steer.
- [x] Remove camelCase examples from docs and fixtures.
- [x] Run final source scans listed below.

Acceptance:

- [x] Documentation describes one execution model: SessionRun projection over AgentRun mainline/activation.
- [x] No doc recommends SessionRun follow-up.
- [x] No doc suggests every user turn creates a new AgentRun mainline.
- [x] No doc suggests branch edits mutate source branch.

## Verification Commands

Run from `D:\AboutDEV\Labrastro\Labrastro` unless a command names another directory.

Backend protocol and route tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/labrastro_server/http/test_protocol.py tests/labrastro_server/http/test_remote_service.py tests/labrastro_server/services/agent_runtime/test_contract_scan.py -q
```

Backend runtime and package tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/labrastro_server/services/agent_runtime/test_control_plane.py tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py tests/labrastro_server/services/test_capability_packages.py tests/domain/agent/test_loop.py -q
```

Go peer tests:

```powershell
Push-Location .\reuleauxcoder-agent
go test ./...
Pop-Location
```

VSCode tests:

```powershell
Push-Location ..\Labrastro-vscode-extension
npm run typecheck
npx vitest run
Pop-Location
```

Source scans:

```powershell
rg -n "session_run_follow_up|queue_follow_up|cancel_follow_up|consume_follow_ups|_inject_pending_follow_ups|/remote/session-runs/follow-up" labrastro_server reuleauxcoder reuleauxcoder-agent ..\Labrastro-vscode-extension\src ..\Labrastro-vscode-extension\webview-ui\src
rg -n "clientRequestId|sessionId" labrastro_server reuleauxcoder reuleauxcoder-agent
rg -n "/remote/admin/capability-packages/ingest/(start|status)" labrastro_server
rg -n "session_run_events_handler|_RemoteSessionRun|_stream_session_run" labrastro_server reuleauxcoder
```

Final hygiene:

```powershell
git diff --check
gitnexus detect-changes -r Labrastro --scope unstaged
```

## Review Checklist

- [x] `SessionRun` no longer owns model/tool execution.
- [x] No `session_run_events_handler`, `_RemoteSessionRun`, or remote relay path drives prompt execution outside AgentRun activation.
- [x] One ChatView branch maps to one long-lived `AgentRun` mainline.
- [x] First input uses start; later idle input uses continue.
- [x] Start is not an implicit continue path.
- [x] Ordinary continue creates a new activation on the same mainline.
- [x] Only branch/fork creates a derived `AgentRun` mainline.
- [x] User-facing steer exists and is separate from admin steer.
- [x] Old follow-up endpoints, events, agent methods and injection hook are gone.
- [x] ActivationSteer delivery is acknowledged through heartbeat.
- [x] Stale steer returns `agent_run_not_steerable` and is routed to next-turn input by UI/controller.
- [x] No fallback from steer to follow-up exists.
- [x] Binding is persisted and used for all ownership checks.
- [x] Missing binding is an explicit error, not inferred from metadata.
- [x] Branch edit creates selected derived branch and does not cancel source branch.
- [x] Only user-authored messages support edit-and-branch; non-user facts remain immutable.
- [x] Branch transcript storage uses structural sharing.
- [x] Sibling branch output does not enter selected transcript.
- [x] Branch switch/hide/stop/delete are separate operations.
- [x] Branch lifecycle backend commands separate selection, hiding, stopping, binding close, and resource cleanup.
- [x] Pending approval/user-input is branch-local.
- [x] `pending_next_turn` is branch-local, does not follow branch switch, and can be edited or cancelled before send.
- [x] Capability package ingest has one public route.
- [x] Package revision/repair reuse package-bound mainline activations.
- [x] Public request payloads are snake_case only.
- [x] Protocol models are canonical and route parsing does not drift.
- [x] Registry, protocol models, contracts fixtures, and parser tests have no gaps inside the runtime convergence surface.
- [x] Tests cover backend, Go peer, VSCode and contract scans.

## Execution Order

1. Add contract guards so old paths are visible failures.
2. Canonicalize protocol models and registry.
3. Implement persisted binding and `session-runs/continue`.
4. Add user-facing AgentRun steer.
5. Complete Go heartbeat steer delivery.
6. Delete SessionRun follow-up chain.
7. Converge capability package ingest/revision interfaces.
8. Implement branch transcript DAG/projection and lifecycle boundaries.
9. Migrate VSCode ChatView input routing and branch UX.
10. Run full verification and update docs.
