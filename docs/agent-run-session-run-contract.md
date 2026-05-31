# AgentRun 与 SessionRun 架构契约

## 职责边界

SessionRun 是用户观察、交互和历史恢复的唯一承载。前端只消费 canonical SessionRun transcript，再通过统一 presentation 渲染。

AgentRun 是后台长期任务生命周期。它可以排队、claim、heartbeat、cancel、complete，也可以绑定 sandbox 和 executor session，但它的原始事件、stdout、TUI 文本不能直接进入用户正文。

ExecutorSession 保存具体 agent 的长期上下文。AgentRun 的 prompt 是推进这个 executor session 的一次输入，不代表一次性会话。

ModelRequest 是 AgentRun 到服务端模型能力的唯一边界。sandbox worker 不能携带服务商密钥，也不能绕过服务端模型配置直接请求外部模型。

## 可见投影

所有 AgentRun executor event 必须先归一为结构化事件，再通过 `labrastro_server/services/agent_runtime/session_projection.py` 投影成 canonical SessionRun event。

AgentRun raw events 是审计层。它可以保存逐 token、完整工具输出和诊断字段，但这些 raw facts 不能等同于用户主时间线。

SessionRun semantic events 是用户可见过程层。连续 `text/thinking/log/status` 必须先按类型和时间窗口聚合，工具、错误、审批和终态必须优先可见，不能被历史 token backlog 阻塞。

固定映射：

- `text` -> 聚合后的 `assistant_delta`
- `thinking` -> 聚合后的 `reasoning_delta`
- `tool_use/tool_result` -> `tool_call_start/tool_call_end`
- `log/status/session_pinned` -> `context_event`
- `error/result/terminal` -> 结构化错误或终态过程事件

语义事件应保留 `raw_event_refs` 或等价诊断引用，供详情/审计入口追溯原始 AgentRun 事实。

非 JSON stdout、banner、诊断输出只能进入 `log`，不能进入 assistant 正文。能力包、未来 agent/taskflow 都复用同一个 projector，不新增专用 UI 或旧轮询草案路径。

## 能力包草案

能力包草案是结构化产物，不是 assistant markdown 正文。

`capability_packager` 的模型输出只表达包结构决策：包 id/name/description、skills、依赖、使用方式、安装计划和证据引用。模型不得通过 fenced JSON 搬运完整 `skill_content` 或大文件正文。

完整 Skill 内容由服务端基于 source bundle、明确的 `workspace_root + source_path`，或后续 artifact/worktree 通道组装成 canonical draft。校验和安装只读取 canonical draft，不从 ChatView assistant 文本反解析草案。

SessionRun 通过 `capability_package_draft` event 展示草案卡片。卡片展示摘要、组件、校验状态和安装确认；完整内容和诊断信息进入 artifact/raw audit/detail，不占用主时间线。

## 模型流协议

`/remote/agent-runs/model-request` 的流式响应是有限 SSE 协议，只允许：

- `heartbeat`
- `token`
- `reasoning_token`
- `tool_call_delta`
- `done`
- `error`
- `interrupted`

每个流式请求必须以 `done/error/interrupted` 之一结束。空闲期间发送 `heartbeat`，避免 sandbox 端因为首 token 慢或中途静默读超时。

transport/chunk/proxy/peer close 等底层问题归类为 `interrupted`，并携带 partial response 与诊断字段。用户可见 transcript 只展示业务语义和可理解的恢复/失败信息，不展示 raw chunk、traceback、stderr。

流式和非流式 model-request 必须使用同一套终态语义。非流式请求遇到 provider stream interruption 时返回完整 JSON body：`ok=false`、`error=provider_stream_interrupted`、`stream_status=interrupted`、partial `response`、`interruption` 和 `diagnostic_*`，不能让 HTTP 连接异常断开。

## 语言契约

语言来源固定为：

`前端 locale -> SessionRun.locale -> AgentRun.metadata.locale -> ModelRequest system instruction`

`zh-CN` 时，语言 system instruction 本身使用中文，并要求用户可见自然语言、过程叙述、思考摘要、草案自然语言字段和最终说明使用简体中文。

JSON key、id、命令、路径、URL、代码、API 名称、引用的原始错误保持原文。

Agent/Provider/ModelBridge 层只产出 `message_key`、`notice_code`、`diagnostic_*` 等结构化事实，不能写用户语言文案。SessionRun 边界按 `SessionRun.locale` 解析 `message_key` 并把稳定的 `message` 写入 canonical event，历史回放不受当前前端语言切换影响。

## 生命周期

SessionRun cancel 必须停止关联 AgentRun、sandbox/model-request 和投影循环，并只写一个用户可见终态。

SessionRun status/recover 必须能根据 metadata 重新绑定运行中的 AgentRun，使窗口重开后继续观察同一个任务，不重复创建任务。

AgentRun live event 成功写入后，complete 不能重放；live 发送失败的事件由 complete 补交。

AgentRun 不允许存在隐藏业务 wall-clock deadline。timeout 必须来自 runtime profile 或 AgentRun metadata；缺省 timeout 表示没有业务 deadline。短任务 profile 应显式配置 step timeout，长周期 TaskFlow 依赖 heartbeat、checkpoint、lease 和 deadline policy 管理生命周期。

## 修改要求

修改 AgentRun、能力包、taskflow 或 server worker 相关代码时，必须验证：

- 没有 workflow 绕过 SessionRun projector。
- 没有 stdout/TUI/banner 进入 assistant 正文。
- 没有完整能力包 JSON 或 `skill_content` 逐 token 进入 assistant 正文。
- 能力包草案通过 `capability_package_draft` 结构化事件展示。
- model-request 有 heartbeat 和明确终态。
- AgentRun terminal/error/cancel/deadline 不被 token backlog 延迟。
- AgentRun timeout 来源可解释、可配置，没有隐藏默认业务上限。
- 断流进入 interruption/recovery 语义。
- locale 进入模型边界，而不只是写 metadata。
- 用户可见提示由 SessionRun 边界本地化，raw exception 只进入诊断字段。
- ChatView 仍只渲染 canonical SessionRun transcript。
