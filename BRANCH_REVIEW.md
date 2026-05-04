# Branch Review: `feat/pr-refactor-tangled-integraiton`

Reference plan: `COMBINED_PR_WORKFLOW_PLAN.md`. 9 commits reviewed against plan tasks.

---

## Per-commit verdict

| Hash | Commit | Verdict |
|---|---|---|
| `5f47a4d3` | CHAINLINK_DB env var injection | ✅ Correct |
| `d0720ef4` | Local PR registry (file_pr_local) | ✅ Correct |
| `9e834e39` | Local merge (merge_pr_local) | ✅ Correct — gates, policy, 25 tests, mergepath #157/#161 coverage |
| `73a91e9b` | Worktree event watcher | ⚠️ Incomplete — old modules not removed |
| `0efc22ee` | review-policy.toml + reviewer.md | ✅ Correct |
| `c7adc062` | Role::reviewer() + spawn_reviewer_subtree | ⚠️ Incomplete — Haskell WASM side missing |
| `8d8604c4` | Stuck terminal state + thread tracking | ✅ Correct |
| `a350d002` | complexity_classifier | ⚠️ Diverges from plan spec |
| `fe62a970` | chore: changelog, archive, plan doc | ⚠️ Plan doc committed after the work it describes |

---

## Issues found

### 1. github_poller.rs + copilot_review.rs not removed (`73a91e9b`)

Both are still live in `rust/exomonad-core/src/services/mod.rs` (lines 7 and 18). The plan explicitly said "REMOVE or archive." This violates the **Single Code Path** rule in CLAUDE.md — `worktree_event_watcher` and the old poller/review code now do the same thing from two paths.

### 2. ReviewerRole.hs (Haskell WASM) missing (`c7adc062`)

The plan's files table lists `.exo/roles/devswarm/ReviewerRole.hs` as **NEW**. All other roles have corresponding WASM definitions (`DevRole.hs`, `WorkerRole.hs`, etc.). The Rust `Role::reviewer()` was added but there is no WASM role enforcement. The tool restriction list (no `fork_wave`, `spawn_leaf`, `merge_pr`, etc.) is passed as a task prompt string only — it is not enforced by the WASM hook layer.

### 3. Events.hs not updated

The plan requires adding `ReviewerApproved`, `ReviewerRequestedChanges`, and `RateLimited` variants to `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs`. None found. Without these, the event handler dispatch table cannot route reviewer-specific events — the system falls back to the Copilot-event path.

### 4. PRReviewHandler.hs not updated

`.exo/lib/PRReviewHandler.hs` was supposed to be updated to handle reviewer agent events (currently hardcoded to Copilot). Not done. The worktree event watcher emits events but the WASM handler that fires on them still expects Copilot format.

### 5. complexity_classifier diverges from plan (`a350d002`)

Plan (mergepath #158) specifies **code-pattern triggers** on diff content: regex matches for `enum \\w+State`, `tokio::spawn`, `async fn.*Mutex`, paths under `**/prompts/**`, etc. The implementation uses `git diff --numstat` (filename + line count only) with `external_review_paths` glob + `external_review_threshold`. This catches large PRs and path-matched files but misses the high-value cases from the plan: state machines, concurrency patterns, and prompt design changes will not trigger the second-reviewer route.

### 6. delivery.rs STUCK format not added

The plan says to add a `STUCK` message format to `rust/exomonad-core/src/services/delivery.rs`. Nothing found there. The stuck event fires and the registry gets written, but the parent notification message format is not standardized through delivery.rs.

### 7. review_thread.rs and agent_identity.rs not created as separate modules

The plan lists both as separate NEW files. Thread resolution logic landed as struct fields in `merge_pr_local.rs`/`worktree_event_watcher.rs`, and author-reviewer separation is in `merge_pr_local.rs`. These are functionally covered but not organized as the plan intended. Minor — no CLAUDE.md rule requires a separate file per concern.

### 8. Plan doc committed last (`fe62a970`)

`COMBINED_PR_WORKFLOW_PLAN.md` was committed after all the implementation. The plan doc should be the scaffold commit that children fork from. Minor for a single-author branch.

---

## Summary

The core B.3 → B.4 → B.5 path (local PR registry, local merge, worktree event watcher) is solid with good test coverage. The major gaps are:

- **Old github_poller/copilot_review not removed** — active dual code path, violates Single Code Path rule.
- **ReviewerRole.hs missing** — reviewer tool restrictions not enforced by WASM hook layer.
- **Events.hs/PRReviewHandler.hs not updated** — reviewer events cannot route through the WASM dispatch table.
- **complexity_classifier covers a narrower set of cases than the plan** — path/line-count only, no code-pattern triggers.
