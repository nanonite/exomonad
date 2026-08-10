# Cross-Harness Inbox Integration

## Problem

ExoMonad's current `send_message` / `notify_parent` tools use a delivery-first model: write to
Claude Code's Teams inbox (Claude agents only) or inject via tmux STDIN. Non-Claude agents —
Codex, OpenCode, and Shoal — have no reliable inbox. Messages sent to them are
fire-and-forget; if tmux injection misses, they're lost with no catch-up path.

## Solution

A universal cross-harness mailbox with four mutually reinforcing layers:

1. **Persistent InboxStore** — SQLite buffer at `.exo/inbox.db`, authoritative for all runtimes
2. **`check_inbox` / `list_agents` tools** — explicit MCP poll available to any agent in any role
3. **Piggyback** — unread messages appended to every tool response as a catch-up mechanism
4. **Watcher poke** — time-based tmux injection for agents that have been idle too long

The single shared `exomonad serve` process owns the InboxStore — the existing UDS server is the
bus, no per-agent SQLite file sharing needed.

---

## Teams Inbox Sync

Claude Code agents receive messages through CC's native InboxPoller AND through InboxStore (via
piggyback / `check_inbox`). These read-state trackers are independent — CC marks JSON `isRead`,
InboxStore tracks `read_at` separately.

**Resolution: write both, accept independent read state.**

All inter-agent messaging goes through ExoMonad's `send_message` / `notify_parent` MCP tools, so
every message is written to InboxStore at send time. The Teams inbox JSON write is kept as a
fast-path for CC's InboxPoller. A Claude agent that reads via CC still has it "unread" in
InboxStore until it calls `check_inbox` — which becomes a fast no-op drain. Occasional
double-notification is acceptable; zero notification is not.

---

## Message Lifecycle (Two-Flag Model)

```
send_message / notify_parent
  → InboxStore::write_message()          ← persistent, always, all runtimes
  → Teams inbox write (Claude agents)    ← fast path, best-effort (CC InboxPoller)
  → tmux injection (all agents)          ← fast path, best-effort

Per message, two nullable timestamps:
  notified_at   set when piggybacked onto a tool response (suppress re-spam)
  read_at       set only by explicit check_inbox (authoritative drain)
```

`peek_unnotified` (piggyback) sets `notified_at` — message surfaces exactly once inline.
`drain_unread` (`check_inbox`) sets `read_at` — message is fully acknowledged.
Both flags are independent: a piggybacked message that was ignored still shows up on `check_inbox`.

---

## Reviewer Role Clarification

The PR reviewer is spawned when a dev-leaf files a PR. It persists for one review cycle:

1. Reviewer reviews the PR and either approves or posts **at most one blocking comment**
2. If blocking: dev-leaf addresses the comment, re-pushes; reviewer then approves or escalates
3. If the reviewer cannot resolve (needs human input), it notifies the TL with `[STUCK]`
4. **The watcher, not the reviewer**, is responsible for monitoring CI status and relaying
   `merge_pr` to the TL once both reviewer approval and CI pass are confirmed

The reviewer uses `list_agents` to resolve the dev-leaf identity and `check_inbox` to read the
dev-leaf's response after posting a blocking comment.

---

## Role Matrix for New Tools

| Role | `check_inbox` | `list_agents` | Notes |
|------|:---:|:---:|-------|
| root | ✓ | ✓ | See agent liveness and unread counts across all spawned children |
| tl | ✓ | ✓ | Sub-TL same as root |
| dev | ✓ | — | Read reviewer feedback; send response back via notify_parent |
| worker | ✓ | — | Read parent replies |
| reviewer | ✓ | ✓ | Resolve dev-leaf identity; read dev-leaf response to blocking comment |

---

## Implementation Phases

### Phase 1 — InboxStore (Rust)

**New file**: `rust/exomonad-core/src/services/inbox_store.rs`

SQLite schema at `.exo/inbox.db`:
```sql
CREATE TABLE messages (
  id          INTEGER PRIMARY KEY,
  from_agent  TEXT    NOT NULL,
  to_agent    TEXT    NOT NULL,
  content     TEXT    NOT NULL,
  summary     TEXT,
  created_at  INTEGER NOT NULL,
  notified_at INTEGER,
  read_at     INTEGER
);

CREATE TABLE agent_inbox_meta (
  agent_id            TEXT    PRIMARY KEY,
  last_check_inbox_at INTEGER
);
```

Key methods:
- `write_message(from, to, content, summary) -> MessageId`
- `peek_unnotified(agent) -> Vec<Message>` — returns unnotified, marks `notified_at`
- `drain_unread(agent) -> Vec<Message>` — returns unread, marks `read_at`, updates `last_check_inbox_at`
- `agents_needing_poke(threshold_secs) -> Vec<(AgentId, usize)>` — agents with unread AND stale check timestamp

Wrap in `Arc<InboxStore>`, inject via `AppState`.

### Phase 2 — Wire delivery through InboxStore

**Modify**: `rust/exomonad-core/src/services/delivery.rs`

In `deliver_to_agent()`, write to `InboxStore::write_message()` before the Teams inbox / tmux
attempts. InboxStore write is the durable record; existing delivery paths remain best-effort.

### Phase 3 — Piggyback on tool responses

**Modify**: `rust/exomonad/src/serve.rs`

After `plugin_manager.call("handle_mcp_call", ...)` returns success, call
`inbox_store.peek_unnotified(agent_name)`. If messages exist, append to `content[0].text`:

```
<unread-mail>
[from: alice] Summary: fix merged. Full message: ...
</unread-mail>
```

Agent name available from URL path `/agents/{role}/{name}/`. No Haskell changes needed.

### Phase 4 — Proto types

**New file**: `proto/effects/inbox.proto`

Types: `InboxCheckEffect`, `AgentListEffect`, `InboxCheckResult`, `AgentListResult`

Also update `rust/exomonad-proto/proto/effects/inbox.proto` (proto is mirrored in two places).

### Phase 5 — New Haskell tools

**New files**:
- `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Inbox.hs` — `check_inbox` tool, emits `inbox.check` effect
- `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Agents.hs` — `list_agents` tool, emits `agent.list` effect

`list_agents` returns `{ agent_id, agent_type, birth_branch, has_unread, last_check_inbox_at, is_alive }`.
`is_alive` = no `exited_at` tombstone in `.exo/agents/{name}/`.

### Phase 6 — Rust effect handlers

**New file**: `rust/exomonad-core/src/handlers/inbox.rs`
- `handle_inbox_check()` — calls `InboxStore::drain_unread()`

**Modify**: `rust/exomonad-core/src/handlers/agent.rs`
- Add `handle_agent_list()` — joins `AgentResolver::list_all()` with InboxStore metadata

Register both in `EffectRegistry`.

### Phase 7 — Role configs

**Modify** `.exo/roles/devswarm/`:
- `DevRole.hs`, `WorkerRole.hs` — add `check_inbox`
- `TLRole.hs`, `RootRole.hs`, `ReviewerRole.hs` — add `check_inbox` + `list_agents`

### Phase 8 — Watcher timeout poke

**Modify**: `rust/exomonad-core/src/services/worktree_event_watcher.rs`

Add `Arc<InboxStore>` to `WorktreeEventWatcher`. In `poll_cycle()`, after existing PR state logic,
query `inbox_store.agents_needing_poke(threshold)` and inject via `route_tmux_message()` +
`wake_pane()` for each result.

**Modify**: `rust/exomonad-core/src/config.rs` — add `inbox_poke_interval: Option<u64>` (default 300s)

### Phase 9 — Prompt standing instructions

Update `.exo/roles/devswarm/context/root.md`, `tl.md`, `dev.md`, `worker.md`, `reviewer.md`:

> Call `check_inbox` at the start of each task and after completing each major step.
> Use `list_agents` to check which agents are alive and whether they have responded.

---

## Critical Files

| File | Change |
|------|--------|
| `rust/exomonad-core/src/services/inbox_store.rs` | New |
| `rust/exomonad-core/src/handlers/inbox.rs` | New |
| `rust/exomonad-core/src/services/delivery.rs` | Modify |
| `rust/exomonad/src/serve.rs` | Modify |
| `rust/exomonad-core/src/services/worktree_event_watcher.rs` | Modify |
| `rust/exomonad-core/src/handlers/agent.rs` | Modify |
| `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Inbox.hs` | New |
| `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Agents.hs` | New |
| `proto/effects/inbox.proto` | New |
| `rust/exomonad-proto/proto/effects/inbox.proto` | New |
| `.exo/roles/devswarm/` role configs | Modify |
| `.exo/roles/devswarm/context/*.md` | Modify |
| `rust/exomonad-core/src/config.rs` | Modify |

---

## Reused Infrastructure

- `AgentResolver::list_all()` — agent enumeration (`agent_resolver.rs`)
- `route_tmux_message()` + `wake_pane()` — tmux injection (`tmux_ipc.rs`)
- `AppState` injection pattern — service sharing in `serve.rs`
- `EffectRegistry` + `yield_effect` dispatch — existing Rust/WASM FFI boundary
- Existing Teams inbox write in `delivery.rs` — kept, not replaced

---

## Verification

1. `just build` — Rust compiles clean
2. `just wasm-all` — WASM compiles with new tools registered
3. Agent A sends message to B → B calls any tool → unread mail appears piggybacked
4. B calls `check_inbox` → drain confirmed, subsequent tool calls show no mail
5. Reviewer flow: reviewer sends blocking comment to dev-leaf → dev-leaf calls `check_inbox` →
   responds via `notify_parent` → reviewer calls `check_inbox` to read response
6. Watcher poke: agent with unread mail + stale `last_check_inbox_at` → poke fires within one poll cycle
7. `cargo test --workspace` — existing tests pass
