# Labrastro 后端开发宪法

本文件是 Codex 处理 `AstralSolipsism/Labrastro` 后端仓库时的最高开发约束。
它不是模块百科；需要架构地图时阅读 `docs/agent-context/backend-runtime-map.md`。

## 1. 本仓库负责什么

本仓库负责 Labrastro 的后端、运行时和执行系统。

- `reuleauxcoder/`：本地执行器、Agent 循环、工具、权限、会话、MCP、Skill、Hook。
- `labrastro_server/`：服务端控制面、HTTP API、SessionRun、AgentRun、Taskflow、能力包、审计、持久化。
- `reuleauxcoder-agent/`：远程 peer 执行二进制。
- `docker/`：后端服务部署与运行环境。

本仓库决定后端事实。前端只能展示、提交请求、返回用户操作，不能自己发明后端事实。

## 2. 事实源边界

修改任何功能前，先判断这件事属于哪个领域、谁拥有可写事实源、谁只是派生视图。

| 领域 | 可写事实源 | 派生视图 / 投影 | 前端职责 |
| --- | --- | --- | --- |
| 交互聊天 | `SessionRun` | canonical transcript / ChatView transcript | 渲染 transcript，提交用户输入和审批回复 |
| 后台 Agent 执行 | `AgentRun` | AgentRun detail、events、artifacts view | 展示状态、事件、产物和终态 |
| Taskflow 计划 | `TaskflowState` / `ProjectState` | Taskflow runtime projection、workspace projection | 展示计划、TaskRun、liveness、追踪关系 |
| 能力包 | 后端 capability package 服务、validator、runtime resolver | review / install projection | 展示评审结果，提交用户决策 |
| 权限审批 | 权限网关、approval events | approval view | 展示审批请求，提交 approve / deny |
| Lifecycle hook | runtime adapter registry、dispatcher、audit events | lifecycle event projection | 展示可见事件、诊断和终态 |
| 服务端配置 | 后端 config loader / store | settings API response | 展示配置，提交保存请求 |
| 远程执行 | relay protocol、peer registration、AgentRun control plane | remote status / feature view | 展示连接状态和远程能力 |

规则：

- 可写事实源只能由所属后端模块修改。
- 派生视图只能从事实源计算，不能反向伪造事实。
- 前端只能展示、提交请求和提交用户决策，不能成为业务事实源。
- 如果视图与事实源冲突，修事实源或 projection，不在 UI 里硬修显示。
- 如果一次修改跨多个事实源，必须说明每个事实源的写入路径和验证方式。

## 3. 禁止用表层方案掩盖底层问题

当问题来自事实源、协议、权限、持久化、运行时合同或审计链路时，必须修对应的底层合同。
不得只在 UI、adapter、兼容层、默认值或日志里掩盖症状。

判断方法：

- 如果数据应该由后端产生，就不能由前端补造。
- 如果状态应该持久化，就不能只存在内存或 UI state。
- 如果权限应该由权限网关裁决，就不能由 hook、UI 或工具自行放行。
- 如果能力应该有 runtime adapter，就不能只加 schema、配置和展示。
- 如果协议语义变化，就不能只改调用方的 optional fallback。
- 如果完成状态需要证据，就不能只靠日志文案或本地变量判断。

非穷尽示例：

- 后端没有写 `SessionRun` event，前端自己拼一个成功消息。
- `AgentRun` 状态缺失，Taskflow projection 或前端自己猜 running / completed。
- 能力包 validator 没覆盖字段，Settings 里用默认值假装合法。
- lifecycle handler 只有 schema 和 UI 展示，没有 runtime adapter，却标记为 executable。
- remote protocol 字段不稳定，前端用 optional fallback 吞掉错误并继续显示成功。
- 服务端保存失败，Settings 只显示本地 optimistic success。

## 4. 后端修改协议

实现前必须先回答：

- 这次修改影响哪个事实源？
- 是否跨 HTTP protocol、remote protocol、SessionRun event 或 AgentRun event？
- 是否影响持久化、审计、权限或运行时合同？
- 是否影响前端 client、reducer、renderer 或设置页？
- 是否需要迁移旧数据或兼容旧事件？

实现时必须优先修正 domain/runtime/server 合同，再处理展示层。
兼容层必须薄，且有明确删除条件。

## 5. 跨前后端协议规则

改 HTTP API、remote protocol、SessionRun event、AgentRun event 时，必须同步检查：

- 后端 route / service
- protocol 类型
- 持久化或投影逻辑
- 前端 client
- 前端 reducer / renderer
- 后端测试
- 前端协议测试

只改一边不算完成。

## 6. 验收要求

没有证据不能声称完成。
没有运行相关验证，只能说明已修改，不能说明行为已验证。
部署后必须证明运行 revision、构建标识和健康状态。
不得把无关 dirty 文件混入交付范围。

交付说明必须包含：

- 变更范围
- 影响边界
- 验证命令和结果
- 未验证项及原因
- 如已部署，给出运行态证据

## 7. 常用验证

后端 Python 测试：

```bash
uv run python -m pytest tests/ -v
```

Go peer 测试：

```bash
cd reuleauxcoder-agent
go test ./...
```

部署验证至少包含：

- 构建 revision
- 容器运行状态
- 容器内 `/app/BUILD_REVISION`
- 健康接口返回

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **Labrastro** (16040 symbols, 35984 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/Labrastro/context` | Codebase overview, check index freshness |
| `gitnexus://repo/Labrastro/clusters` | All functional areas |
| `gitnexus://repo/Labrastro/processes` | All execution flows |
| `gitnexus://repo/Labrastro/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
