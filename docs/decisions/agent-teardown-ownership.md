# Agent Teardown Ownership (`close_self` vs orchestration-owned disposal)

**Status:** Accepted

**Date:** 2026-05-25

**Chainlink:** #414

## Context

A recurring confusion: when a dev-leaf finishes its work (files its PR, runs
`chainlink session end`, stops generating), should it close its own tmux window?
The observed behavior — a leaf that "stopped work" but whose window lingers —
looks like a bug, and prompted the question of whether the `close_self` effect
should exist at all.

The triad model (worktree + context window + actor, born and dying together)
says teardown happens once — but it does not by itself say *who pulls the
trigger*. There are two candidate owners: the agent itself (`close_self`), or the
orchestration layer (the TL via `dispose_leaf` / `close_issue_and_cleanup`, and
the reconciler via direct reaping). Mixing them is the smell.

### What actually invokes `close_self` today

`agent.close_self` (`haskell/wasm-guest/src/ExoMonad/Effects/Agent.hs`) closes the
caller's own tmux window/pane. Auditing every call site:

| Flow | Site | Live? | Notes |
|------|------|-------|-------|
| **WorkerExit hook** | `Guest/Tool/Runtime.hs` (`handleWorkerExit`) | **Yes** | Ephemeral worker pane exits → notify parent → `close_self` to clean up its own pane. Workers have no PR, no worktree, no review loop. |
| **`shutdown` MCP tool** | `Guest/Tools/Events.hs` (`shutdownCore`) | **No** | Defined and exported in the SDK, but **wired to no role** in `.exo/roles/`. Dormant escape hatch. Notifies parent then `close_self` — *without* consulting `canExit`. |
| Orphan reconciler | `services/orphan_reconciler.rs` | n/a | Does **not** use `close_self`. On session-age timeout it kills the window directly (`tmux kill-window` + `dispose_agent_resources`) and notifies the **TL** (`notify_tl_about_agent`) — external reaping, not self-close. |

So the only *live* `close_self` path is ephemeral worker cleanup. **No live flow
has a dev-leaf calling `close_self`** — and that is correct.

### Why dev-leaves must not self-close

The review loop must keep the issue-owned worktree and PR alive, but it does not
require the coding process to remain alive after its one assignment. The watcher
persists review feedback and routes it through the exact live pane when an
invocation exists; otherwise the durable inbox is consumed by the next
`resume_pr` invocation. The `DevPhase` stop hook blocks an active
`ChangesRequested` repair, while published, approved, CI, and escalated states
can exit cleanly. A later repair uses a fresh invocation in the same owner
worktree, branch, and PR. A leaf that self-closed its window would still bypass
orchestration-owned cleanup, so `close_self` remains inappropriate for dev
ownership.

## Decision

**Triad teardown is orchestration-owned.** An agent does not, in general, tear
itself down.

1. **Dev-leaves do not self-dispose resources.** A coding process exits after
   its authoritative handoff, while the issue-owned worktree, PR, and watcher
   records remain. The TL owns resource disposal through `dispose_leaf`,
   `close_issue_and_cleanup`, or the `IssueClosed` event path (see
   [agent-lifecycle-invariants](agent-lifecycle-invariants.md)); the orphan
   reconciler remains a safety net. `chainlink session end` records telemetry
   and handoff notes; it does not replace orchestration cleanup.

2. **`close_self` is justified only for ephemeral workers.** A worker pane has no
   PR, no worktree, and no review loop to protect, so self-cleanup on
   `WorkerExit` is harmless and convenient. This is the one niche where
   agent-owned teardown is appropriate.

3. **The dormant `shutdown` MCP tool is removed.** It was wired to no role and
   dangerous if revived for a leaf, because it notified-and-closed without
   consulting `canExit` — exactly the gating that protects an in-review PR. If a
   graceful self-shutdown is ever wanted later, it must be designed as a new
   worker-only path that routes through lifecycle checks instead of reusing a
   dormant escape hatch.

## Consequences

- A completed coding process may have no live window while the issue-owned PR
  remains pending. `resume_pr` recreates only the process invocation in the
  existing owner worktree, branch, and PR; it does not create a second owner.
- The reconciler remains the safety net for leaves the TL forgets to dispose;
  it reaps externally and informs the TL, never the leaf.
- There is no general-purpose `shutdown` MCP tool. `close_self` remains an
  internal effect for WorkerExit cleanup only.

## Related

- [agent-lifecycle-invariants](agent-lifecycle-invariants.md) — leaf lifecycle
  bound to issue state; `dispose_agent_resources` as the shared teardown path.
- [hylo-worktree-model](hylo-worktree-model.md) — the triad (worktree + context
  window + actor) born and dying together.
