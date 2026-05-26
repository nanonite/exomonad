# Agent Teardown Ownership (`close_self` vs orchestration-owned disposal)

**Status:** Proposed

**Date:** 2026-05-25

**Chainlink:** (none yet — documents existing behavior and records an open question)

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

The convergence loop requires the leaf to stay alive after filing its PR: the
watcher injects reviewer feedback (`ChangesRequested`) into the leaf's pane, and
the leaf fixes and pushes. The `DevPhase` stop hook enforces this — `canExit`
returns `MustBlock`/blocking for `DevApproved` (waiting for watcher merge-ready),
`DevCITriggered`, `DevCIBlocked`, and `DevNeedsHumanDirection`; only `DevDone`
exits cleanly. A leaf that self-closed its window would orphan the PR and break
the loop. `close_self` bypasses `canExit` entirely, so exposing it to a dev-leaf
would be a foot-gun that defeats the stop-hook gating.

## Decision

**Triad teardown is orchestration-owned.** An agent does not, in general, tear
itself down.

1. **Dev-leaves never self-close.** A leaf that has handed off work *idles* with
   its window alive (the `FINISHING` state in `session_status`). It is disposed
   by the TL — `dispose_leaf`, `close_issue_and_cleanup`, or the `IssueClosed`
   event path (see [agent-lifecycle-invariants](agent-lifecycle-invariants.md)) —
   or reaped by the orphan reconciler on session-age timeout. `chainlink session
   end` is telemetry (handoff notes + work-state), not process termination.

2. **`close_self` is justified only for ephemeral workers.** A worker pane has no
   PR, no worktree, and no review loop to protect, so self-cleanup on
   `WorkerExit` is harmless and convenient. This is the one niche where
   agent-owned teardown is appropriate.

3. **The `shutdown` MCP tool should be narrowed or removed.** It is currently
   dead (wired to no role) and dangerous if revived for a leaf, because it
   notifies-and-closes without consulting `canExit` — exactly the gating that
   protects an in-review PR. If a graceful self-shutdown is ever wanted, it must
   route through the stop-hook `canExit` check and be scoped to roles with no
   lifecycle to protect (workers), never dev/reviewer leaves.

## Consequences

- A leaf left in `FINISHING` (window alive, work done) is **not** the leaf's bug
  — it is the TL's missing disposal. This is also why such leaves surface as
  stale rows in `session_status` (window-liveness view) while the PR/issue view
  has moved on.
- The reconciler remains the safety net for leaves the TL forgets to dispose;
  it reaps externally and informs the TL, never the leaf.
- Follow-up (not yet scheduled): delete the dormant `shutdown` tool, or re-scope
  it worker-only behind a `canExit` check, so the "should `close_self` exist"
  question is resolved in code rather than left as latent surface area.

## Related

- [agent-lifecycle-invariants](agent-lifecycle-invariants.md) — leaf lifecycle
  bound to issue state; `dispose_agent_resources` as the shared teardown path.
- [hylo-worktree-model](hylo-worktree-model.md) — the triad (worktree + context
  window + actor) born and dying together.
