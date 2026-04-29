# Plan: Fleshing out TANGLED_MIGRATION_PLAN.md with Mergepath improvements

## Context

The existing [TANGLED_MIGRATION_PLAN.md](/home/goya/agent-workspace/exomonad/TANGLED_MIGRATION_PLAN.md) covers Phases A–F for migrating exomonad's CI off GitHub Actions onto a self-hosted Tangled knot+spindle. The weakest part of that plan today is **Phase D — Copilot-review replacement**, which currently lists three pieces (auto-review, polling, merge) and three options for each (native / Tangled-resident agent / stay on GH) but does not commit to a path.

[mergepath](https://github.com/nathanjohnpayne/mergepath) is a *deterministic repository standard* and reference implementation for "multiple AI coding agents and development tools operating consistently without configuration drift." Its `REVIEW_POLICY.md` and `scripts/coderabbit-wait.sh` solve operational problems exomonad will hit the moment Copilot is removed. **The most valuable thing mergepath gives us is a concrete answer to Phase D.**

This plan surveys mergepath, classifies each idea by fit-to-exomonad, and folds the high-fit ones into the existing migration plan as concrete edits to specific phases. It is purely additive — it does not invalidate or replace TANGLED_MIGRATION_PLAN.md, only sharpens the parts marked "to be decided."

---

## Mergepath survey (what they built, what it gives us)

| Mergepath contribution | Exomonad analogue today | Fit |
|---|---|---|
| **Multi-identity author/reviewer split.** All agents commit as `nathanjohnpayne`; each agent has a separate reviewer identity (`nathanpayne-claude`, `nathanpayne-cursor`, `nathanpayne-codex`). An agent never reviews under the identity that authored. | None. All exomonad agents commit under their own git identity, and review is outsourced entirely to GitHub Copilot. | **HIGH** — directly fills the Phase D gap. |
| **Sibling-agent review pattern.** A different agent reviews the PR via its reviewer identity, posts inline comments via `gh pr review`, and approves only when satisfied. CodeRabbit (Phase 2.5) and Codex App (Phase 4a) are advisory layers on top, with manual CLI fallback (Phase 4b). | The TL waits for `[PR READY]` from Copilot; no exomonad-internal review step exists. | **HIGH** — answers Phase D option (b). |
| **`scripts/coderabbit-wait.sh` HEAD-anchored "cleared" check.** Anchors the cleared signal on the current HEAD committer date so a stale review from a prior HEAD cannot false-clear. Handles platform rate-limit state with bounded retries. Distinct exit codes: 0 cleared, 2 findings, 4 grace-window timeout (advisory), 5 rate-limit stalled (alert human). | [`github_poller.rs`](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/github_poller.rs) detects SHA change after `ChangesRequested` and fires `FixesPushed`. No HEAD committer-date anchor; no rate-limit-aware retry; no distinct human-alert exit. | **HIGH** — direct upgrade to existing poller, applies whether we stay on GitHub or migrate to Tangled. |
| **`.github/review-policy.yml`.** Per-repo policy: line-count threshold for external review, protected path globs, max-wait windows, retry budgets, advisory vs. blocking flags. Read by the agent at the start of every review cycle. | None. `merge_pr` merges unconditionally on `[PR READY]`. | **MEDIUM** — policy file is a clean way to make TL merge decisions explicit and tunable. |
| **Escalation as terminal, human-mediated state.** "The agent never resolves a fired escalation signal on its own." Once an escalation fires, only the human can take the PR over. | `[FAILED: id]` triggers TL re-decomposition or escalation-flagging, but the boundary between "TL should retry" and "human must intervene" is fuzzy. | **MEDIUM** — sharpens the leaf-loop semantics; aligns with the existing `Stuck` state-machine pattern. |
| **`Authoring-Agent:` PR description line.** Required because all PRs share one author identity; the line tells the workflow which reviewer identity to assign. | exomonad encodes the agent in the branch name (`{parent}.{slug}-{type}`), so identity is recoverable. | **LOW–MEDIUM** — branch name already carries it, but adding the explicit line is cheap and makes the Tangled PR UI self-explanatory. |
| **Phase 0 credential preflight (`scripts/op-preflight.sh`).** 1Password-backed PAT cache with biometric prompt and TTL, lets a session reuse credentials without re-prompting. | exomonad uses `GITHUB_TOKEN` env var. | **LOW** — too heavy unless and until exomonad has multi-identity reviewers backed by separate accounts. Revisit if we adopt the multi-identity model with real Tangled accounts. |
| **Canonical-files standard (`README.md`, `AGENTS.md`, `CLAUDE.md`, `DEPLOYMENT.md`, `CONTRIBUTING.md`, `.ai_context.md`); CLAUDE.md must be a thin pointer to AGENTS.md.** | exomonad's [CLAUDE.md](/home/goya/agent-workspace/exomonad/CLAUDE.md) is the substantive root doc. | **LOW** — flipping the convention buys little for a single-tool-primarily project. Skip. |
| **`docs/agents/` focused sub-files split.** | CLAUDE.md is already heavily structured with per-section headings and per-directory CLAUDE.md sub-files. | **LOW** — duplicative of existing structure. Skip. |
| **`scripts/ci/` lint checks (`check_required_root_files`, `check_no_tool_folder_instructions`, `check_dist_not_modified`, etc.).** | Project-specific structure rules; not analogous. | **LOW** — would be cargo-cult. Skip. |
| **Mergepath Playground (`mergepath/playground/index.html`) + `scripts/policy-sim.sh`.** UI for tuning review policy and replaying recent PRs against draft policy. | None. | **OUT OF SCOPE** for this plan. |

---

## Recommended folds into TANGLED_MIGRATION_PLAN.md

### Phase B addition — `Authoring-Agent:` PR line (small, cheap)

In `file_pr` ([rust/exomonad-core/src/services/file_pr.rs](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/file_pr.rs)) append a footer to the generated PR body:

```
Authoring-Agent: {agent_type}    # claude | gemini | opencode
Authoring-Role:  {role}          # tl | dev | worker | reviewer
Birth-Branch:    {full branch name}
```

Already-derivable info, but explicit in the PR body is friendlier for any future Tangled reviewer agent that has to dispatch on it without parsing branch names.

### Phase C addition — HEAD-anchored cleared detection (real bug-fix)

This applies whether or not Tangled migration proceeds. In [`github_poller.rs`](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/github_poller.rs) and [`copilot_review.rs`](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/copilot_review.rs):

1. When transitioning out of `ChangesRequested`, do not treat *any* `Approved` review as cleared. Require:
   - The review's `submitted_at` ≥ HEAD committer date, AND
   - The review's `commit_id` matches HEAD SHA at the time the review was submitted (mergepath's "wallclock_freshness_window_seconds" floor closes the cherry-pick/amend race).
2. Add explicit handling for the rate-limited-reviewer state. Today, no review = silent timeout. Add an `EventAction::AlertParent { reason: RateLimited }` distinct from `ReviewTimeout` so the TL can route it differently (mergepath: exit 5, alert human; exit 4, advisory log).
3. Capture both states in `EventAction` and the WASM event-handler dispatch ([haskell/wasm-guest/src/ExoMonad/Guest/Events.hs](/home/goya/agent-workspace/exomonad/haskell/wasm-guest/src/ExoMonad/Guest/Events.hs)).

This is the change with the biggest blast radius: it's where mergepath's operational scar tissue (their issues #136 and #138) maps onto a class of bugs we will absolutely hit on a self-hosted reviewer.

### Phase D rewrite — concrete answer using the sibling-agent pattern

Phase D in TANGLED_MIGRATION_PLAN.md asks "for each of (auto-review, polling, merge), pick (a) native, (b) Tangled-resident agent, or (c) stay on GH." With mergepath as a model, commit to **(b) Tangled-resident agent** for auto-review, and replace the ADR-only deliverable with a working implementation sketch:

1. **New role: `reviewer`.** Add `.exo/roles/devswarm/ReviewerRole.hs` mirroring `DevRole.hs`. Reviewer agents have access to `gh` (or Tangled's XRPC equivalent) and `git`, but no `fork_wave` / `spawn_*` / `merge_pr`. They can only post review comments and approve or request changes.
2. **TL spawns a reviewer per leaf PR.** When a leaf calls `notify_parent` with status `pr_filed`, the TL spawns a new reviewer agent via `spawn_gemini` (or `fork_wave` with `agent_type=claude` for higher-stakes PRs) into a worktree on the *same branch as the leaf*, with `role=reviewer` and `fork_session=false`. The reviewer's spec is "review PR #N and post comments; approve only if satisfied; never merge."
3. **Identity:** the reviewer's git config sets `user.name=exomonad-reviewer-{leaf-name}` and a distinct GH/Tangled account if available. If not, distinct authorship within a single GH account is acceptable for Phase D.0 — the *separation* matters more than the actual account, so the TL can tell author commits from review activity.
4. **Convergence loop replaces the Copilot loop:**
   - Reviewer posts comments → poller detects new comments on PR by reviewer identity → fires `EventAction::InjectMessage` to the leaf (existing wiring, unchanged).
   - Leaf addresses comments, pushes → poller detects HEAD change while a reviewer review is `ChangesRequested` → fires `FixesPushed` to TL (existing wiring, unchanged).
   - Reviewer re-checks (NEW: reviewer is woken by `EventAction::InjectMessage` on leaf push, since unlike Copilot a Tangled reviewer agent *can* re-review).
5. **Merge gate on review-policy.toml.** Add `.exo/review-policy.toml`:
   ```toml
   external_review_threshold = 300        # lines changed
   external_review_paths = ["proto/**", "rust/exomonad-core/src/handlers/**"]
   reviewer_max_wait_seconds = 1200
   reviewer_max_rate_limit_retries = 2
   ```
   `merge_pr` reads this and refuses to merge until the threshold-or-protected-path gate is cleared by an approved review from a non-author identity.

This is the MVP. CodeRabbit/Codex-equivalent "third independent reviewer" is explicitly out of scope for the first cut — exomonad's reviewer-agent IS the equivalent, just self-hosted.

### Phase D addition — escalation as terminal state

Add a `Stuck` phase to the leaf state machine ([rust/exomonad-core](/home/goya/agent-workspace/exomonad/rust/exomonad-core/) — search for `StateMachine` impls). After N reviewer rounds without convergence (default 5, configurable in review-policy.toml), the leaf transitions to `Stuck`, which:
- Blocks `merge_pr` regardless of review state.
- Sends `[STUCK: id, rounds=N]` to the TL via `notify_parent`.
- Cannot be auto-resolved by the TL — TL must surface to human, who decides re-decompose / abandon / merge-with-override.

This is mergepath's "agent never resolves a fired escalation signal on its own," translated to exomonad's typed state machine.

### Phase E addition — docs update reflects new loop

When [CLAUDE.md](/home/goya/agent-workspace/exomonad/CLAUDE.md) and [.claude/rules/exomonad.md](/home/goya/agent-workspace/exomonad/.claude/rules/exomonad.md) are updated in Phase E, replace every "Copilot review" reference with "reviewer agent" and document:
- The author/reviewer identity discipline (an agent never reviews its own code).
- The merge gate via `.exo/review-policy.toml`.
- The `Stuck` terminal state and human-handoff protocol.

---

## Critical files (existing work to consult/modify)

| Path | Phase | Modification |
|---|---|---|
| [`rust/exomonad-core/src/services/github_poller.rs`](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/github_poller.rs) | C | HEAD-anchored cleared detection, rate-limit-aware exit signaling |
| [`rust/exomonad-core/src/services/copilot_review.rs`](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/copilot_review.rs) | C | Same; rename to `external_review.rs` if reviewer-agnostic by Phase D |
| [`rust/exomonad-core/src/services/file_pr.rs`](/home/goya/agent-workspace/exomonad/rust/exomonad-core/src/services/file_pr.rs) | B | Append `Authoring-Agent:` / `Birth-Branch:` lines to PR body |
| [`haskell/wasm-guest/src/ExoMonad/Guest/Events.hs`](/home/goya/agent-workspace/exomonad/haskell/wasm-guest/src/ExoMonad/Guest/Events.hs) | C, D | New `EventAction::AlertParent { reason: RateLimited }`; `ReviewerApproved`/`ReviewerRequestedChanges` event variants |
| `.exo/roles/devswarm/ReviewerRole.hs` | D | New role mirroring `DevRole.hs` minus spawn/merge tools |
| `.exo/review-policy.toml` | D | New config file (threshold, protected paths, timeouts) |
| [`haskell/wasm-guest/src/ExoMonad/Guest/Tools/`](/home/goya/agent-workspace/exomonad/haskell/wasm-guest/src/ExoMonad/Guest/Tools/) (`MergePr.hs` if it exists, else corresponding tool) | D | Read review-policy.toml; gate merge on policy |
| [`CLAUDE.md`](/home/goya/agent-workspace/exomonad/CLAUDE.md) and [`.claude/rules/exomonad.md`](/home/goya/agent-workspace/exomonad/.claude/rules/exomonad.md) | E | Replace "Copilot" with "reviewer agent"; document identity discipline, policy file, Stuck state |
| [`TANGLED_MIGRATION_PLAN.md`](/home/goya/agent-workspace/exomonad/TANGLED_MIGRATION_PLAN.md) | (this plan) | Inline these additions into Phases B/C/D/E directly, or add a sibling Phase G referencing back |

---

## What this plan deliberately does NOT add

- **AGENTS.md as canonical, CLAUDE.md as pointer.** Exomonad's CLAUDE.md is the substantive doc and works fine; the inversion is mergepath-specific and adds only churn.
- **`scripts/ci/` structural lints.** They lint mergepath's standard, not exomonad's structure.
- **1Password preflight for credentials.** Too heavy until reviewer identities are real GH/Tangled accounts.
- **Mergepath Playground UI.** Out of scope.
- **CodeRabbit/Codex GitHub App integration.** The exomonad reviewer agent IS the equivalent — adding a third advisory layer is duplicative for a self-hosted setup.

---

## Verification (for the changes this plan adds)

1. **Phase B (Authoring-Agent line):** Spawn a `spawn_gemini` leaf, verify the resulting PR body contains the three identity lines. Trivial.
2. **Phase C (HEAD-anchored cleared):** Write a `cargo test` that reproduces the stale-review false-clear by injecting an `Approved` review with `submitted_at` predating HEAD's committer date. Test passes only when the new HEAD-anchor logic rejects it.
3. **Phase C (rate-limit signal):** Mock the GH API to return rate-limit; verify `EventAction::AlertParent { reason: RateLimited }` is emitted and the parent's tmux pane / Teams inbox receives the distinct `[REVIEWER RATE-LIMITED]` notification.
4. **Phase D (reviewer agent E2E):** New E2E test under `tests/e2e/reviewer-loop/`:
   - Spawn TL → spawn dev leaf → leaf files PR → TL spawns reviewer agent → reviewer posts `ChangesRequested` → leaf addresses, pushes → reviewer approves → TL merges via `merge_pr`.
   - Verify policy-gate: same flow with a 500-line PR but `external_review_threshold=300`, no reviewer agent ever spawned ⇒ `merge_pr` refuses with a clear error referencing the policy file.
5. **Phase D (Stuck terminal):** E2E variant where the reviewer never approves over 5 rounds. Verify TL receives `[STUCK: ...]`, `merge_pr` is blocked, and `task_list` shows the leaf in `Stuck` state requiring human action.
6. **Phase E (docs):** `grep -ri "copilot" CLAUDE.md .claude/rules/` returns no remaining references after the doc update.

---

## Open questions for the user before implementation begins

1. **Scope of this plan vs. the existing one.** Should I (a) inline these additions directly into [TANGLED_MIGRATION_PLAN.md](/home/goya/agent-workspace/exomonad/TANGLED_MIGRATION_PLAN.md), (b) keep this as a sibling plan referenced from there, or (c) treat this as scratchpad and let TANGLED_MIGRATION_PLAN.md be edited freely?
2. **Reviewer identity granularity.** For the MVP, is "distinct git author within a single GH account" sufficient, or do we need separate Tangled identities for the reviewer agents from day 1?
3. **HEAD-anchored detection — apply now, or wait for Tangled migration?** This bug-fix stands alone; I'd recommend it land independently against the current GitHub-based loop, but it can be deferred if you'd rather not touch `github_poller.rs` until the Tangled equivalent exists.
