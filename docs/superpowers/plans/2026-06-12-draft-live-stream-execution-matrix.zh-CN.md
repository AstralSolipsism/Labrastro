# 草稿实时预览热路径改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本计划在开发期执行，不保留旧协议兼容层，不做迁移桥，不做 MVP，不允许只修局部体验后留下双轨语义。

**Goal:** 系统性重建长 Markdown 草稿的实时展示链路，解决慢、预览停滞、假恢复、600 秒硬切问题，并确保服务端草稿正文始终是唯一真实来源。

**Architecture:** 架构收敛为“一份真实正文，三类消费”：服务端草稿运行时持有唯一正文；实时预览通道只负责当前运行中的低延迟显示；会话记录只负责生命周期、低频进度、快照和审计；最终提交只读取服务端草稿正文。旧的 `document_draft_delta` 高频正文事件语义必须退出热路径，不允许继续把可回放 session event 当正文传输管道。

**Tech Stack:** Python AgentLoop / DocumentDraftRuntime / remote relay / HTTP session run SSE / session projection，VS Code extension TextDocumentContentProvider，Solid webview transcript reducer，pytest，Vitest，GitNexus。

---

## 0A. 2026-06-12 补充合同

本节补充本轮代码审查发现的合同缺口，优先级等同于第 0 节铁律。

| 编号 | 合同 | 验收 |
| --- | --- | --- |
| C1 | preview/progress/snapshot 的时间阈值必须从“出现待发送内容”开始计时，而不是从第一次 flush 后才开始计时 | 低速小 token 未达到字符阈值时，超过 interval 后仍产生对应事件 |
| C2 | 草稿热路径不得在每个 token 上拼接完整正文或重复计算全文 hash | `DocumentDraftRuntime` 增量维护长度/hash；`DocumentDraftLiveStream.append()` 普通判断只读增量状态 |
| C3 | `document_draft_snapshot` 只要带正文，进入 durable buffer / trace persistence / pending trace 前必须先投影成 envelope + artifact，不允许 raw content 旁路进入重路径 | `DraftDocumentProvider` 通过 `wait_events` 看到的 snapshot 仍是单一 `content` 合同，不新增第二套前端正文读取通道 |
| C4 | snapshot artifact envelope 必须保留正文校验和生命周期元数据，但 artifact 引用自身不能携带正文 preview | `content_length`、`content_sha256`、`snapshot_kind`、`final`、`last_chunk_seq`、`draft_id`、`target_path` 不丢；`artifact_ref.preview == ""` |
| C5 | 前端发现 offset gap、sequence gap、hash mismatch 或缺少 content 的 snapshot 时，必须停止盲拼并等待下一次有效 snapshot | 不允许用后续 preview chunk 继续拼接不可信正文 |
| C6 | draft 协议长度单位统一为 UTF-16 code units | Python 使用 `draft_text_units()`；TypeScript 使用 `draftTextUnits()`；不得用 Python `len(str)` 推断协议长度 |
| C7 | ChatView 事件视图不得承载草稿正文 | `document_draft_preview_chunk` 被丢弃；`document_draft_snapshot` 进入 ChatView 前剥离 `content`、`artifact_ref` 和调试字段 |
| C8 | status/list/load 是轻路径，不能 hydrate 大 snapshot artifact | 只有 `wait_events` / 当前 stream 消费者可以 hydrate snapshot 给 `DraftDocumentProvider` |
| C9 | session projection 不能从 snapshot 正文反推 metadata | 缺少显式 `content_length` 时不更新 `contentLength`，不能读取 `content` 兜底 |
| C10 | draft approval/commit 正文所有权归 runtime/backend，不能归 approval metadata | `draft_document_content` 禁止出现；approval payload 只展示 diff/path/title；remote backend 内部携带 expected_state |
| C11 | 旧 `document_draft_delta` 生产事件名禁止回归 | 生产路径 grep 无命中；测试只能用它做“不得出现”的断言 |
| C12 | `_RemoteSessionRun.append_event()` 必须先构建 durable event view，再把同一个 durable view 交给 trace；trace 不得在 artifact 化前拿 raw event | 即时 trace 和 pending trace 的 `document_draft_snapshot` payload 都不含 `content`，也不含正文 preview |
| C13 | `document_draft_preview_chunk` 是实时热路径事件，不得被 artifact 化成无正文 envelope | 即使超过普通 payload 阈值，`wait_events` 给前端的 preview chunk 仍直接包含 `content`，且不进入 `session.events` |

## 0B. 当前收敛后的单一合同

本节是后续执行的最高优先级对照表。若聊天上下文压缩，只看本节也必须能避免跑偏。

| 链路 | 唯一真实来源 | 允许承载正文的位置 | 禁止承载正文的位置 | 校验点 |
| --- | --- | --- | --- | --- |
| 模型输出到草稿 | `DocumentDraftRuntime.active.content` | runtime 内存、`document_draft_preview_chunk.content`、进入 stream view 后的 `document_draft_snapshot.content`、snapshot artifact | `assistant_delta`、ChatView、session projection、status payload、approval metadata/tool args、trace raw payload、durable raw payload | `content_sha256`、UTF-16 `content_length`、`last_chunk_seq` |
| 实时预览 | runtime 正文派生的 preview chunk | `_RemoteSessionRun.append_live_event`、`DraftDocumentProvider` buffer | `_RemoteSessionRun.append_event`、trace persistence、历史投影 | live event 不持久化，offset/seq/hash 不一致时等待 snapshot |
| Durable 账本 | progress/snapshot metadata | `document_draft_progress`、snapshot envelope、trace event metadata、snapshot artifact 引用 | 高频正文 chunk、ChatView 原文、raw snapshot content、raw artifact preview | projection 只读显式 metadata，不读正文兜底 |
| 大正文恢复 | snapshot artifact | `wait_events` 输出给当前 stream / Provider 的 hydrated snapshot | status、list、load、approval status、ChatView event view、trace persistence | status 测试 monkeypatch hydrate 函数必须不被调用 |
| 审批展示 | owner preview diff | approval sections 的 diff 展示 | `draft_document_content`、`content` tool arg、二次 peer preview owner args | remote backend preview state 在内部从 preview 带到 commit |
| 最终落盘 | runtime 持有的正文 + owner expected_state | `RemoteRelayToolBackend.commit_document()` 内部 exec args | 前端预览、session history、approval payload | peer execute 校验 old state / plan hash，create-only |

### 当前必须保留的实现落点

| 合同 | Server 落点 | Extension 落点 | 防漂移测试 |
| --- | --- | --- | --- |
| UTF-16 协议长度 | `reuleauxcoder/domain/agent/document_draft_text.py` | `src/DraftDocumentBuffer.ts::draftTextUnits` | `test_events.py`、`test_document_draft_stream.py`、`DraftDocumentBuffer.test.ts` |
| live-only preview | `remote_relay.py` 中 preview chunk 走 `append_live_event` | `DraftDocumentProvider.applySessionRunEvents` | `test_remote_service.py::test_remote_session_run_document_draft_preview_chunk_is_live_only` |
| durable/trace 事件视图 | `_SessionRunEventBuffer.append()` 返回 durable event view；`_RemoteSessionRun._append_event_locked()` 只持久化 durable view | `src/sessionRunEventViews.ts` 再生成 ChatView view | `test_remote_session_run_persists_document_draft_snapshot_trace_without_body`、`test_remote_session_run_flushes_pending_document_draft_snapshot_trace_without_body` |
| ChatView 脱敏 | `apply_session_event` 不处理 preview chunk 正文 | `src/sessionRunEventViews.ts` | `sessionRunEventViews.test.ts`、`sessionRunTranscriptReducer.test.ts` |
| status 轻路径 | `_RemoteSessionRun.status_payload()` 不调用 `_events_after_locked()` hydrate | `activeRunPayloadWithServerStatus` 不推进 cursor | `test_remote_session_run_status_does_not_hydrate_large_document_draft_snapshot`、`LabrastroController.chat-stream.test.ts` |
| projection 不读正文 | `_apply_document_draft_event()` 只读显式 `content_length` | reducer 已只读显式 length | `test_document_draft_snapshot_without_explicit_length_does_not_infer_from_body` |
| approval 不带全文 | `DocumentDraftRuntime._request_commit_approval()`、`remote_relay._approval_tool_args()` | approval diff tab 只展示 sections | `test_document_draft_runtime_commits_through_workspace_owner`、`test_runner_remote_draft_approval_uses_runtime_diff_without_body_args` |
| expected_state 内部传递 | `RemoteRelayToolBackend._pending_preview_states` | 无前端职责 | `test_remote_backend_carries_document_preview_state_to_commit_without_approval_args` |

### 禁止回归 grep 门禁

```powershell
cd D:\AboutDEV\Labrastro\Labrastro
rg -n "document_draft_delta|DOCUMENT_DRAFT_DELTA|draft_document_content" reuleauxcoder labrastro_server --glob "!tests/**"

cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
rg -n "document_draft_delta|DOCUMENT_DRAFT_DELTA|draft_document_content" src webview-ui\src --glob "!*.test.ts" --glob "!*.spec.ts"
```

两个命令都必须无输出。文档和测试里允许出现旧事件名，只能用于说明历史问题或断言禁止回归。

## 0. 执行铁律

本节是防漂移合同。任何实现、测试、后续会话压缩后的恢复工作都必须先对照本节。

| 编号 | 铁律 | 不合格表现 |
| --- | --- | --- |
| R1 | 开发期不做旧协议兼容，不保留双轨事件语义 | 同时支持旧 `document_draft_delta` 正文流和新预览流 |
| R2 | 不做 MVP、不做局部最小实现，必须一次性打通服务端、传输、前端、测试、文档 | 只在 `DraftDocumentProvider` 节流，但 session event 仍承载正文 |
| R3 | 服务端活动草稿正文是唯一真实来源 | 前端预览、session 历史、提交路径分别拼正文 |
| R4 | 热路径和重路径必须分离 | 每个正文批次都进入 session projection、历史文档、审计回放 |
| R5 | 最终提交只读服务端草稿正文，不读前端预览，也不从 session events 反推 | 前端漏事件导致落盘内容缺失 |
| R6 | 中断、取消、提交、失败前必须先生成一致快照 | 详情显示 7998 字，预览仍停在中间 |
| R7 | 恢复失败必须真实呈现 | `recovery.failed == true` 仍显示“正在尝试恢复” |
| R8 | 600 秒 wall-time 不能掐断仍有内容的活跃输出 | provider 仍持续产出 chunk，但底层监督层按总时长切断 |
| R9 | 任何“暂时保留旧事件”“后续再配置化”“先固定阈值”的实现都不允许合并 | 留下下一轮漂移入口 |

阶段划分只是执行顺序，不是可发布版本。只有全部阶段完成并通过第 10 节验收，才算完成。

---

## 1. GitNexus 分析结论

分析时间：2026-06-12。

本节记录的是制定计划时的影响图谱和问题形态；执行时的当前合同以第 0B 节为准。

索引状态：

| 仓库 | 当前提交 | GitNexus 状态 | 说明 |
| --- | --- | --- | --- |
| `Labrastro` | `cca367f` | up to date | 已全量重建索引；上一次增量中断后自动恢复为干净索引 |
| `Labrastro-vscode-extension` | `247639c` | up to date | 已增量刷新 |

关键查询结果：

| 关注点 | GitNexus 命中落点 | 结论 |
| --- | --- | --- |
| 模型正文到草稿事件 | `AgentLoop.run`、`_on_token`、`DocumentDraftRuntime`、`AgentEvent.document_draft_delta` | 分析时热路径在 `AgentLoop` 内按模型 token 直接生成草稿事件 |
| 草稿事件到会话记录 | `remote_relay._on_agent_event`、`_RemoteSessionRun.append_event`、`apply_session_event` | 分析时 `document_draft_delta` 进入可回放事件缓冲和持久化路径，属于重路径 |
| HTTP SSE 输出 | `_COALESCED_SESSION_RUN_EVENTS`、`_append_live_event_locked`、`wait_events` | 分析时虽然有 40ms 合并，但合并后的事件仍会持久化和进入 session projection |
| VS Code Markdown 预览 | `DraftDocumentProvider`、`LabrastroController.applySessionRunEventsBatch`、`startSessionRun`、`recoverSessionRun` | 预览 provider 是高风险入口，因为它挂在运行、恢复、批量事件入口上 |
| 600 秒中断 | `StreamSupervisor._check_wall_time`、`StreamLivenessLimits`、`LLM._recover_interrupted_response` | wall-time 属于 provider 监督层，当前会切断仍在活跃输出的长生成 |
| ChatView 草稿状态 | `upsertDocumentDraft`、`documentDraftStatus` | ChatView 只需要状态和计数，不应该承接完整高频正文 |

影响半径：

| 符号或文件 | GitNexus 风险 | 执行含义 |
| --- | --- | --- |
| `AgentLoop.run` | MEDIUM | 适合放置草稿流编排，但不要继续把 token 直接转 session event |
| `DocumentDraftRuntime` | LOW | 适合承载唯一正文、快照、长度和 hash |
| `StreamSupervisor` | MEDIUM | 影响 provider adapter、LLM client、model bridge、HTTP agent run，必须配套测试 |
| `DraftDocumentProvider` | HIGH | 直接影响 extension 运行、恢复、批量事件处理，必须单独抽缓冲模型和测试 |
| `apply_session_event` | CRITICAL | 不允许大面积重写；只处理低频状态和快照元数据 |
| `upsertDocumentDraft` | LOW | 只保留状态、计数、生命周期更新 |
| `LLM._recover_interrupted_response` | LOW | 恢复结果状态可以小范围修正 |

设计结论：

```text
session event 是账本，不是正文传输管道。
Markdown preview 是显示副本，不是真实正文来源。
DocumentDraftRuntime 是草稿正文唯一真实来源。
StreamSupervisor 只判断流是否活着，不决定长文档是否应该继续写。
```

---

## 2. 改造前问题实现与目标实现对比

### 改造前问题实现

```text
模型输出一个 token
  -> AgentLoop 追加到草稿
  -> AgentLoop 立即发 document_draft_delta
  -> remote relay 立即 append_event
  -> _RemoteSessionRun 合并后仍写入可回放事件缓冲
  -> trace persistence / session projection / ChatView reducer
  -> DraftDocumentProvider 追加正文
  -> VS Code Markdown 立即刷新或高频刷新
```

问题：

| 问题 | 具体表现 | 根因 |
| --- | --- | --- |
| 慢 | 7998 字生成 3359 条草稿正文事件 | 模型 token 被当成 UI 和审计事件粒度 |
| 预览停滞 | 详情显示已生成 7998 字，Markdown 预览停在表格中间 | 预览依赖实时 delta 拼接，缺少最终快照校准 |
| 假恢复 | UI 显示“正在尝试恢复”，实际恢复已失败 | 恢复事件没有区分 attempted 和 failed |
| 硬中断 | 600 秒后仍有内容输出也被掐断 | wall-time 无视活跃输出，只看总时长 |
| 职责混乱 | 正文、计数、审计、预览刷新混在一条链路 | 热路径和重路径未分层 |

### 目标实现（当前必须收敛到的实现）

```text
模型输出
  -> DocumentDraftRuntime 追加正文，形成唯一真实正文
  -> DocumentDraftLiveStream 按明确策略产出实时预览批次
  -> _RemoteSessionRun.append_live_event 只推当前 SSE，不持久化、不投影
  -> DraftDocumentProvider 本地显示缓冲区接收批次，节流刷新 Markdown

同时：
DocumentDraftRuntime
  -> document_draft_progress 低频写 session event
  -> document_draft_snapshot 定期和终止边界写 session event
  -> draft_document_commit 从同一份正文创建文件
```

这不是双通道业务逻辑，而是同一份正文的三种消费：

| 消费 | 事件 | 是否持久化 | 是否包含正文 | 用途 |
| --- | --- | --- | --- | --- |
| 实时预览 | `document_draft_preview_chunk` | 否 | 是，批量正文 | 当前运行中的 VS Code Markdown 预览 |
| 会话状态 | `document_draft_progress` | 是 | 否 | ChatView 状态、计数、历史回放 |
| 快照校准 | `document_draft_snapshot` | 是 | durable/trace 只存 envelope + artifact；当前 stream view hydrate 后含 `content` | 中断/恢复/历史预览/最终一致性 |

---

## 3. 新事件合同

本节是实现合同，不是建议。旧的 `document_draft_delta` 高频正文语义必须被删除或改名退出热路径；测试中不得继续依赖它传正文。

### 3.1 Agent 内部事件

| 事件 | 内容 | 触发时机 | 规则 |
| --- | --- | --- | --- |
| `document_draft_started` | `draft_id`、`target_path`、`title`、`format`、`status` | 草稿开始 | 无正文 |
| `document_draft_preview_chunk` | `draft_id`、`target_path`、`chunk_seq`、`start_offset`、`end_offset`、`content`、`content_sha256` | 实时预览批次 flush | live-only，不进入 session projection，不持久化 |
| `document_draft_progress` | `draft_id`、`target_path`、`content_length`、`content_sha256`、`last_chunk_seq`、`status` | 每 2048 字或每 1 秒，二者先到者；终止前强制 | durable，无正文 |
| `document_draft_snapshot` | raw agent event 可带 `content`；进入 durable/trace 后只保留 envelope + artifact；当前 stream view hydrate 后恢复 `content` | 每 16384 字或每 5 秒；中断、取消、提交请求、完成前强制 | durable/trace 不存 raw content；stream 消费者保持单一 `content` 合同 |
| `document_draft_commit_requested` | `draft_id`、`target_path`、`snapshot_hash`、`content_length`、`approval_id`、`item_id` | 提交审批前 | 无正文，绑定最近 final snapshot |
| `document_draft_committed` | `draft_id`、`target_path`、`snapshot_hash`、`content_length`、`item_id` | 提交成功 | 无正文 |
| `document_draft_failed` | `draft_id`、`target_path`、`snapshot_hash`、`content_length`、`error` | 失败 | 无正文 |
| `document_draft_cancelled` | `draft_id`、`target_path`、`snapshot_hash`、`content_length`、`reason` | 取消或 provider 中断后取消活动草稿 | 无正文，必须在 final snapshot 之后 |

### 3.2 HTTP / SSE 传输合同

| 方法 | 用途 | 允许事件 | 持久化 |
| --- | --- | --- | --- |
| `_RemoteSessionRun.append_event` | 会话账本 | 生命周期、状态、进度、快照、审批、错误 | 是 |
| `_RemoteSessionRun.append_live_event` | 当前运行实时预览 | `document_draft_preview_chunk` | 否 |

必须新增 `append_live_event` 或等价显式 API。禁止继续把草稿正文通过 `append_event("document_draft_delta", ...)` 发送。

`wait_events` 可以继续通过现有 SSE 返回 live event 和 durable event，但 live event 不得调用 `_persist_or_queue_trace_event`，不得进入 `apply_session_event`，不得被历史会话回放依赖。

`append_event` 的执行顺序必须固定为：先把 raw event 投影成 durable event view，再把 durable view 放入 buffer 和 trace。trace persistence、pending trace、session projection、status/list/load 都不得直接接触带正文的 raw snapshot。`document_draft_preview_chunk` 则必须保持 live-only 原文事件，即使超过普通 payload 阈值也不能 artifact 化成无 `content` envelope。

### 3.3 前端消费合同

| 消费者 | 处理事件 | 禁止行为 |
| --- | --- | --- |
| `DraftDocumentProvider` | `document_draft_started`、`document_draft_preview_chunk`、`document_draft_snapshot`、终态事件 | 从 ChatView 文本、session projection 或审批 diff 反推正文 |
| `sessionRunTranscriptReducer` | `document_draft_started`、`document_draft_progress`、`document_draft_snapshot` 元数据、终态事件 | 把正文批次变成 assistant message |
| `ChatView` | 中断/恢复状态、草稿状态卡 | 展示长正文原文或 raw JSON |

---

## 4. 统一性与漂移防线

统一后的数据关系：

```text
DocumentDraftRuntime.active.content
  -> document_draft_preview_chunk：当前运行预览
  -> document_draft_progress：状态计数
  -> document_draft_snapshot：校准和历史
  -> draft_document_commit：最终落盘
```

不允许出现：

```text
服务端拼一份正文
前端拼一份正文
session event 再拼一份正文
提交时从历史事件反推正文
```

漂移防线：

| 防线 | 执行要求 | 验收 |
| --- | --- | --- |
| 顺序 | 每个 preview chunk 必须带 `chunk_seq`、`start_offset`、`end_offset` | 前端发现 offset 缺口时停止盲拼并等待 snapshot |
| 内容校验 | chunk 和 snapshot 必须带 sha256 | 重复、乱序、缺失可以被检测 |
| 快照 | 中断、取消、提交请求、完成前必须 final snapshot | 详情计数和预览内容最终一致 |
| 单源 | 提交只读 `DocumentDraftRuntime.active.content` 或其不可变 final snapshot | 前端漏事件不影响落盘内容 |
| 幂等 | 前端按 `draft_id + chunk_seq` 去重，snapshot 按 hash 覆盖 | 重连/重复事件不重复正文 |
| 账本隔离 | `apply_session_event` 只处理 progress/snapshot 元数据，不处理 preview chunk 正文 | 历史记录不会因正文流过大拖慢 |

---

## 5. 文件落点矩阵

### Server 仓库：`Labrastro`

| 文件 | 当前职责 | 必须修改成 | 风险 |
| --- | --- | --- | --- |
| `reuleauxcoder/domain/agent/document_draft.py` | 管理活动草稿、提交、取消 | 增加 `snapshot()`、`content_length`、`content_sha256`、final snapshot 绑定；提交读取同源正文 | LOW |
| `reuleauxcoder/domain/agent/document_draft_stream.py` | 不存在 | 新建草稿实时流合并器，生成 chunk/progress/snapshot 决策，不负责持久化 | NEW |
| `reuleauxcoder/domain/agent/loop.py` | LLM loop、token callback、草稿事件 | `_on_token` 只追加唯一正文并调用流合并器；终止边界强制 snapshot/flush | MEDIUM |
| `reuleauxcoder/domain/agent/events.py` | Agent event 类型和 payload 工厂 | 明确定义 `document_draft_preview_chunk`、`document_draft_progress`、`document_draft_snapshot`；删除旧高频正文语义 | MEDIUM |
| `reuleauxcoder/interfaces/entrypoint/remote_relay.py` | Agent event 到 remote session | preview chunk 走 `append_live_event`；progress/snapshot 走 `append_event` | MEDIUM |
| `labrastro_server/interfaces/http/remote/service.py` | `_RemoteSessionRun`、SSE 缓冲、事件持久化 | 增加 live-only 事件 API；从 coalesced durable set 移除草稿正文事件 | MEDIUM |
| `labrastro_server/interfaces/http/remote/routes/chat.py` | session run SSE 输出 | 保证 live event 可通过当前 SSE 到达，但不进入历史账本 | MEDIUM |
| `reuleauxcoder/domain/session/document.py` | 会话历史投影 | 只处理 progress/snapshot 元数据和终态；不得处理 preview chunk 正文 | CRITICAL |
| `reuleauxcoder/domain/session/locale.py` | 中断/恢复提示文案 key | 增加恢复失败、已保留部分输出、可继续生成文案 | LOW |
| `reuleauxcoder/services/providers/stream_supervisor.py` | provider 流监督和超时 | wall-time 改成活动感知和配置驱动；idle timeout 继续负责卡死检测 | MEDIUM |
| `reuleauxcoder/domain/config/models.py` | Provider config schema | 增加 `stream_liveness` 一等配置，不放进 `extra` |
| `reuleauxcoder/services/config/loader.py` | 配置加载 | 接受并校验 `stream_liveness` |
| `reuleauxcoder/services/llm/client.py` | 中断恢复、诊断、继续生成 | 恢复失败状态准确传出；恢复请求使用合理 continuation budget | LOW |

### Extension 仓库：`Labrastro-vscode-extension`

| 文件 | 当前职责 | 必须修改成 | 风险 |
| --- | --- | --- | --- |
| `src/DraftDocumentProvider.ts` | 虚拟 Markdown 文档和预览打开 | 只负责 VS Code provider 外壳，不直接承担复杂顺序/快照逻辑 | HIGH |
| `src/DraftDocumentBuffer.ts` | 不存在 | 新建显示缓冲模型：offset 校验、chunk 去重、snapshot 覆盖、刷新调度 | NEW |
| `src/LabrastroController.ts` | session run 事件接收和分发 | live preview event 交给 DraftDocumentProvider；ChatView 只拿 durable 状态事件 | HIGH |
| `webview-ui/src/chat/sessionRunTranscriptReducer.ts` | ChatView 事件归约 | 只保留草稿状态、计数、snapshot 元数据；不消费 preview chunk 正文 | LOW |
| `webview-ui/src/components/ChatView.tsx` | 运行状态和中断提示 | 修正恢复失败文案；草稿卡只展示状态和打开预览入口 | MEDIUM |
| `webview-ui/src/i18n/zh-CN.ts` | 中文文案 | 增加恢复失败、已保留部分输出文案 | LOW |
| `webview-ui/src/i18n/en.ts` | 英文文案 | 同步英文文案 | LOW |

---

## 6. 分阶段执行矩阵

### 阶段 0：执行前工作区处理

| 步骤 | 操作 | 验收 |
| --- | --- | --- |
| 0.1 | 确认两个仓库 dirty 状态 | 明确哪些是本计划修改，哪些是历史残留 |
| 0.2 | 处理上一轮误提前写入的测试草稿 | 要么纳入本计划并按新事件合同改写，要么在用户明确同意后回滚 |
| 0.3 | 运行 `gitnexus status` | 两个仓库均 up to date |
| 0.4 | 更新本文档后再开始代码 | 后续 agent 只按本文档执行，不按聊天记忆补全设计 |

### 阶段 1：定义并测试新事件合同

| 步骤 | 修改点 | 验收 |
| --- | --- | --- |
| 1.1 | 在 `events.py` 定义新事件工厂和 payload 字段 | 测试能直接构造 preview/progress/snapshot |
| 1.2 | 删除或改写旧 `document_draft_delta` 正文测试 | 测试中不再依赖旧高频正文事件 |
| 1.3 | 增加 session projection 测试 | `document_draft_preview_chunk` 不进入历史投影 |
| 1.4 | 增加 HTTP session event 测试 | live event 不持久化，durable event 持久化 |

### 阶段 2：服务端草稿正文单源和流合并器

| 步骤 | 修改点 | 验收 |
| --- | --- | --- |
| 2.1 | `DocumentDraftRuntime` 增加 snapshot API | snapshot 内容、长度、hash 与活动草稿一致 |
| 2.2 | 新建 `document_draft_stream.py` | 合并器输出 preview/progress/snapshot 三类事件决策 |
| 2.3 | `AgentLoop._on_token` 改为只追加正文并驱动合并器 | 8000 字不会生成接近 token 数的 preview chunk |
| 2.4 | 所有终止边界强制 final snapshot | commit/cancel/interrupted/budget/max turns 前均有 final snapshot |

默认策略必须集中定义为命名常量或配置，不得散落硬编码：

| 策略 | 默认值 | 说明 |
| --- | --- | --- |
| preview flush 间隔 | 100ms | 实时显示，不进账本 |
| preview flush 字符 | 2048 chars | 限制 event 数 |
| progress 间隔 | 1s 或 2048 chars | 低频状态 |
| snapshot 间隔 | 5s 或 16384 chars | 预览校准和历史恢复 |

### 阶段 3：remote session 热路径和重路径分离

| 步骤 | 修改点 | 验收 |
| --- | --- | --- |
| 3.1 | `_RemoteSessionRun` 新增 `append_live_event` | live event 进入当前 SSE 缓冲，但不持久化 |
| 3.2 | `append_event` 保持 durable 语义 | 生命周期、progress、snapshot、审批、错误继续持久化 |
| 3.3 | `_COALESCED_SESSION_RUN_EVENTS` 不再包含草稿正文事件 | 不再用 coalesced durable event 承载正文 |
| 3.4 | `remote_relay` 按事件类型分流 | preview chunk 走 live；progress/snapshot 走 durable |
| 3.5 | `events_lost` / offset gap 有明确降级 | 前端等待下一次 snapshot 校准，不盲拼 |

### 阶段 4：前端预览缓冲模型

| 步骤 | 修改点 | 验收 |
| --- | --- | --- |
| 4.1 | 新建 `DraftDocumentBuffer.ts` | 单测覆盖顺序、重复、缺口、snapshot 覆盖 |
| 4.2 | `DraftDocumentProvider` 使用 buffer | provider 只负责打开预览和节流 fire |
| 4.3 | `LabrastroController` 分流 live/durable 事件 | preview chunk 不进 ChatView reducer |
| 4.4 | 中断/完成 snapshot 校准预览 | 详情计数和预览最终一致 |

### 阶段 5：ChatView 状态和恢复提示

| 步骤 | 修改点 | 验收 |
| --- | --- | --- |
| 5.1 | reducer 只处理 durable 草稿状态 | ChatView 不显示长正文，不生成 assistant message |
| 5.2 | `recovery.failed` 真实映射 | 不显示“正在尝试恢复” |
| 5.3 | 中断状态提示 | 显示“输出中断，已保留部分内容，可继续生成” |
| 5.4 | 草稿卡打开预览 | 用户能从紧凑状态卡重新打开 VS Code Markdown 预览 |

### 阶段 6：Provider 流监督策略

| 步骤 | 修改点 | 验收 |
| --- | --- | --- |
| 6.1 | `ProviderConfig` 增加 `stream_liveness` 一等配置 | loader 和 validator 接受并校验，不放 `extra` |
| 6.2 | `StreamLivenessLimits` 从 config 构造 | 不再只有硬编码 600/120 |
| 6.3 | wall-time 改成活动感知 | 持续输出时不因固定总时长切断 |
| 6.4 | idle timeout 保留 | 无输出卡死仍中断 |
| 6.5 | run budget 负责总成本/总时长 | provider supervisor 不越权决定长文档业务预算 |

### 阶段 7：端到端验收和 GitNexus 复查

| 步骤 | 操作 | 验收 |
| --- | --- | --- |
| 7.1 | 跑 server focused pytest | 全部通过 |
| 7.2 | 跑 extension typecheck 和 Vitest | 全部通过 |
| 7.3 | 跑 `git diff --check` | 无 whitespace error |
| 7.4 | 刷新 GitNexus 索引 | 两仓库 up to date |
| 7.5 | 跑 `gitnexus detect-changes` | 风险集中在本文档列出的文件 |
| 7.6 | 对照第 0 节铁律逐条检查 | 任一不满足不得交付 |

---

## 7. 测试矩阵

### Server 测试

| 测试文件 | 必须覆盖 |
| --- | --- |
| `tests/domain/agent/test_events.py` | preview chunk / snapshot 使用 UTF-16 协议长度；progress 不带正文 |
| `tests/domain/agent/test_loop.py` | 草稿 token 不再直接产生旧 `document_draft_delta`；终止前 final snapshot；恢复失败文案 |
| `tests/domain/agent/test_document_draft.py` | snapshot 与活动草稿同源；提交读取同源正文；approval metadata 不带 `draft_document_content` |
| `tests/domain/agent/test_document_draft_stream.py` | 合并器 preview/progress/snapshot 阈值、强制 flush、hash、offset |
| `tests/domain/session/test_document.py` | preview chunk 不进入 projection；progress/snapshot 元数据更新草稿卡；projection 不从正文推断长度 |
| `tests/labrastro_server/http/test_remote_service.py` | live event 不持久化；durable snapshot 可回放；status 不 hydrate；SSE 顺序正确 |
| `tests/extensions/tools/test_remote_backend_dispatch.py` | draft preview expected_state 由 backend 内部带到 commit；空 expected_state 不发送 |
| `tests/interfaces/entrypoint/test_runner_remote_exec.py` | draft approval 使用 runtime diff，不二次 peer preview，不携带正文参数 |
| `tests/services/test_stream_liveness.py` | 活跃输出超过 wall-time 不切；idle 超时仍切 |
| `tests/services/test_llm_client.py` | 恢复失败 payload 不再被当作 recovering |
| `tests/services/config/test_loader.py` | `stream_liveness` 配置加载、未知字段仍拒绝 |

命令：

```powershell
cd D:\AboutDEV\Labrastro\Labrastro
.\.venv\Scripts\python.exe -m pytest -q `
  tests\domain\agent\test_events.py `
  tests\domain\agent\test_loop.py `
  tests\domain\agent\test_document_draft.py `
  tests\domain\agent\test_document_draft_stream.py `
  tests\domain\session\test_document.py `
  tests\labrastro_server\http\test_remote_service.py `
  tests\extensions\tools\test_remote_backend_dispatch.py `
  tests\interfaces\entrypoint\test_runner_remote_exec.py `
  tests\services\test_stream_liveness.py `
  tests\services\test_llm_client.py `
  tests\services\config\test_loader.py `
  --basetemp .pytest-tmp-draft-stream `
  -o cache_dir=.pytest-cache-draft-stream
```

### Extension 测试

| 测试文件 | 必须覆盖 |
| --- | --- |
| `src/DraftDocumentBuffer.test.ts` | chunk 顺序、重复、缺口、snapshot 覆盖、hash 校验 |
| `src/DraftDocumentProvider.test.ts` | 节流刷新、打开预览、终态后保留预览 |
| `src/sessionRunEventViews.test.ts` | ChatView 事件视图丢弃 preview chunk，剥离 snapshot `content` 和 `artifact_ref` |
| `src/LabrastroController.chat-stream.test.ts` | live preview event 只进 provider，durable event 进 ChatView 前先脱敏 |
| `webview-ui/src/chat/sessionRunTranscriptReducer.test.ts` | 草稿状态/计数/snapshot 元数据；不生成正文消息 |
| `webview-ui/src/components/ChatView.context-events.test.ts` | 恢复失败和可继续生成提示 |

命令：

```powershell
cd D:\AboutDEV\Labrastro\Labrastro-vscode-extension
npm run typecheck:extension
npm run typecheck:webview
npx vitest run `
  src/DraftDocumentBuffer.test.ts `
  src/DraftDocumentProvider.test.ts `
  src/sessionRunEventViews.test.ts `
  src/LabrastroController.chat-stream.test.ts `
  webview-ui/src/chat/sessionRunTranscriptReducer.test.ts `
  webview-ui/src/components/ChatView.context-events.test.ts
```

---

## 8. 用户体验验收

| 场景 | 当前问题 | 改造后验收 |
| --- | --- | --- |
| 生成 8000 字 Markdown | 每 2-3 字刷新一次，肉眼很慢 | preview chunk 数量显著降低，预览按批次平滑更新 |
| 生成复杂表格 | 预览停在表格中间 | final snapshot 校准后表格完整 |
| 超过 600 秒仍在输出 | 被硬切 | 只要持续有内容，不因固定 wall-time 切断 |
| provider 卡死无输出 | 可能挂住 | idle timeout 中断并保留 partial snapshot |
| 恢复失败 | 显示“正在尝试恢复” | 显示“输出中断，已保留部分内容，可继续生成” |
| ChatView | 长正文挤进过程卡片或调试信息 | 只显示紧凑草稿状态、计数、打开预览入口 |
| VS Code 预览 | 自动打开后卡住或过刷 | 自动打开、节流刷新、snapshot 补齐 |

---

## 9. 非目标

本计划不处理：

| 非目标 | 原因 |
| --- | --- |
| `apply_patch` 审批合同重构 | 与当前草稿实时预览热路径不是同一问题 |
| `draft_document_commit` 产品定位再争论 | ADR 0007 已暂定 create-only；本计划只保证草稿正文流和提交同源 |
| ChatView 全量视觉重构 | 需要单独 UX 计划 |
| 旧会话历史迁移 | 开发期不做迁移；新协议从当前开发态直接替换 |
| 旧事件兼容 adapter | 禁止双轨，避免模型和前端继续漂移 |

---

## 10. 完成定义

必须同时满足：

| 类别 | 完成标准 |
| --- | --- |
| 架构 | `DocumentDraftRuntime` 是草稿正文唯一真实来源 |
| 协议 | `document_draft_delta` 不再承担正文传输；新 preview/progress/snapshot 合同落地 |
| 热路径 | preview chunk live-only，不持久化，不投影 |
| 重路径 | session event 只记录生命周期、progress、snapshot、审批、错误 |
| 长度 | offset/content_length 使用 UTF-16 code units，projection 不从正文推断 |
| 轻路径 | status/list/load/ChatView/approval status 不 hydrate snapshot artifact |
| 审批 | draft approval payload 和 metadata 不携带完整正文，remote expected_state 由 backend 内部传递 |
| 性能 | 长文档正文事件数量按批次下降，不再接近 token 数 |
| 预览 | VS Code Markdown 预览可持续更新，并能被 snapshot 补齐 |
| 中断 | 活跃输出不被固定 600 秒硬切；idle 卡死仍被切断 |
| 真实提示 | 恢复失败不显示“正在恢复” |
| 提交 | 最终提交只读服务端草稿正文或其 final snapshot |
| 测试 | server pytest、extension typecheck、Vitest 通过 |
| 图谱 | GitNexus detect-changes 风险集中在计划列出的文件 |
| 防漂移 | 第 0 节 R1-R9 全部满足 |
