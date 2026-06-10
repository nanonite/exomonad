# Plan: Root TL Idle/Shutdown Convergence

## Context

The cross-harness mailbox (Chainlink #479) shipped, and the root TL prompt now carries a
standing instruction to call `check_inbox` "at the start of each task and after completing each
major step." In practice, once all chainlink work is done and no agents remain, the TL has no
termination condition — it keeps calling `check_inbox`, gets `{"count": 0, "messages": []}`, and
sits there indefinitely with nothing productive to do.

We need a convergence protocol: after a run of empty `check_inbox` results, the TL verifies no
work remains (open chainlink issues, live spawned agents) and, if truly idle, gracefully shuts
down the shared `exomonad serve` process so the human knows the run is complete.

---

## Design

### 1. Idle counter — prompt-level, no new state

The TL counts consecutive empty `check_inbox` results **in its own context** (the same
conversation persists across wake-ups). Threshold: **20**, hardcoded as a prompt convention —
no config/schema changes. The counter resets to 0 whenever a message arrives or new work is
spawned.

### 2. New tool: `has_pending_work` (root-only)

Combines two checks into one call:
- Open chainlink issues (reuse the query used by the existing `chainlink_issue_list` handler,
  filtered to open/unassigned status)
- Live spawned agents other than root (reuse `AgentResolver`, the same source `list_agents`
  reads from per #479)

Returns:
```json
{
  "has_pending_work": false,
  "open_issue_count": 0,
  "alive_agent_count": 0,
  "alive_agents": []
}
```

At the 20-empty-check threshold, the TL calls `has_pending_work`. If `true`, reset the counter
and continue idling — do not shut down.

### 3. New tool: `shutdown_server` (root-only)

Triggers the same graceful-shutdown signal already used by the existing `/shutdown` HTTP
endpoint (`rust/exomonad/src/serve.rs`, `shutdown_endpoint` — a `tokio::sync::Notify` that stops
the UDS/TCP listeners after responding `{"status": "ok"}`).

**Server-side safety check (mandatory, not just prompt-trusted):** the effect handler calls
`AgentResolver` itself before honoring the request. If any non-root agent appears alive, it
refuses:
```json
{ "success": false, "error": "2 agent(s) still alive: [leaf-a, reviewer-b]" }
```
Only when the resolver shows no other live agents does it trigger the shutdown `Notify` and
return `{ "success": true, "message": "Server shutting down" }` — response is flushed before the
listener actually stops, same as today's `/shutdown`.

Both tools are registered **root-only** (`RootRole.hs`) — sub-TLs must never be able to take down
the shared server out from under siblings.

---

## Implementation

### Proto

**New file**: `proto/effects/lifecycle.proto` (mirrored at
`rust/exomonad-proto/proto/effects/lifecycle.proto`)

Types: `HasPendingWorkEffect`, `HasPendingWorkResult { has_pending_work, open_issue_count,
alive_agent_count, repeated AgentInfo alive_agents }`, `ServerShutdownEffect`,
`ServerShutdownResult { success, error, message }`.

### Haskell tools

**New file**: `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Lifecycle.hs`
- `has_pending_work` — no args, emits `lifecycle.has_pending_work` effect
- `shutdown_server` — no args, emits `lifecycle.shutdown_server` effect

Follow the existing tool pattern (see `Inbox.hs` / `Agents.hs` from #479 for the shape).

**Modify**: `.exo/roles/devswarm/RootRole.hs` — register both tools, root-only.

### Rust handlers

**New file**: `rust/exomonad-core/src/handlers/lifecycle.rs`
- `handle_has_pending_work()` — calls the existing chainlink open-issue query (refactor a shared
  helper out of the `chainlink_issue_list` handler if it isn't already a standalone function) and
  `AgentResolver::list_all()` filtered to alive, non-root agents.
- `handle_server_shutdown()` — calls `AgentResolver::list_all()` for alive non-root agents; if
  any, return the refusal result. Otherwise trigger the shutdown `Notify`.

**Modify**: `rust/exomonad/src/serve.rs` — the shutdown `Notify` currently lives in the state used
by `shutdown_endpoint`. Thread it (or an `Arc` clone) into the shared state/`EffectContext` that
`EffectRegistry` handlers can reach, so `handle_server_shutdown()` can call `notify.notify_one()`
the same way the HTTP endpoint does. Do not duplicate the shutdown logic — both paths trigger the
same `Notify`.

Register both effect namespaces (`lifecycle.has_pending_work`, `lifecycle.shutdown_server`) in
`EffectRegistry`.

### Prompt updates

**Modify**: `.exo/roles/devswarm/context/root.md` and `You are the root TL.md` — add a
"Idle / Shutdown Convergence" section:

> If `check_inbox` returns empty 20 times in a row with no new work spawned, call
> `has_pending_work`. If it reports no open issues and no live agents, call `shutdown_server` and
> stop — the run is complete. If it reports pending work, reset your counter and continue idling
> normally (do not busy-loop calling `check_inbox`).

---

## Critical Files

| File | Change |
|------|--------|
| `proto/effects/lifecycle.proto` | New |
| `rust/exomonad-proto/proto/effects/lifecycle.proto` | New (mirror) |
| `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Lifecycle.hs` | New |
| `.exo/roles/devswarm/RootRole.hs` | Modify — register `has_pending_work`, `shutdown_server` |
| `rust/exomonad-core/src/handlers/lifecycle.rs` | New |
| `rust/exomonad/src/serve.rs` | Modify — expose shutdown `Notify` to effect handlers |
| `.exo/roles/devswarm/context/root.md` | Modify — idle/shutdown convergence section |
| `You are the root TL.md` | Modify — same |

---

## Reused Infrastructure

- `AgentResolver::list_all()` — agent enumeration, same source as `list_agents` (#479)
- Existing `chainlink_issue_list` query logic — refactor into a shared helper, don't duplicate
- Existing `/shutdown` endpoint's `Notify`-based graceful shutdown — reuse the signal, not the logic path
- `check_inbox` / piggyback from #479 — no changes needed, just consumed by the new prompt logic

---

## Verification

1. `just build` / `just wasm-all` — compiles clean
2. `has_pending_work` returns `has_pending_work: true` when chainlink has open issues, `false`
   when none and no agents alive
3. `shutdown_server` refuses with the alive-agent list when a leaf/reviewer is still running
4. `shutdown_server` succeeds and the server process exits when no agents are alive — verify via
   the same mechanism used to test `/shutdown` today
5. `cargo test --workspace` — existing tests pass
6. Manual: drive a root TL through a full chainlink backlog to empty, confirm it eventually calls
   `has_pending_work` then `shutdown_server` rather than looping indefinitely
