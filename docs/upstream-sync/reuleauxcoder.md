# ReuleauxCoder Upstream Sync Ledger

This ledger tracks how EZCode/Labrastro absorbs upstream changes from
`RC-CHN/ReuleauxCoder`. It is the maintained source of truth when Git ancestry
or patch-id cannot represent a local rewrite.

## Current Snapshot

- Upstream repo: `RC-CHN/ReuleauxCoder`
- Upstream branch: `main`
- Last reviewed upstream commit: `b570877f7634bcb70add41ac925282792a91e11d`
- Local branch: `main`
- Local head after current absorption batch: `45bcbcf6bc28b8d0f21f819b140bb6d643abab4d`
- Review date: 2026-05-16

## Status Rules

- `merged`: upstream change arrived through a merge or patch-equivalent commit.
- `ported`: behavior was manually ported into local commits.
- `superseded`: behavior was absorbed and then replaced by local architecture.
- `partial`: local code covers only part of the upstream change.
- `pending`: still needs port/evaluation.
- `deferred`: reviewed and intentionally moved to a later design or port phase.
- `skipped`: reviewed and intentionally not needed.

Future upstream ports should include trailers in the local commit message:

```text
Upstream-Origin: RC-CHN/ReuleauxCoder@<sha>
Upstream-Status: ported
Upstream-Local-Mode: split
```

## Baselines

| Upstream range | Local status | Evidence |
| --- | --- | --- |
| up to upstream `v0.2.6` | merged | `5b91e09 merge: 同步上游 v0.2.6 到 ezcode` |
| upstream `v0.2.8` | merged | `39aa257 merge: sync upstream v0.2.8 into ezcode` |
| upstream `v0.2.9` | merged | `e23231d merge: 同步上游 v0.2.9 到 ezcode` |
| upstream `f3a0505..93b6283` | merged/equivalent | `e23231d`, plus `git cherry` marks these commits patch-equivalent |

## Important Mappings

| Upstream | Status | Local evidence | Notes |
| --- | --- | --- | --- |
| `8e9d9e6` Windows CI and cross-platform shell | ported | `cfdec34`, `84b7dc2` | Split into CI matrix and stronger shell/platform implementation. Git still reports upstream commit as missing because patch-id differs. |
| `629e356` defer sub-agent injection with pending tool calls | superseded | `20f7ebb`, `2a6a507` | Buffered injection was ported first, then replaced by durable AgentRun delegation. |
| `9d1d924` unify `agent` tool `task/tasks` | superseded | `a9c210b`, `2a6a507`, `7232655` | Local model now uses `delegate_agent(agent_id, task)` over AgentRun. |
| `cef3a63` Windows-safe `Path.home()` test | ported | `84b7dc2`, `c0038e9` | Current test patches `Path.home` directly. |
| `a2b7300` hook-modified messages reach provider params | ported | `afb1905` | Local provider request rebuild path carries transformed messages/tools forward. |
| `4e5b29f` README update | superseded | `dde129d`, `e1379a1`, `3b4e654` | Local docs describe Labrastro/EZCode product boundaries. |

## Current Groups

### Ported executor improvements

- `950727b` and `8623800`: added `list_file` and Markdown-safe filename escaping in `ba3383c`.
- `6eb9e2f`: tool description now recommends `list_file` for project structure/path exploration in `ba3383c`.

Coverage: Python builtin tool, remote relay dispatch, Go peer execution/features, approval defaults, and tests.

### Ported LLM and context correctness

- `4db57d8`: reasoning-only assistant messages now receive a non-empty placeholder in `45bcbcf`.
- `5794cd8`: snip protection now keeps recent assistant tool-call rounds, default 2, in `45bcbcf`.
- `ac21249`: after-snip token reporting now uses compressed real-time totals in `45bcbcf`.
- `88e457d`: session preview UX remains pending for a later session/frontend batch.

Coverage: `LLMResponse.message`, LLM sanitizer, context manager, config defaults, and targeted tests.

### LSP capability group

Upstream commits:

- `5601519` LSP data types, registry, config
- `38553be` LSP client, manager, diagnostic hooks
- `df59435`, `6ca1628` AppRunner wiring and startup feedback
- `e8f12ae`, `336021c`, `06604ee`, `a731858`, `d4f063d`, `ee9b9b2` lifecycle and path fixes
- `e4f88fa` immediate diagnostics after edits
- `090aa00` active `lsp` tool
- `452868f`, `4cc7cbb`, `2025c9e` docs/tests

Current status: deferred. Upstream LSP assumes the host process can see the edited workspace, while
EZCode often executes through remote peers and Labrastro server control paths. The port should decide whether LSP runs
on host, peer, or both.

## Operational Rule

When reviewing new upstream commits, do this order:

1. Fetch upstream.
2. Check Git ancestry and `git cherry`.
3. Read `docs/upstream-sync/reuleauxcoder.yaml`.
4. Treat `pending` and `partial` entries as actionable.
5. Treat `deferred` entries as intentionally delayed until their notes' design gates are resolved.
6. Treat `ported`, `merged`, and `superseded` entries as already accounted for unless the notes say otherwise.
