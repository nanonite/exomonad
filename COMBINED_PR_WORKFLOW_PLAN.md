# Combined PR Workflow Plan: Chainlink --db + Local Worktree PRs + Tangled CI

## Goal

Replace all GitHub-hosted PR effects (creation, review, polling, merge, CI) with self-hosted equivalents so the TL→leaf convergence loop runs entirely on local git worktrees. Three workstreams converge here:

1. **Chainlink `--db <path>` flag** — enables `spawn_leaf` worktree workers to issue-track from the root DB (the missing piece from [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) "Out of Scope")
2. **Local git worktree PR workflows** — `.exo/prs.json` + worktree event watcher replace `file_pr`, `merge_pr`, `github_poller`, and `copilot_review` GitHub API calls. The sibling-agent review pattern from [tangle-migration-plan-with-mergpath.md](tangle-migration-plan-with-mergpath.md) provides the review gap.
3. **Tangled CI migration** — self-hosted CI on local knot+spindle replaces `.github/workflows/ci.yml` and `.github/workflows/copilot-review.yml` (from [TANGLED_MIGRATION_PLAN.md](TANGLED_MIGRATION_PLAN.md))

---

## Architecture Overview

```
                        ┌──────────────────────┐
                        │   TL (root worktree)  │
                        │   chainlink --db .     │
                        │   + exo TL tools       │
                        └──────┬───────────────┘
                               │ spawn_leaf / spawn_subtree
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │ Leaf Worker  │  │ Leaf Worker  │  │ Reviewer     │
     │ (worktree)   │  │ (worktree)   │  │ (worktree)   │
     │ chainlink    │  │ chainlink    │  │ reviews diff │
     │  --db ../..  │  │  --db ../..  │  │ posts to     │
     └──────┬───────┘  └──────┬───────┘  │ .exo/reviews │
            │                 │           └──────┬───────┘
            │  file_pr (local)│                  │
            ▼                 ▼                  ▼
     ┌──────────────────────────────────────────────────┐
     │            .exo/prs.json  (local PR registry)     │
     │            .exo/reviews/  (per-PR review state)   │
     └──────────────────────────────────────────────────┘
                              │
                              ▼
     ┌──────────────────────────────────────────────────┐
     │     Worktree Event Watcher (replaces poller)     │
     │     watches .exo/prs.json + worktree branches    │
     │     fires same EventAction → WASM dispatch       │
     └──────────────────────────────────────────────────┘
                              │
                              ▼
     ┌──────────────────────────────────────────────────┐
     │         Tangled knot+spindle (local VM)           │
     │         .tangled/workflows/ci.yml                 │
     │         builds on push to local knot              │
     └──────────────────────────────────────────────────┘
```

---

## Part A — Chainlink `--db <path>` Flag

### A.1 Problem

`spawn_leaf` agents run in git worktrees under `.exo/worktrees/<agent-name>/`. Chainlink's `find_chainlink_dir()` walks up from CWD looking for `.chainlink/`. While it might eventually reach the project root, the git worktree boundary (`.git` file, not directory) adds fragility. More critically, `spawn_leaf` agents are isolated from the root directory — they shouldn't depend on parent-directory walking.

From `IMPLEMENTATION_PLAN.md` §Architecture Notes:
> `chainlink` DB is hard-coded to `{project_dir}/.chainlink/` — MCP chainlink tools target `spawn_worker` workers only. `spawn_leaf` worktree workers need a `--db` flag added to chainlink CLI.

### A.2 Implementation

All changes in `/home/goya/agent-workspace/chainlink/chainlink/`.

**Step 1: Add `--db` global arg to CLI struct** (`src/main.rs` ~line 23):

```rust
#[derive(Parser)]
#[command(name = "chainlink", version, about = "...")]
pub struct Cli {
    #[arg(long = "db", env = "CHAINLINK_DB", global = true,
           help = "Path to .chainlink directory or issues.db file")]
    pub db_path: Option<PathBuf>,

    #[arg(short = 'q', long, global = true, help = "Quiet mode")]
    pub quiet: bool,

    #[arg(long, global = true, help = "Output as JSON")]
    pub json: bool,

    #[arg(long, env = "CHAINLINK_LOG", global = true,
           default_value = "warn", help = "Log level")]
    pub log_level: String,

    #[arg(long, env = "CHAINLINK_LOG_FORMAT", global = true,
           default_value = "text", help = "Log format")]
    pub log_format: String,

    #[command(subcommand)]
    pub command: Commands,
}
```

**Step 2: Modify `get_db()` to accept optional path** (`src/main.rs` ~line 916):

```rust
fn find_chainlink_dir() -> Result<PathBuf> {
    let mut current = env::current_dir()?;
    loop {
        let candidate = current.join(".chainlink");
        if candidate.is_dir() {
            return Ok(candidate);
        }
        if !current.pop() {
            bail!(
                "Not a chainlink repository (or any parent). \
                 Run 'chainlink init' first, or use --db <path>."
            );
        }
    }
}

fn get_db(db_path_override: Option<&PathBuf>) -> Result<Database> {
    let db_path = if let Some(override_path) = db_path_override {
        if override_path.is_dir() {
            override_path.join("issues.db")
        } else {
            override_path.clone()
        }
    } else {
        let chainlink_dir = find_chainlink_dir()?;
        chainlink_dir.join("issues.db")
    };
    Database::open(&db_path).context("Failed to open database")
}
```

**Step 3: Wire `cli.db_path` into every command dispatch** (`src/main.rs`):

Every subcommand handler currently calls `get_db()`. Update all ~20 call sites to pass `cli.db_path.as_ref()`. The `init` command ignores the override (init always creates in CWD).

**Step 4: Update help text**:

```
--db <PATH>               Path to .chainlink directory. Overrides automatic detection.
                          Use this when running from a git worktree or subdirectory
                          outside the project root. Also settable via CHAINLINK_DB env var.
```

### A.3 Integration with Exomonad Spawn

In `rust/exomonad-core/src/services/agent_control/spawn.rs` `spawn_leaf_subtree()` and `spawn_subtree()` (~line 1008-1010), inject `CHAINLINK_DB` into the agent's environment before spawning:

```rust
// In common_spawn_env or spawn_leaf_subtree:
env.insert(
    "CHAINLINK_DB".into(),
    ctx.project_dir().join(".chainlink").display().to_string(),
);
```

This mirrors the existing pattern in `agent-profile-plan.md` §6a.

### A.4 Verification

```bash
# Unit: chainlink CLI accepts --db
cargo run -- --db /tmp/test-db/.chainlink issue list --json
# → uses /tmp/test-db/.chainlink/issues.db not cwd walk-up

# Unit: CHAINLINK_DB env var
CHAINLINK_DB=/tmp/test-db/.chainlink chainlink issue list --json
# → same behavior

# Unit: --db with file path (not directory)
cargo run -- --db /tmp/test-db/.chainlink/issues.db issue list --json
# → uses the exact file

# Unit: --db ignored for `init`
cargo run -- --db /some/other/path init
# → creates .chainlink in cwd, not /some/other/path

# Integration: spawn_leaf → agent runs chainlink commands → uses root DB
just e2e-spawn-leaf-chainlink
```

**Done when:** `chainlink --db` works from a subdirectory 4 levels deep. `CHAINLINK_DB` env var works equivalently. `spawn_leaf` agents have the env var injected.

---

## Part B — Local Git Worktree PR Workflows

### B.1 Problem

Today's PR lifecycle depends on GitHub API in 10+ call sites across `file_pr.rs`, `merge_pr.rs`, `github_poller.rs`, and `copilot_review.rs`. The TL→leaf convergence loop breaks if GitHub is unreachable, rate-limited, or removed.

### B.2 Architecture: Local PR Registry (`.exo/prs.json`)

Replace `octocrab` GitHub API calls with a local JSON registry:

```json
{
  "prs": {
    "1": {
      "number": 1,
      "head_branch": "main.feat-foo.claude",
      "base_branch": "main",
      "title": "Add foo feature",
      "body": "...",
      "author_agent": "feat-foo-claude",
      "author_role": "leaf",
      "created_at": "2026-05-01T12:00:00Z",
      "state": "open",
      "review_state": "changes_requested",
      "last_review_at": "2026-05-01T12:05:00Z",
      "last_head_sha": "abc123def",
      "ci_status": "pending",
      "reviewer_agent": "reviewer-foo-gemini",
      "rounds": 2,
      "stuck": false
    }
  },
  "next_number": 2
}
```

**States:** `open`, `merged`, `closed`, `stuck`

**Review states:** `pending_review`, `changes_requested`, `approved`

### B.3 Replacing `file_pr.rs`

**File:** `rust/exomonad-core/src/services/file_pr.rs`

| GitHub API (remove) | Local replacement |
|---|---|
| `octo.pulls().list().head()` — find existing PR | Read `.exo/prs.json`, match on `head_branch` |
| `octo.pulls().create()` — create PR | Write entry to `.exo/prs.json`, assign next number |
| `octo.pulls().update()` — update PR body | Update `.exo/prs.json` entry |
| `push_bookmark` (remote push) | Already local git operation — keep |

**Implementation (new `file_pr_local.rs`):**

```rust
pub async fn file_pr_local(input: FilePRInput) -> Result<FilePROutput> {
    let prs_path = ctx.project_dir().join(".exo/prs.json");
    let mut registry = read_pr_registry(&prs_path)?;

    // Check for existing PR on this head_branch
    let existing = registry.find_by_branch(&input.head_branch);
    if let Some(pr) = existing {
        // Update
        update_pr_in_registry(&mut registry, pr.number, &input)?;
        return Ok(FilePROutput {
            pr_number: pr.number,
            created: false,
            pr_url: None, // internal: no URL
            head_branch: input.head_branch.clone(),
            base_branch: pr.base_branch.clone(),
        });
    }

    // Create new
    let number = registry.next_number;
    registry.next_number += 1;
    let pr = PrEntry {
        number,
        head_branch: input.head_branch.clone(),
        base_branch: input.base_branch.clone(),
        title: input.title,
        body: append_authoring_footer(&input)?,
        author_agent: input.agent_id.clone(),
        author_role: input.role.clone(),
        created_at: Utc::now(),
        state: PrState::Open,
        review_state: ReviewState::PendingReview,
        reviewer_agent: None,
        ..Default::default()
    };
    registry.prs.insert(number, pr);
    write_pr_registry(&prs_path, &registry)?;

    // Emit local PR filed event (not GitHub event)
    emit_pr_filed_event(&input.head_branch, number)?;

    Ok(FilePROutput {
        pr_number: number,
        created: true,
        pr_url: None,
        head_branch: input.head_branch.clone(),
        base_branch: input.base_branch.clone(),
    })
}
```

**Authoring-Agent footer (from mergepath):**

Append to PR body:
```
---
Authoring-Agent: {agent_type}
Authoring-Role:  {role}
Birth-Branch:    {full_branch}
```

### B.4 Replacing `merge_pr.rs`

**File:** `rust/exomonad-core/src/services/merge_pr.rs`

| GitHub API (remove) | Local replacement |
|---|---|
| `octo.pulls().get()` — get PR branch name | Read `.exo/prs.json` |
| `octo.pulls().merge()` — GitHub merge | `git merge <branch>` into parent, then `git push` to local knot |

**Implementation (new `merge_pr_local.rs`):**

```rust
pub async fn merge_pr_local(input: MergePRInput) -> Result<MergePROutput> {
    let prs_path = ctx.project_dir().join(".exo/prs.json");
    let registry = read_pr_registry(&prs_path)?;

    let pr = registry.get(input.pr_number)
        .context("PR not found in local registry")?;

    // Gate: review policy check
    let policy = read_review_policy()?;
    require_review_clearance(&pr, &policy)?;

    // Gate: not stuck
    if pr.stuck {
        bail!("PR #{} is STUCK — requires human intervention", pr.number);
    }

    // Local merge
    let parent_branch = pr.base_branch.clone();
    git_checkout(&parent_branch)?;
    git_merge(&pr.head_branch, &input.strategy)?;
    git_push(&parent_branch)?;

    // Update registry
    let mut registry = read_pr_registry(&prs_path)?;
    registry.prs.get_mut(&input.pr_number).unwrap().state = PrState::Merged;
    write_pr_registry(&prs_path, &registry)?;

    // Emit sibling_merged event
    emit_sibling_merged_event(&pr.head_branch, &parent_branch, pr.number)?;

    Ok(MergePROutput { merged: true, pr_number: pr.number })
}
```

### B.5 Replacing `github_poller.rs` + `copilot_review.rs`

**File:** `rust/exomonad-core/src/services/github_poller.rs` (1403 lines)
**File:** `rust/exomonad-core/src/services/copilot_review.rs` (334 lines)

The pure state machine `compute_pr_actions()` (lines 115-263 of `github_poller.rs`) is **reusable** — it takes observations, not API calls. Only the *source* of observations changes.

**New file: `rust/exomonad-core/src/services/worktree_event_watcher.rs`**

```rust
pub struct WorktreeEventWatcher<C> {
    ctx: Arc<C>,
    poll_interval: Duration,
    state: HashMap<u64, PRState>, // Same PRState struct, reused
    prs_path: PathBuf,
    review_policy: ReviewPolicy,
}

impl<C: AgentControlContext> WorktreeEventWatcher<C> {
    pub async fn run(&mut self) {
        loop {
            tokio::time::sleep(self.poll_interval).await;
            let new_observations = self.collect_observations().await?;
            for (pr_number, obs) in new_observations {
                let actions = compute_pr_actions(&mut self.state, pr_number, obs)?;
                self.execute_actions(pr_number, actions).await?;
            }
        }
    }

    async fn collect_observations(&self) -> Result<HashMap<u64, PRObservation>> {
        let registry = read_pr_registry(&self.prs_path)?;
        let mut observations = HashMap::new();

        for (number, pr) in &registry.prs {
            if pr.state != PrState::Open { continue; }

            // Get current HEAD SHA from worktree
            let worktree_path = self.ctx.project_dir()
                .join(".exo/worktrees")
                .join(&pr.author_agent);
            let current_sha = git_head_sha(&worktree_path)?;

            // Get review state from .exo/reviews/
            let review_path = self.ctx.project_dir()
                .join(".exo/reviews")
                .join(format!("pr_{}.json", number));
            let review = read_review_state(&review_path)?;

            // Get CI status from local Tangled knot
            let ci_status = query_local_ci(&pr.head_branch)?;

            observations.insert(*number, PRObservation {
                head_sha: current_sha,
                review_state: review.state,
                new_comments: review.new_comments_since(pr.last_review_at),
                ci_status,
                age: Utc::now() - pr.created_at,
            });
        }

        Ok(observations)
    }
}
```

**Events emitted (unchanged from GitHub poller):**
- `PRReviewEvent::ReviewReceived` → new reviewer comments detected
- `PRReviewEvent::ReviewApproved` → `[PR READY]` notification
- `PRReviewEvent::ReviewTimeout` → `[REVIEW TIMEOUT]` notification
- `PRReviewEvent::FixesPushed` → HEAD SHA changed while `ChangesRequested`
- `CIStatusEvent` → CI transitions
- `SiblingMergedEvent` → sibling branch merged

**HEAD-anchored cleared detection (from mergepath):**

In `compute_pr_actions()`, add:
```rust
// Require review freshness — review must be on current HEAD
if obs.review_state == ReviewState::Approved {
    let head_committer_date = git_committer_date(&head_sha)?;
    if review.submitted_at < head_committer_date {
        // Stale approval — ignore, treat as "still needs review"
        return actions; // no approval action
    }
}
```

**Rate-limit-aware signaling (from mergepath):**
```rust
// In worktree_event_watcher.collect_observations()
match query_local_ci(&branch) {
    Ok(status) => { /* normal path */ },
    Err(e) if e.is_rate_limited() => {
        emit_event(EventAction::AlertParent {
            reason: "Review agent rate-limited".into()
        });
        continue; // don't process this PR this cycle
    }
}
```

### B.6 Sibling-Agent Review (Mergepath Pattern)

**New file: `.exo/roles/devswarm/ReviewerRole.hs`**

```haskell
module ReviewerRole (reviewerRole) where

import ExoMonad.Guest.Tools (ToolSet)

reviewerRole :: ToolSet
reviewerRole = mempty
    { allowedTools = ["git_fetch", "git_diff", "git_log",
                      "read_file", "grep", "glob", "bash",
                      -- Review writes:
                      "post_review_comment",  -- writes to .exo/reviews/
                      "approve_pr",           -- writes approved to .exo/reviews/
                      "request_changes"       -- writes changes_requested
                      ]
    , spawnTools = []  -- Reviewers cannot spawn sub-agents
    , mergeTools = []  -- Reviewers cannot merge
    , autoReview = False
    }
```

**TL spawns reviewer per leaf PR:**

When TL receives `[PR READY]` → TL spawns a reviewer agent into a worktree on the same branch with `role=reviewer`:
```rust
// In TL role handler for notify_parent with status "pr_filed":
spawn_reviewer_subtree(ctx, &pr_info).await?;
```

The reviewer agent:
1. `git diff main..HEAD` — examines the diff
2. Posts review comments → writes to `.exo/reviews/pr_{N}.json`
3. Approves or requests changes → updates `.exo/reviews/pr_{N}.json`

**Review policy gate (`.exo/review-policy.toml`):**
```toml
external_review_threshold = 300          # lines changed
external_review_paths = [
    "proto/**",
    "rust/exomonad-core/src/handlers/**",
]
reviewer_max_wait_seconds = 1200         # 20 min
reviewer_max_rounds = 5                  # before Stuck
reviewer_max_rate_limit_retries = 2
```

### B.7 Stuck Terminal State (from Mergepath)

Add a `Stuck` phase to the leaf state machine. After N rounds without convergence:

```rust
// In leaf state machine
if pr.rounds >= review_policy.reviewer_max_rounds && pr.review_state == ChangesRequested {
    pr.stuck = true;
    emit_event(EventAction::NotifyParent {
        message: format!("[STUCK: {}, rounds={}]", pr.number, pr.rounds),
    });
    // TL must surface to human — cannot auto-resolve
}
```

**TL behavior on Stuck:**
- Block `merge_pr` for stuck PRs
- Surface to human with round count and diff summary
- Human decides: re-decompose / abandon / merge-with-override

---

## Part C — Tangled CI Migration (with Mergepath Folds)

Tracked in [TANGLED_MIGRATION_PLAN.md](TANGLED_MIGRATION_PLAN.md) and [tangle-migration-plan-with-mergpath.md](tangle-migration-plan-with-mergpath.md).

### Phase C.1 — Local Knot + Spindle

From `TANGLED_MIGRATION_PLAN.md` Phase A. Stand up the Tangled VM, verify ssh/API responsiveness. No exomonad code touched.

### Phase C.2 — Tangled CI Pipeline

From `TANGLED_MIGRATION_PLAN.md` Phase B. Create `.tangled/workflows/ci.yml` mirroring `.github/workflows/ci.yml`. Mirrors Haskell (cabal build+test+hlint), Rust (cargo clippy+fmt+test), proto checks, integration tests.

**+ Mergepath fold:** Add `Authoring-Agent:` / `Birth-Branch:` lines to PR body in `file_pr.rs` (Phase B addition from mergepath plan).

### Phase C.3 — Green Pipeline

From `TANGLED_MIGRATION_PLAN.md` Phase C. Push to local knot, iterate until CI is green.

**+ Mergepath fold:** HEAD-anchored cleared detection in worktree event watcher (Phase C addition from mergepath).

### Phase C.4 — Review Replacement (Sibling-Agent)

From `TANGLED_MIGRATION_PLAN.md` Phase D, rewritten per mergepath §Phase D rewrite:

Commit to **(b) Tangled-resident agent** for auto-review:
- `ReviewerRole.hs` (new)
- TL spawns reviewer per leaf PR
- Sibling-agent convergence loop replaces Copilot loop
- `.exo/review-policy.toml` gates merges
- `Stuck` terminal state after N rounds

The old Phase D (ADR-only investigation) is replaced by the implementation sketch from Part B of this plan.

### Phase C.5 — GH Actions Shutdown

From `TANGLED_MIGRATION_PLAN.md` Phase E:
- Delete `.github/workflows/ci.yml`
- Delete `.github/workflows/copilot-review.yml`
- Update `CLAUDE.md` + `.claude/rules/exomonad.md`: replace "Copilot review" with "reviewer agent"

**+ Mergepath fold:** Document reviewer identity discipline, `.exo/review-policy.toml`, and `Stuck` state.

### Phase C.6 — Reachability Decision

From `TANGLED_MIGRATION_PLAN.md` Phase F. If Tangled appview needs to reach the local knot, evaluate Tailscale Funnel or Cloudflare Tunnel.

---

## Execution Order

```
Part A (Chainlink --db)  ────────────────────────────────►  independent
Part B (Local PR workflows)  ────────────────────────────►  independent of C
    B.3 file_pr_local  ──►  B.4 merge_pr_local  ──►  B.5 event_watcher
    B.6 reviewer_role   (parallel with B.5)
    B.7 stuck_state      (follows B.6)
Part C (Tangled CI)
    C.1  ──►  C.2  ──►  C.3  ──►  C.4  ──►  C.5  ──►  C.6 (reserved)
```

**Parts A and B are independent.** Part B requires Part A only if `spawn_leaf` reviewer agents need chainlink issue tracking — for MVP reviewer agents, they can work without chainlink (they review diffs, not chainlink issues).

**Part C is independent of A and B** through Phase C.4. Phase C.4 (review replacement) **depends on Part B** being implemented, as the sibling-agent review pattern IS the review replacement.

**Recommended order:** A → B.3 → B.4 → C.1 → C.2 → C.3 → B.5 → B.6 → B.7 → C.4 → C.5

---

## Files Created/Modified

### Chainlink (Part A)

| Path | Action |
|------|--------|
| `/home/goya/agent-workspace/chainlink/chainlink/src/main.rs` | Add `--db` arg to `Cli`, modify `get_db()`, wire to all subcommands |
| `/home/goya/agent-workspace/chainlink/chainlink/src/daemon.rs` | Accept db path override |

### Exomonad (Part B)

| Path | Action |
|------|--------|
| `rust/exomonad-core/src/services/file_pr.rs` | Add local PR registry path, Authoring-Agent footer |
| `rust/exomonad-core/src/services/file_pr_local.rs` | **NEW** — local PR registry implementation |
| `rust/exomonad-core/src/services/merge_pr.rs` | Add local merge path |
| `rust/exomonad-core/src/services/merge_pr_local.rs` | **NEW** — local merge (git merge + registry update) |
| `rust/exomonad-core/src/services/worktree_event_watcher.rs` | **NEW** — replaces `github_poller.rs` + `copilot_review.rs` |
| `rust/exomonad-core/src/services/github_poller.rs` | REMOVE or archive |
| `rust/exomonad-core/src/services/copilot_review.rs` | REMOVE or archive |
| `.exo/prs.json` | **NEW** — schema + initial empty registry |
| `.exo/review-policy.toml` | **NEW** — review policy config |
| `.exo/reviews/` | **NEW** — per-PR review state files |
| `.exo/roles/devswarm/ReviewerRole.hs` | **NEW** — reviewer agent role + prohibitions |
| `rust/exomonad-core/src/services/complexity_classifier.rs` | **NEW** — PR complexity classifier (mergepath #158) |
| `rust/exomonad-core/src/services/review_thread.rs` | **NEW** — thread resolution tracking (mergepath #166) |
| `rust/exomonad-core/src/services/agent_identity.rs` | **NEW** — identity enforcement at spawn (mergepath #157) |
| `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs` | Add `ReviewerApproved`, `ReviewerRequestedChanges`, `RateLimited` variants |
| `.exo/lib/PRReviewHandler.hs` | Update to handle reviewer agent events (currently hardcoded to Copilot) |
| `rust/exomonad-core/src/services/agent_control/spawn.rs` | Inject `CHAINLINK_DB` env; add `spawn_reviewer_subtree()`; set git identity |
| `rust/exomonad-core/src/services/delivery.rs` | Add `STUCK` message format |

### Tangled CI (Part C)

| Path | Action |
|------|--------|
| `.tangled/workflows/ci.yml` | **NEW** — Tangled CI pipeline |
| `docs/decisions/tangled-migration.md` | **NEW** — ADR (replaced by this plan) |
| `.github/workflows/ci.yml` | DELETE (Phase C.5) |
| `.github/workflows/copilot-review.yml` | DELETE (Phase C.5) |
| `CLAUDE.md` | Update CI/PR-loop sections |
| `.claude/rules/exomonad.md` | Update Convergence Protocol |
| `~/.ssh/config` | Append `Host local-tangled` |

---

## Verification

### Part A

```bash
# chainlink --db flag
chainlink --db /tmp/test-db/.chainlink issue list --json
echo $?  # 0, uses custom path

# CHAINLINK_DB env
CHAINLINK_DB=/tmp/test/.chainlink chainlink issue create "test issue"
echo $?  # 0, uses env path

# spawn_leaf injects env var
# (integration test: spawn leaf, check env contains CHAINLINK_DB)
```

### Part B

```bash
# Unit: local PR registry read/write
cargo test -p exomonad-core file_pr_local::test_create_local_pr

# Unit: merge gate refuses un-reviewed PR
cargo test -p exomonad-core merge_pr_local::test_review_policy_gate

# Unit: HEAD-anchored stale review rejected
cargo test -p exomonad-core worktree_event_watcher::test_stale_review_ignored

# Unit: Stuck terminal state after N rounds
cargo test -p exomonad-core worktree_event_watcher::test_stuck_after_max_rounds

# E2E: TL → leaf → reviewer → approve → merge
just e2e-reviewer-loop
# Spawns TL, leaf files PR, TL spawns reviewer, reviewer approves, TL merges

# E2E: policy gate blocks merge for large PR without review
just e2e-review-policy-gate

# Unit: merge blocked by unresolved review threads (mergepath #166)
cargo test -p exomonad-core merge_pr_local::test_blocked_by_unresolved_threads

# Unit: agent cannot modify review state unless role=reviewer (mergepath #165)
cargo test -p exomonad-core merge_pr_local::test_agent_cannot_self_approve

# Unit: atomic write prevents partial-read race (mergepath #160)
cargo test -p exomonad-core worktree_event_watcher::test_atomic_review_write

# Unit: needs_human_review flag blocks merge (mergepath #161)
cargo test -p exomonad-core merge_pr_local::test_needs_human_review_blocks_merge

# Unit: identity separation enforced at merge (mergepath #157)
cargo test -p exomonad-core merge_pr_local::test_author_reviewer_separation

# Unit: complexity classifier routes through second reviewer (mergepath #158)
cargo test -p exomonad-core merge_pr_local::test_complex_pr_requires_second_reviewer
```

### Part C

```bash
# Tangled CI pipeline green
git push local-knot main
# Verify: spindle logs show all steps pass

# No remaining Copilot references in docs
grep -ri "copilot" CLAUDE.md .claude/rules/exomonad.md
# -> 0 results
```

---

## Mergepath Operational Scar Tissue (added from issue review)

### 1. Review Thread Resolution Gate (from mergepath #166)

**Lesson:** Merge can be blocked by unresolved review threads even when all CI is green — and the blocker is invisible unless you explicitly query `reviewThreads` (it doesn't show in `gh pr checks`). Agents wasted 10-30 minutes diagnosing "why can't I merge?" when the answer was a single unresolved CodeRabbit nitpick thread.

**Applied to our plan:** The `.exo/reviews/pr_{N}.json` schema must track per-thread state, and `merge_pr` must check all threads are resolved before proceeding:

```json
{
  "pr_number": 1,
  "review_state": "changes_requested",
  "threads": [
    {
      "id": "thread_1",
      "file": "src/services/file_pr.rs",
      "line": 142,
      "author": "reviewer-foo-gemini",
      "body": "This unwrap will panic on network timeout",
      "resolved": true,
      "resolved_by": "leaf-foo-claude",
      "resolution": "Replaced with proper error handling in commit abc123"
    },
    {
      "id": "thread_2",
      "file": "src/services/merge_pr.rs",
      "line": 88,
      "author": "reviewer-foo-gemini",
      "body": "Consider adding a timeout parameter",
      "resolved": false,
      "resolved_by": null,
      "resolution": null
    }
  ]
}
```

**`merge_pr` gate addition:**
```rust
fn check_threads_resolved(review: &ReviewState) -> Result<()> {
    let unresolved: Vec<_> = review.threads.iter()
        .filter(|t| !t.resolved)
        .collect();
    if !unresolved.is_empty() {
        bail!(
            "Merge blocked: {} unresolved review threads remain.\n\
             Threads: {}\n\
             Use `review_resolve_thread <thread_id>` to resolve each.",
            unresolved.len(),
            unresolved.iter().map(|t| format!("{}:{} ({})", t.file, t.line, t.id)).join(", ")
        );
    }
    Ok(())
}
```

The worktree event watcher must also surface unresolved thread count in blocked-merge diagnostics — the agent should never need to guess why merge is blocked.

---

### 2. Agent Action Boundaries (from mergepath #165)

**Lesson:** During real sessions on matchline, agents drifted into removing `needs-external-review` labels after the human authorized it in chat. CodeRabbit caught this — label removal is a **human action**, not an agent action. One-time chat authorization does not extrapolate.

**Applied to our plan:**

Add explicit prohibitions in `.exo/roles/devswarm/ReviewerRole.hs` and the role spec for leaf agents:

```haskell
-- ReviewerRole.hs agent prohibitions
agentProhibitions :: [Prohibition]
agentProhibitions =
    [ Prohibition "modify_review_state"
        "Agents must never modify .exo/reviews/pr_*.json files. \
        \Only reviewer agents (role=reviewer) may write review state. \
        \The leaf agent may not approve, request-changes, or resolve threads \
        \on its own PR."
    , Prohibition "clear_stuck_flag"
        "The 'stuck' flag in .exo/prs.json is a human-only gate. \
        \Agents must never clear it, even if the human says 'just merge it' \
        \in chat. The human must manually remove the flag."
    , Prohibition "bypass_review_policy"
        "Agents must never skip .exo/review-policy.toml gates. \
        \If a policy gate blocks merge, the agent surfaces the exact gate \
        \and asks the human to override via policy update, \
        \not via ad-hoc bypass."
    ]
```

These rules are enforced at the `merge_pr` tool level — it checks the caller's `role` field and refuses review-modifying operations if `role != reviewer`.

---

### 3. Race Condition on Review Write (from mergepath #160)

**Lesson:** 17 of 21 violations in one weekly audit were auto-merge firing before reviewer approval fully attached. Dependabot/CI-bump PRs especially prone — checks complete fast, auto-merge triggers, approval lost the race.

**Applied to our plan:**

Use atomic rename-write for `.exo/reviews/pr_{N}.json` so the worktree event watcher never sees a partially-written file:

```rust
async fn write_review_state_atomic(path: &Path, review: &ReviewState) -> Result<()> {
    let tmp_path = path.with_extension("tmp");
    let json = serde_json::to_vec_pretty(review)?;
    tokio::fs::write(&tmp_path, &json).await?;
    tokio::fs::rename(&tmp_path, path).await?;  // atomic on same filesystem
    Ok(())
}
```

The event watcher additionally applies a `review_freshness_window_ms = 500` delay after detecting a file modification before reading — prevents reading mid-write even if atomic rename is unavailable.

**Merge ordering guarantee:** The `merge_pr` tool must also confirm `review_state == "approved"` AND `review.updated_at > pr.last_head_sha_timestamp` before merging — a fresh check just before the merge command, not relying on the watcher's last poll.

---

### 4. advisory-only Blocking Flag (from mergepath #161)

**Lesson:** `needs-human-review` label was applied but branch protection didn't enforce it. Two PRs merged while carrying the label. Advisory gates that aren't enforced aren't gates.

**Applied to our plan:**

Add `needs_human_review: bool` to `.exo/prs.json` PR entries. `merge_pr` must check this field and refuse to merge if `true`:

```rust
fn check_human_review_flag(pr: &PrEntry) -> Result<()> {
    if pr.needs_human_review {
        bail!(
            "PR #{} is flagged 'needs_human_review'. \
             Only a human may clear this flag. \
             Edit .exo/prs.json and set 'needs_human_review: false' \
             to proceed, or run: just review-clear {}",
            pr.number, pr.number
        );
    }
    Ok(())
}
```

The flag is set by:
- The `Stuck` terminal state (automatically)
- Reviewer agents when they encounter something they can't assess
- CI failures on protected paths
- Any agent detecting a security-sensitive change

Only the human may clear it. The `just review-clear <N>` helper writes the flag change with a human-confirmation prompt.

---

### 5. Identity Enforcement at Spawn (from mergepath #157)

**Lesson:** Env vars don't persist across bash tool calls. Agents posted reviews under the wrong identity (author instead of reviewer) because `GH_TOKEN` was empty in a fresh shell and `gh` fell through to ambient auth. No error surfaced — the review just landed under the wrong identity.

**Applied to our plan:**

In our local worktree system, git identity is set at spawn time per-role:

```rust
// In spawn_leaf_subtree / spawn_reviewer_subtree:
fn set_agent_git_identity(worktree_path: &Path, agent: &AgentInfo) -> Result<()> {
    let name = match agent.role {
        Role::Reviewer => format!("exomonad-reviewer-{}", agent.slug),
        Role::Leaf => format!("exomonad-leaf-{}", agent.slug),
        Role::TL => format!("exomonad-tl-{}", agent.slug),
        _ => format!("exomonad-{}", agent.slug),
    };
    let email = format!("{}@exomonad.local", name);

    // Set in worktree's git config (not global)
    run_git(&["-C", &worktree_path.display().to_string(),
             "config", "user.name", &name])?;
    run_git(&["-C", &worktree_path.display().to_string(),
             "config", "user.email", &email])?;

    // Prevent ambient auth fallthrough
    run_git(&["-C", &worktree_path.display().to_string(),
             "config", "user.useConfigOnly", "true"])?;
    Ok(())
}
```

`merge_pr` verifies at merge time that the merge commit author ≠ the review author:

```rust
fn verify_author_reviewer_separation(
    pr: &PrEntry, review: &ReviewState
) -> Result<()> {
    if review.reviewer_identity == pr.author_agent {
        bail!(
            "Policy violation: PR #{} author '{}' == review author '{}'. \
             An agent must never review its own code.",
            pr.number, pr.author_agent, review.reviewer_identity
        );
    }
    Ok(())
}
```

The `.exo/reviews/pr_{N}.json` records `reviewer_identity` on every review, making the audit trail explicit.

---

### 6. Complexity-Based Review Routing (from mergepath #158)

**Lesson:** Empirical data from 9 PRs on matchline showed that cross-context review (Phase 4b, different agent in different session) caught 6x more real bugs on complex PRs vs trivial ones. The pattern: state machines, concurrency, prompt design, cross-cutting refactors, and validation/invariant code. Pure data or simple helper PRs got nothing past same-context review.

| PR type | Phase 4b bugs caught |
|----------|---------------------|
| Schema additions + service methods | 0 |
| State machine + optimistic/concurrency | 6 |
| Transactional CAS + auth | 1 |
| LLM prompt design | 1 |
| Pure filter logic | 0 |
| Two-line config tweak | N/A |

**Applied to our plan:**

Add `complexity_triggers` to `.exo/review-policy.toml`:

```toml
[review_routing]
# When these patterns match, route through a SECOND reviewer agent
# even if the first review passes. (Cross-context review catch signal.)
second_reviewer_triggers = [
    # State machines — tagged unions, FSM types
    { pattern = "enum \\w+State", glob = "**/*.rs" },
    { pattern = "state_machine|StateMachine|Fsm", glob = "**/*.rs" },

    # Concurrency / transactions
    { pattern = "runTransaction|Promise\\.all|optimistic|CAS", glob = "**/*" },
    { pattern = "tokio::spawn|rayon::|async_std", glob = "**/*.rs" },
    { pattern = "async fn.*Mutex|async fn.*RwLock", glob = "**/*.rs" },

    # Prompt design / LLM contracts
    { glob = "**/prompts/**" },
    { glob = "**/.v[0-9]+.md" },

    # Cross-cutting refactors — touches ≥3 callers
    # (detected at runtime by counting call sites)

    # Validation / invariant enforcement
    { glob = "**/validation/**" },
    { glob = "**/security/**" },
    { pattern = "invariant|must_hold|assert_eq!", glob = "**/*.rs" },
]

# Max additional latency budget for second reviewer
second_reviewer_timeout_seconds = 1800  # 30 min
```

**PR complexity classifier** at `just classify-pr <branch>`:

```rust
fn classify_pr_complexity(pr: &PrEntry, diff: &str) -> ComplexityClass {
    let mut triggers = Vec::new();

    if matches_state_machine(diff) { triggers.push("state-machine"); }
    if matches_concurrency(diff) { triggers.push("concurrency"); }
    if matches_prompt_design(diff) { triggers.push("prompt-design"); }
    if matches_cross_cutting(diff) { triggers.push("cross-cutting"); }
    if matches_validation(diff) { triggers.push("validation"); }

    if triggers.is_empty() {
        ComplexityClass::Simple   // one reviewer sufficient
    } else {
        ComplexityClass::Complex { triggers }  // route through second reviewer
    }
}
```

When a PR is classified as Complex, the TL spawns a **second reviewer agent** with a different model (e.g., leaf=claude → second reviewer=gemini) into a separate session. Only after both reviewers approve (and threads are resolved) does `merge_pr` proceed.

---

## Out of Scope

- **CodeRabbit/Codex equivalents** — the exomonad reviewer agent IS the review layer (adding a third advisory layer is duplicative for self-hosted)
- **1Password credential preflight** — revisit when reviewer identities are real Tangled accounts
- **Mergepath Playground UI** — not needed
- **AGENTS.md as canonical** — exomonad's CLAUDE.md is the root doc
- **Tangled webhook registration** — Phase C.6 deferred
- **`scripts/gh-projects/` kit** — GitHub-specific, not applicable to local worktree system

---

## References

### Mergepath issues informing this plan

| Issue | Lesson applied |
|-------|---------------|
| [#166](https://github.com/nathanjohnpayne/mergepath/issues/166) — Review thread resolution gate invisible to diagnostics | Section 1: per-thread tracking in `.exo/reviews/pr_N.json`, `merge_pr` gate checks all threads resolved |
| [#165](https://github.com/nathanjohnpayne/mergepath/issues/165) — Agents removed blocking labels with chat authorization | Section 2: explicit agent prohibitions in ReviewerRole, enforced at tool level |
| [#160](https://github.com/nathanjohnpayne/mergepath/issues/160) — Auto-merge fires before reviewer approval attaches | Section 3: atomic rename-write, freshness window, pre-merge re-check |
| [#161](https://github.com/nathanjohnpayne/mergepath/issues/161) — `needs-human-review` label advisory-only | Section 4: `needs_human_review` flag with enforced gate in `merge_pr` |
| [#157](https://github.com/nathanjohnpayne/mergepath/issues/157) — Env-var race bypasses reviewer-identity policy | Section 5: git identity set at spawn, `useConfigOnly`, merge-time author/reviewer separation check |
| [#158](https://github.com/nathanjohnpayne/mergepath/issues/158) — Proactive 4b routing for complex-change classes | Section 6: complexity triggers in `.exo/review-policy.toml`, second reviewer for complex PRs |
| [#139](https://github.com/nathanjohnpayne/mergepath/issues/139) — Credential warming breaks across Bash tool calls | Informed `CHAINLINK_DB` env var injection at spawn (Part A) |
| [#138](https://github.com/nathanjohnpayne/mergepath/issues/138) — Auto-retry CodeRabbit after rate-limit window | Informed rate-limit-aware signaling in worktree event watcher (Part B.5) |
| [#136](https://github.com/nathanjohnpayne/mergepath/issues/136) — Wait-for-reviewer state before auto-merge | HEAD-anchored detection + freshness window in worktree event watcher (Part B.2 mergepath fold) |

### Source plans

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — Chainlink MCP integration + OpenCode reliability (source of chainlink `--db` gap)
- [TANGLED_MIGRATION_PLAN.md](TANGLED_MIGRATION_PLAN.md) — Local Tangled CI migration Phases A–F
- [tangle-migration-plan-with-mergpath.md](tangle-migration-plan-with-mergpath.md) — Mergepath folds into Tangled migration plan
