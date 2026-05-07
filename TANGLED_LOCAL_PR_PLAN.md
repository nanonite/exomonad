# Plan: Fully Local PR Review (Tangled replaces GitHub)

**Status:** Implementation in progress. ~90% complete. This is the authoritative plan — supersedes
`TANGLED_MIGRATION_PLAN.md`, `TANGLED_CI_REVIEWER_PLAN.md`, `TANGLED_INTEGRATION_PLAN.md`, and
`COMBINED_PR_WORKFLOW_PLAN.md`.

---

## Goal

Replace GitHub entirely as the remote origin and PR review surface. All of the following must work
without a network connection or GitHub account:

- Agent pushes branch → local Tangled knot (Docker container, `localhost:5555`)
- Spindle runs CI pipeline (`nixery` engine, `.tangled/workflows/ci.yml`)
- CI status tracked by exomonad via spindle WebSocket
- PR created in local registry (`.exo/prs.json`) — no `gh pr create`
- Reviewer agent (Claude/OpenCode) spawned automatically on PR creation
- Reviewer reads diff from local worktree, posts review to `.exo/reviews/pr_N.json`
- `worktree_event_watcher` fires `[PR READY]` / `[FIXES PUSHED]` / `[STUCK]` to TL
- `merge_pr` gates on CI success + reviewer approval

---

## Architecture

```
Worker pushes branch → git@local-tangled (knot, :5555)
  ↓ knot emits sh.tangled.pipeline on /events WebSocket
Spindle (:6555) receives event → clones repo via HTTP from knot → runs ci.yml
  ↓ spindle emits sh.tangled.pipeline.status on /events WebSocket
exomonad worktree_event_watcher subscribes → updates ci_status_map[branch]
  ↓
Worker calls file_pr → creates entry in .exo/prs.json + pushes branch to tangled remote
  ↓
worktree_event_watcher detects new PR → spawns reviewer agent (spawn_reviewer_subtree)
Reviewer agent reads worktree diff → writes .exo/reviews/pr_N.json
  ↓
watcher reads review file → fires WASM event handlers → TL gets [PR READY] or [FIXES PUSHED]
  ↓
TL calls merge_pr → gates: stuck=false, CI=Success, reviewer=Approved, no conflicts
```

---

## What Is Already Built

These are done and in main. Do not re-implement.

| Piece | File | Status |
|-------|------|--------|
| CI subscriber (knot + spindle WebSocket) | `worktree_event_watcher.rs` | ✅ |
| `ci_status_map` (Arc<RwLock<HashMap>>) | `worktree_event_watcher.rs` | ✅ |
| Local PR registry (`.exo/prs.json`) | `file_pr_local.rs` | ✅ |
| Local merge (git merge + registry update) | `merge_pr_local.rs` (6 gates) | ✅ |
| `spawn_reviewer_subtree()` | `agent_control/spawn.rs:1120` | ✅ |
| Reviewer role + WASM | `ReviewerRole.hs` | ✅ |
| Stuck / max-rounds logic | `worktree_event_watcher.rs` | ✅ |
| `tangled_knot_url`, `tangled_spindle_url`, `tangled_owner_did` in config | `config.rs` | ✅ |
| `.tangled/workflows/ci.yml` | `.tangled/workflows/ci.yml` | ✅ |
| Per-agent git identity in worktree | `git_worktree.rs` | ✅ |
| `exomonad new` config template with tangled fields | `new.rs` | ✅ |

---

## What Remains (the 10%)

### Issue #110 — `exomonad init`: register repo with knot + set tangled remote

**Why:** Without repo registration, the knot doesn't know the repo exists — no post-receive hook
fires, no pipeline event is emitted, no CI runs. This is the prerequisite for everything else.

**Approach:** Docker exec (XRPC service auth blocks the clean path in dev mode).

When `tangled_knot_url` AND `tangled_owner_did` AND `tangled_knot_container` are set in config:

1. `docker exec $container sh -c "mkdir -p /home/git/repositories/owner/$repo_name.git && git init --bare ... && chown -R git:git ..."`
2. `docker exec $container sh -c "mkdir -p /home/git/repositories/$owner_did && ln -sfn ../owner/$repo_name.git /home/git/repositories/$owner_did/$repo_name"`
3. `sqlite3 $spindle_db "INSERT OR IGNORE INTO repos ..."`
4. `git remote add tangled git@local-tangled:repositories/owner/$repo_name.git` (idempotent)

Config fields needed:
```toml
tangled_knot_url     = "ws://localhost:5555"
tangled_spindle_url  = "ws://localhost:6555"
tangled_owner_did    = "did:plc:localdev"
tangled_knot_container = "tangled-knot-knot-1"   # Docker container name
tangled_spindle_db   = "/path/to/spindle.db"      # absolute path
```

The repo name is derived from `git remote get-url origin` (last path component, strip `.git`) or
falls back to the project directory name.

**File:** `rust/exomonad/src/init.rs` — add `register_tangled_repo()` function called near end of
`run()`.

---

### Issue #112 — Auto-spawn reviewer on PR creation

**Why:** `spawn_reviewer_subtree()` exists but nothing calls it. PRs sit unreviewed forever.

**Where:** `worktree_event_watcher.rs` — `process_observations()` method.

**Change:** When a PR entry is found in `.exo/prs.json` with `state == "open"` and no reviewer
worktree exists for that PR number, call `spawn_reviewer_subtree()`.

Add `reviewer_spawned: bool` to `WatchState` to prevent double-spawn on the next poll cycle.

Reviewer agent context (passed as task to the spawned agent):
- PR number, head branch, base branch
- Path to diff: `git diff $base..$head`
- Write review to `.exo/reviews/pr_$N.json` with fields: `approved: bool`, `comments: Vec<String>`
- Call `notify_parent` when done

**Files:**
- `rust/exomonad-core/src/services/worktree_event_watcher.rs`
- `rust/exomonad-core/src/services/agent_control/spawn.rs` (verify `spawn_reviewer_subtree` signature)

---

### Issue #113 — CI merge gate (gate 7)

**Why:** `merge_pr_local.rs` has 6 gates but none check CI status. PRs can merge even when CI
is failing or hasn't run.

**Change:** Add gate 7 in `check_merge_gates()`:

```rust
// Gate 7: CI must pass when spindle is configured
if self.spindle_url.is_some() {
    match self.ci_status_map.read().await.get(&pr.head_branch) {
        Some(CIStatus::Success) | Some(CIStatus::Neutral) => {}
        Some(status) => return Err(MergeError::CiNotPassed(*status)),
        None => return Err(MergeError::CiNotPassed(CIStatus::Unknown)),
    }
}
```

**Files:**
- `rust/exomonad-core/src/services/merge_pr_local.rs`

---

### Issue #114 — Verify `file_pr` pushes to `tangled` remote, not `origin`

**Why:** `file_pr` currently pushes the branch before creating the PR entry. If it pushes to
`origin` (GitHub), the knot never sees the branch and CI never runs.

**Check:** Read `rust/exomonad-core/src/services/file_pr_local.rs` and confirm the push remote.
If it's `origin`, change it to check for a `tangled` remote first (fall back to `origin` if not
set) — or make the remote configurable via config.

**Files:**
- `rust/exomonad-core/src/services/file_pr_local.rs`

---

### Issue #115 — Populate `.exo/review-policy.toml` defaults

**Why:** The stuck/max-rounds logic reads from `review-policy.toml` but if the file is missing or
empty the logic silently uses zero/none values, breaking the convergence loop.

`exomonad new` should write a default `review-policy.toml` with documented values:

```toml
min_review_rounds        = 1
reviewer_max_rounds      = 2
reviewer_max_wait_seconds = 1200
review_freshness_window_secs = 1200
external_review_threshold = 300
```

**Files:**
- `rust/exomonad/src/new.rs` — add `write_review_policy()` call
- Template content: match the defaults in CLAUDE.md review policy table

---

## Init Sequence (after all issues land)

```bash
# One-time machine setup (tangled-knot docker compose already running):
# Fill in .exo/config.toml after exomonad new generates the template

chainlink init --no-hooks        # optional: set up local issue tracker
exomonad new                     # creates .exo/config.toml template + review-policy.toml
# edit .exo/config.toml: fill in tangled_knot_url, tangled_spindle_url,
#                         tangled_owner_did, tangled_knot_container, tangled_spindle_db
exomonad init                    # registers repo with knot, sets tangled remote, starts session
```

After `exomonad init`:
- `git remote tangled` is set
- Repo is registered with knot + spindle knows about it
- `exomonad serve` is running, subscribed to both knot and spindle WebSockets
- Next `file_pr` call → pushes to tangled → CI runs → reviewer spawns → TL gets notified

---

## Issue Priority Order

```
#110 → #114 → #112 → #113 → #115
```

`#110` is the prerequisite (no CI without repo registration). `#114` must be verified before
`#112` (reviewer only helps if the branch actually lands on the knot). `#115` is polish.

---

## Files That Will Not Change

These are done and stable:

- `tangled-knot/docker-compose.yml` — knot infrastructure
- `rust/exomonad-core/src/services/worktree_event_watcher.rs` — CI subscriber (done; reviewer spawn is additive)
- `.tangled/workflows/ci.yml` — CI pipeline definition
- `rust/exomonad-core/src/services/agent_control/spawn.rs` — `spawn_reviewer_subtree()` already correct
- `haskell/wasm-guest/...` — ReviewerRole.hs correct (duplicate import fixed)
