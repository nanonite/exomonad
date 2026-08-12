# Watcher as sensor: move review workflow into the Python TL

## Context

The TL-as-loop epic moved run-level orchestration into `tl_loop` but left review
orchestration in the Rust watcher. Two components now decide when a PR is ready.

`rust/exomonad-core/src/services/worktree_event_watcher.rs` (7,808 lines) owns
semantics it should not:

| Behavior | Location |
|---|---|
| Decides when to spawn a reviewer | `should_spawn_reviewer_for_new_head` :211, `claim_reviewer_attempt` :225 |
| Spawns the reviewer | `spawn_reviewer_for_pr` :956, call sites :1005, :2026 |
| Computes authoritative merge readiness | `ci_mergeable_at` :329/:469, `merge_ready_notified` :2682/:2699 |
| Composes + delivers repair handoffs | `parent_repair_handoff_message` :562, `deliver_parent_repair_handoff` :2306, 7 fingerprint sites |
| Tracks review rounds | `distinct_changes_requested_rounds` :185 |
| Disposes reviewers | `dispose_reviewers_for_pr` :2029 |

Meanwhile `tl_loop` independently adjudicates review, applies policy gates, binds
the reviewed head, and calls `merge_pr`. The documented merge path is four layers
deep and only some of them are authorities anyone can name.

Outcome: one workflow brain (Python TL), one sensor (Rust watcher), one I/O
runtime (Rust). A partially-updated ADR already exists at
`docs/decisions/watcher-as-sensor.md` — it must be revised, see Phase 0.

## Decisions taken

1. **The TL adjudicates on reviewer evidence.** The reviewer submits structured
   findings; `tl_loop/rlm/adjudicate.py` turns them into the GO/NO-GO workflow
   decision. The reviewer's findings are binding input — the TL may not invent a
   verdict where none exists, and may not merge a head with an unresolved
   blocking finding.
2. **The TL owns acceptance criteria.** It injects Definition of Done and
   acceptance criteria at reviewer spawn rather than trusting the dev-leaf to
   have written them into the PR body.
3. **Watcher becomes a pure sensor.** Observes, appends canonical ledger facts,
   serves `watcher_pr_state`. Nothing else.
4. **A timeout is never an approval.** It parks with a named gate.
5. **Messaging/steering is out of scope** — separate Chainlink epic (below).

### Correction: repair goes to the *same* dev-leaf

Your question — "compose_repair would be the TL figuring out what is wrong and
injecting that guidance to another dev-leaf?" — the answer is the **same**
dev-leaf, via `resume_pr`.

`tl_loop/rlm/repair.py` already enforces this and dispatches only through
`resume_pr`. It preserves the issue, PR, branch, worktree, ownership chain, and
expected head SHA. Sending repair to *another* leaf is the `spawn_leaf`
orphan-PR failure mode: a new name creates a disconnected sibling branch,
usually based against `main`, and breaks the one-agent-one-branch invariant.
`root.md` carries this as an explicit anti-pattern.

If a distinct repair harness is ever wanted, express it as
`resume_pr(..., repair_harness="dev")` — never `spawn_leaf` with a new name.

### Acceptance criteria: what exists vs. what is missing

`.exo/roles/devswarm/context/reviewer.md` already defines an "Acceptance
Criteria review contract": the literal `## Acceptance Criteria` heading in the
PR body is authoritative, and a missing heading means request-changes.

The gap is provenance. Those criteria are written by the **dev-leaf** into its
own PR body — the agent being reviewed authors its own pass condition. The TL
already holds the real source (`SliceState.test_plan`, and the plan's `verify`
/ `boundary` / DONE CRITERIA). Decision 2 closes that loop.

## Target architecture

```
Forgejo / git / CI  →  watcher observes  →  ledger  →  TL consumes  →  TL acts
```

```
watcher  = facts          reviewer = evidence + findings
ledger   = memory         CI       = machine authority
TL       = workflow       dev-leaf = implementation authority
```

Canonical merge rule:

```
merge_allowed =
      tl_adjudicated_go(current_head_sha)      # from binding reviewer findings
   && ci_success_or_neutral(current_head_sha)
   && current_head_sha == live_pr_head_sha     # integrity invariant
```

Per-head state, keyed by `head_sha`: `review_findings`, `ci_state`,
`reviewer_attempt`, plus `repair_attempts` per slice.

Controller transitions:

| Trigger | TL action |
|---|---|
| PR filed / head changed | Clear prior review + CI state; `spawn_reviewer(pr, head_sha, acceptance_criteria)`; wait |
| Reviewer findings arrive | `adjudicate_review(findings, diff, criteria, head_sha)` |
| Adjudicated NO-GO | `compose_repair` → `resume_pr` (same owner); new head; respawn reviewer |
| Adjudicated GO | Record `review_ok(head_sha)`; wait for CI on the same head |
| CI success/neutral | Record `ci_ok(head_sha)`; if `review_ok(head_sha)` → `merge_pr` → verify post-merge |
| CI failure | `compose_repair` → `resume_pr` (same owner) |
| Timeout / missing CI / stale reviewer | Park with a named gate. No merge |

## Phases

### Phase 0 — Revise the existing ADR

`docs/decisions/watcher-as-sensor.md` currently says to demote
`adjudicate_review` out of the approval path. Decision 1 supersedes that.

- Rewrite the "What each current layer becomes" section: the TL adjudicates on
  binding reviewer findings; the reviewer does not approve unilaterally.
- Add Decision 2 (TL-owned acceptance criteria) and the same-leaf repair rule.
- Record messaging as explicitly out of scope with a pointer to the new epic.
- Status → Accepted.

### Phase 1 — Documentation

Removes the timeout-as-approval language, which is wrong in three places.
**It is a documentation defect, not a live vulnerability** —
`tl_loop/loop/review.py::verify_review` already requires a verdict and a
matching head, so there is no implemented Python bypass.

- `CLAUDE.md` — replace "or an allowed review timeout with passing CI, permits
  `merge_pr`" with "Review timeout parks the slice."
- `docs/guides/programming-the-tl.md` — replace "Four independent checks" with
  the canonical rule; move second-reviewer rules under "Optional policy";
  drop the "forbid the timeout-merge escape" paragraph.
- `docs/architecture/agent-system.md` + `.html` — "the TL runs the workflow;
  the watcher only observes world state". Deprecate `MergeReady` in the event
  table. Rewrite the "Controller-side merge gates" table added in `5626ab86`.
- `docs/decisions/tl-loop-event-bridge.md` — declare the target event contract
  (`pr.filed`, `pr.updated`/`pr.head_changed`, `pr.review`,
  `ci.status_changed`, `pr.merged`, `pr.merge_failed`); `merge_ready` is
  derived state, not a source event.

**Verify:** `just validate-observability-contracts`; grep `*.md` for
`REVIEW TIMEOUT` and confirm no merge-permitting language survives.

### Phase 2 — Reviewer → TL evidence channel

The reviewer MCP tools already exist in `.exo/roles/devswarm/ReviewerRole.hs`
(`approve_pr` :270, `request_changes` :293, `post_review_comment` :319) and
already call `applyEvent @ReviewerPhase`. Two things are missing: the verdict
carries no head SHA, and it reaches the TL only via the watcher re-observing
Forgejo.

1. Extend the reviewer tool args with the `head_sha` the reviewer actually read
   and structured `findings` (severity + path + rationale). Schemas live in
   `ReviewerRole.hs`; all tool definitions stay in Haskell WASM per CLAUDE.md.
2. Emit a canonical `pr.review` ledger event carrying verdict, head SHA, and
   findings. Reuse the existing effect surface — do not add a Rust MCP tool.
3. Add `spawn_reviewer` to `tl_loop/client/effects.py`, taking the PR, head SHA,
   and TL-supplied acceptance criteria.
4. Compose acceptance criteria in the TL from `SliceState.test_plan` and the
   plan's `verify` / `boundary` / DONE CRITERIA. Keep `reviewer.md`'s
   `## Acceptance Criteria` contract as the reviewer-side format.
5. Register the reviewer's new args in `.exo/roles/devswarm/test/Main.hs`
   (existing coverage at :395, :411, :736).

**Verify:** `just role-hook-tests`; `just wasm-all`; a reviewer submitting a
verdict for SHA A while the PR head is SHA B must be rejected at the TL.

### Phase 3 — Move review workflow into `tl_loop`

Additive. The watcher keeps emitting legacy events so both paths can be diffed
before anything is deleted.

1. Per-head review state in `tl_loop/state/schema.py` — add `review_findings`,
   `ci_state`, `reviewer_attempt` keyed by head SHA, plus `repair_attempts`.
   Closed keys. **`SCHEMA_VERSION` is 1 and `_version` rejects anything else —
   a bump without a migration breaks resume for in-flight runs.**
2. `pr.filed` and `pr.updated`/head-change transitions in
   `tl_loop/fsm/transition.py` and `tl_loop/loop/driver.py`. A head change
   clears prior review and CI state for that slice.
3. Reviewer spawn from the head-change transition. Attempt claiming moves from
   `claim_reviewer_attempt` into durable run state.
4. Route findings into `adjudicate_review`; route NO-GO and CI failure into
   `compose_repair` → `resume_pr` (already enforced in `tl_loop/rlm/repair.py`).
5. Narrow `verify_review` to the canonical rule; make extra-review policy an
   explicit optional predicate rather than an inline veto.
6. Decide the fate of `GO-WITH-NITS` — it currently gates on nits being written
   to the Chainlink issue. Real behavior; needs a home or a deliberate removal.
7. Timeout parks with a named gate.

**Gate:** Phase 3's reviewer spawn stays behind a flag until Phase 4 removes the
watcher's, or both will spawn.

**Verify:** new Python tests per transition keyed by head SHA; a replay fixture
where SHA A is approved and SHA B is pushed must not merge;
`just e2e-tl-loop-active` green.

### Phase 4 — Simplify the watcher

Only after Phase 3 is proven in a live run.

1. Remove `should_spawn_reviewer_for_new_head`, `claim_reviewer_attempt`, and
   the `spawn_reviewer_for_pr` call sites (:1005, :2026). Keep the spawn effect
   — the TL calls it now.
2. Remove `ci_mergeable_at` and `merge_ready_notified`. Emit `merge_ready` as a
   compatibility event behind a temporary flag, then delete.
3. Remove `parent_repair_handoff_message`, `deliver_parent_repair_handoff`, and
   the 7 `parent_handoff_fingerprint` sites.
4. Keep: PR/head/review/CI observation, canonical ledger emission,
   `watcher_pr_state`.
5. Decide where `dispose_reviewers_for_pr` (:2029) lives — it is resource
   cleanup, but currently fires on a watcher-side semantic judgment.

**Verify:** `just test`; every removed behavior must have a Python test that now
covers it; watcher line count should drop substantially.

### Phase 5 — TL-specific MCP tool cleanup

| Tool | Action |
|---|---|
| `fork_wave` | **Remove.** A sub-TL is a nested `tl_run` |
| `has_pending_work` | **Remove.** Now a controller phase predicate |
| `shutdown_server` | Remove from role registration; keep as an operator CLI path |
| `check_inbox` | Keep for human/worker delivery; remove from root/tl registration |
| `poll_workers` | Keep for the heartbeat; drop the idle-protocol framing |
| `merge_pr` | Keep; confirm registration matches the authority matrix |

`fork_wave` blast radius — 13 code files and ~20 test/doc files:
`haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs`,
`Records/Spawn.hs`, `Prompt.hs`, `.exo/roles/devswarm/{TLRole,RootRole}.hs`,
`test/Main.hs`, `rust/exomonad-core/src/protocol/mcp.rs`,
`services/agent_control/spawn.rs`, `rust/exomonad/src/init.rs`,
`rust/exomonad-core/tests/wasm_integration.rs`,
`tl_loop/client/effects.py`, `tl_loop/tests/test_{driver,effects}.py`, plus
`tests/e2e/subtl-recursive-fork-wave/` (delete or repoint) and
`docs/decisions/hylo-worktree-model.md`.

Also rewrite `.exo/roles/devswarm/context/chainlink-tl.md` (132 lines) — it
still opens with interactive-TL framing, the same problem `root.md` had in
`957a921e`.

**Verify:** `just role-hook-tests`; `just test`; the role × tool matrix in
`docs/architecture/agent-system.md` §2 must match actual registration.

## Out of scope — file as Chainlink epics

**Epic A — Agent loop and steering (new).** prime-agent's steering queue works
because prime-agent *owns* the agent loop: `runLoop` in
`packages/agent/src/agent-loop.ts:317-344` polls `getSteeringMessages` and
pushes straight into `currentContext.messages` before the next assistant
response, with a second `getFollowUpMessages` channel drained when the agent
would otherwise stop (`agent.ts:185` `PendingMessageQueue`, `one-at-a-time`).

ExoMonad does not own the loop — Claude Code, Codex, and OpenCode do. Our only
injection points are the Teams inbox, HTTP-over-UDS, and tmux paste-buffer,
which is the flakiness you have been hitting. Scope: evaluate owning the agent
loop, or building a durable per-agent steering queue drained at harness turn
boundaries with acknowledgement. Follow prime-agent's convention where it fits
the orchestrator goals.

**Epic B — TL-specific MCP tool redesign.** Phase 5 above, filed as trackable
work rather than a trailing phase if it grows.

Both blocked on the server: `.exo/server.sock` is absent, so the `chainlink_*`
MCP tools are unreachable and per your standing instruction I will not use the
Chainlink bash CLI.

## Verification (end to end)

```bash
just validate-observability-contracts   # Phase 1
just role-hook-tests                    # Phases 2, 5
just wasm-all                           # Phase 2
just rust-test                          # fast loop
just test                               # full workspace, Phases 4-5
just e2e-tl-loop-active                 # bounded controller over a scratch repo
```

Acceptance: a PR whose SHA A is approved and then advanced to SHA B must not
merge; a review timeout must park rather than merge; a NO-GO must resume the
same owner's worktree and branch; and `grep -rn 'merge_ready' rust/` should
return only compatibility-flagged or deleted paths by end of Phase 4.
