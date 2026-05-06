# Plan: Tangled CI → Reviewer Agent Integration

## Context

The user identified a critical architectural gap: when a worker pushes a branch to the local knot, the full loop — CI execution on the spindle → CI result back to exomonad → reviewer agent spawned → review results delivered to TL — has three broken links in the current implementation.

This plan designs all three fixes as a coherent flow.

---

## Confirmed Gaps (from code inspection)

| Gap | Location | Current state |
|-----|----------|---------------|
| CI status never queried | `worktree_event_watcher.rs:307` | Hardcoded `CIStatus::Unknown` |
| `sh.tangled.pipeline.getStatus` doesn't exist | tangled-core spindle XRPC | Only `cancelPipeline`, secrets, `owner` exist |
| Reviewer never auto-spawned | `worktree_event_watcher.rs` | `spawn_reviewer_subtree()` exists but nothing calls it |
| `merge_pr_local` doesn't gate on CI | `merge_pr_local.rs:73-139` | 6 gates, none check CI pass |
| Config has no spindle URL field | `rust/exomonad/src/config.rs` | `tangled_knot_url` mentioned in issue #105 but spindle is the right target |

---

## The Correct Flow (what needs to be built)

```
Worker pushes branch → knot stores it → knot emits GitRefUpdate (WebSocket /events)
  ↓
Spindle receives GitRefUpdate → clones from knot (HTTP) → runs ci.yml pipeline
  ↓
Spindle emits PipelineStatus events on its own /events WebSocket
  ↓
exomonad worktree_event_watcher subscribes to spindle /events
  → updates in-memory CIStatus per branch
  ↓
Worker files PR → reviewer agent spawned immediately (parallel with CI)
  ↓
Reviewer agent (Claude/OpenCode) reads diff via worktree, writes .exo/reviews/pr_N.json
  ↓
worktree_event_watcher reads .exo/reviews/pr_N.json → fires WASM events → TL notified
  ↓
TL calls merge_pr (gates on CIStatus::Success + ReviewState::Approved)
```

---

## The `getStatus` Question

Issue #105 says "XRPC call to `sh.tangled.pipeline.getStatus`" — this endpoint does not exist and the spindle is event-driven, not query-based. **Do not add it.** The spindle already emits `PipelineStatus` on its `/events` WebSocket. Subscribe there, same as the appview does in `tangled-core/appview/state/spindlestream.go`.

The config field should be `tangled_spindle_url` (not `tangled_knot_url`), since CI status comes from the spindle, not the knot.

---

## Reviewer Timing: Parallel with CI (per CLAUDE.md)

CLAUDE.md Convergence Protocol is explicit: **"Reviewer agent reviews automatically on PR creation."** CI and review run in parallel. CI pass is checked at **merge time** — the TL sees `[FIXES PUSHED] PR #N — CI passing. Ready to merge.` before calling `merge_pr`.

The flow is:
1. Worker pushes → knot → spindle runs CI (async)
2. Worker files PR → reviewer agent spawned immediately (on PR creation)
3. Spindle CI result arrives → updates `CIStatus` in watcher state
4. Reviewer posts review → watcher fires WASM events → TL gets `[PR READY]` or `[FIXES PUSHED]`
5. TL calls `merge_pr` → gate checks both `CIStatus::Success` AND `ReviewState::Approved`

---

## Stuck / Max Rounds — Already Built

The stuck/failed review logic is **fully implemented**. Nothing from the Tangled integration touches it:

1. Each `ChangesRequested` review increments `WatchState.rounds` in `worktree_event_watcher.rs`
2. When `rounds >= reviewer_max_rounds` (configured as 2 in `.exo/review-policy.toml`), `compute_pr_actions()` emits `WriteRegistryStuck`
3. `set_pr_stuck()` writes `stuck = true` to `.exo/prs.json`
4. WASM event handler fires `[STUCK: agent-id]` → delivered to TL via Teams inbox
5. `merge_pr_local.rs` gate 1 blocks merge when `pr.stuck == true`

Populating `.exo/review-policy.toml` (issue #106) activates this already-written logic.

---

## Implementation — Three Changes

### 1. Spindle event subscription in `worktree_event_watcher.rs`

Add a background task that subscribes to `ws://<spindle_url>/events` and maintains a `HashMap<BranchName, CIStatus>` shared with the poll cycle. Replace line 307's `CIStatus::Unknown` with a lookup into this map.

**Files:**
- `rust/exomonad-core/src/services/worktree_event_watcher.rs` — add `SpindleEventSubscriber` struct, wire into `WorktreeEventWatcher::new()`
- `rust/exomonad/src/config.rs` — add `tangled_spindle_url: Option<String>` to both `RawConfig` and `Config`

The subscriber:
- Connects to `ws://<tangled_spindle_url>/events`
- Parses `{"nsid": "sh.tangled.pipeline.status", "event": {...}}` messages
- Maps `event.pipeline` AT-URI → branch name by correlating with prs.json `head_branch`
- Updates shared `Arc<RwLock<HashMap<BranchName, CIStatus>>>`
- Is a no-op when `tangled_spindle_url` is `None` (backward compatible)

**Dependency:** `tokio-tungstenite` (check if already in workspace before adding)

### 2. Auto-spawn reviewer on PR creation

In `process_observations()`, detect when a PR enters `PendingReview` state for the first time and no reviewer is already running for that branch. Call `spawn_reviewer_subtree()`.

**Files:**
- `rust/exomonad-core/src/services/worktree_event_watcher.rs` — add reviewer spawn logic in `process_observations()`
- `WatchState` — add `reviewer_spawned: bool` field to prevent double-spawn
- `rust/exomonad-core/src/services/agent_control/spawn.rs:1120-1156` — `spawn_reviewer_subtree()` already exists, no changes needed

### 3. CI gate in `merge_pr_local.rs`

Add gate 7 in `check_merge_gates()` (`rust/exomonad-core/src/services/merge_pr_local.rs:73-139`):

```rust
// Gate 7: CI must pass when spindle URL is configured
if let Some(ci) = ci_status {
    if *ci != CIStatus::Success && *ci != CIStatus::Neutral {
        return Err(MergeError::CiNotPassed(*ci));
    }
}
```

When `tangled_spindle_url` is `None`, gate is skipped (backward compatible).

---

## Config Changes

`rust/exomonad/src/config.rs` — add to `RawConfig` and `Config`:
```toml
tangled_spindle_url: Option<String>  # e.g. "ws://localhost:8080"
```

`CLAUDE.md` — add to config table:
```toml
tangled_spindle_url = "ws://localhost:8080"  # spindle WebSocket for CI status
```

---

## Critical Files

| File | Change |
|------|--------|
| `rust/exomonad-core/src/services/worktree_event_watcher.rs` | Add spindle subscriber, reviewer spawn trigger |
| `rust/exomonad-core/src/services/merge_pr_local.rs` | Add CI gate (gate 7) |
| `rust/exomonad/src/config.rs` | Add `tangled_spindle_url` |
| `rust/exomonad-core/src/services/agent_control/spawn.rs` | Already has `spawn_reviewer_subtree()` — no changes needed |
| `.exo/review-policy.toml` | Populate (was empty) — issue #106 |
| `.exo/prs.json` | Create (was missing) — issue #106 |
| `CLAUDE.md` | Document `tangled_spindle_url`, DID upgrade path — issue #107 |

---

## What This Does NOT Change

- The knot setup (Docker, SSH config) — still Phase C.1/C.2 manual ops
- The `.tangled/workflows/ci.yml` — already correct, iterate to green per issue #104
- `tangled-core` Go source — no changes needed; spindle's existing `/events` WebSocket is sufficient
- Stuck/max-rounds logic — already built, just needs review-policy.toml populated

---

## Known Limitation: Teams Inbox is Claude Code Only

`notify_parent` delivers via Teams inbox only when the parent is a Claude Code session. Gemini and OpenCode reviewer agents cannot receive Teams inbox messages — they fall back to tmux STDIN injection. This means `[STUCK: agent-id]`, `[PR READY]`, `[FIXES PUSHED]` notifications to the TL only work end-to-end when the TL is Claude Code.

**Deferred**: Address when adding Gemini/OpenCode reviewer support.

---

## Verification

```bash
# 1. Spindle subscriber connects and receives events
RUST_LOG=debug cargo run -p exomonad serve
# → logs: "Spindle subscriber connected", "CIStatus updated: branch=X status=success"

# 2. Reviewer auto-spawns on PR creation
# File a PR via file_pr MCP tool → tmux window for reviewer agent appears

# 3. Merge gate rejects CI-failing PRs
cargo test -p exomonad-core worktree_event_watcher
cargo test -p exomonad-core merge_pr_local

# 4. Config backward compat (no tangled_spindle_url = no-op)
# Remove tangled_spindle_url from config → CI gates skip, reviewer still spawns on PR file
```

---

## Issue Mapping

| Chainlink | Updated scope |
|-----------|--------------|
| #104 | Unchanged — iterate ci.yml to green on live spindle |
| #105 | **Changed**: implement spindle WebSocket subscriber (not XRPC getStatus); add `tangled_spindle_url` (not `tangled_knot_url`) |
| #106 | Unchanged — populate review-policy.toml, create prs.json |
| #107 | Unchanged — CLAUDE.md docs; reference `tangled_spindle_url` (not `tangled_knot_url`) |
| **New** | Auto-reviewer spawn + CI merge gate (extends #105 scope or new chainlink issue) |
