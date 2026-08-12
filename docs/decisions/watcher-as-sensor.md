# ADR: The watcher is a sensor, not a review coordinator

Date: 2026-08-12
Status: Accepted
Supersedes parts of: [tl-as-loop.md](tl-as-loop.md), [tl-loop-event-bridge.md](tl-loop-event-bridge.md)

## Context

The TL-as-loop epic moved run-level orchestration into `tl_loop`. It did not
move review orchestration. The result is a split brain: two components both
decide when a PR is ready.

`rust/exomonad-core/src/services/worktree_event_watcher.rs` is 7,808 lines and
currently owns semantics, not just observation:

| Behavior | Location |
|----------|----------|
| Decides when to spawn a reviewer for a new head | `should_spawn_reviewer_for_new_head` (:211), `claim_reviewer_attempt` (:225) |
| Spawns the reviewer | `spawn_reviewer_for_pr` (:956, :1005, :2026) |
| Computes authoritative merge readiness | `ci_mergeable_at` (:329, :469), `merge_ready_notified` (:2682, :2699) |
| Composes and delivers repair handoffs | `parent_repair_handoff_message` (:562), `deliver_parent_repair_handoff` (:2306), seven `parent_handoff_fingerprint` sites |
| Disposes reviewers | `dispose_reviewers_for_pr` (:2029) |
| Tracks review rounds against `reviewer_max_rounds` | `distinct_changes_requested_rounds` (:185) |

Meanwhile `tl_loop` independently adjudicates review, applies policy gates,
binds the reviewed head, and calls `merge_pr`. The documented merge path is
four layers deep — watcher `merge_ready`, RLM adjudicator verdict, policy veto,
head binding — and only two of those are authorities anyone can point at.

This ADR narrows the gate set and moves review workflow to the controller.

## Decision

```
watcher   = facts
ledger    = memory
TL        = workflow and adjudication
reviewer  = binding evidence and findings
CI        = machine authority
dev-leaf / worker = implementation authority
```

The watcher observes Forgejo, git, CI, and time, and appends canonical facts to
the ledger. It does not authorize merges, spawn reviewers, or compose repair
handoffs. The Python controller consumes those facts and owns reviewer spawn,
repair, merge, retry, and parking.

Raw Forgejo polling stays in Rust. Rust owns I/O; Python owns orchestration.
Moving HTTP into Python would trade one split brain for a worse one.

### The canonical merge rule

```
merge_allowed =
      tl_adjudicated_go(current_head_sha)
   && ci_success_or_neutral(current_head_sha)
   && current_head_sha == live_pr_head_sha
```

Optionally, when the project owner has declared risk surfaces:

```
   && extra_review_policy_satisfied(current_head_sha)
```

No other approval layer is required.

### What each current layer becomes

**1. Watcher `merge_ready` — demoted to derived state.** The controller can
compute it from `pr.review` and `ci.status_changed` keyed by head SHA. A
separate watcher decision adds a second opinion with no extra information.
Keep the event only as a temporary compatibility signal behind a flag, then
remove it.

**2. TL `adjudicate_review` — the workflow approval decision.** The reviewer
submits structured findings for the exact head it inspected. Those findings are
binding evidence: the TL may not invent a verdict where no reviewer evidence
exists, may not ignore an unresolved blocking finding, and may not adjudicate
one head using findings from another. `adjudicate_review` turns that evidence
into the workflow's GO/NO-GO decision. The reviewer supplies evidence and
findings; the TL owns the adjudication and the resulting merge decision.

This is the largest blast radius in this ADR — see Risks.

**3. Policy veto / second reviewer — optional human policy.** Path globs and
size thresholds are worth having when the project owner has declared risk
surfaces (security-sensitive files, public protocol files, migrations,
generated schema contracts, large diffs). They are not universally necessary
and must not be presented as a required approval layer. Default is one reviewer
+ CI + head binding.

**4. Head binding — keep, reclassified as an integrity invariant.** Not an
approval layer; a correctness guarantee. Without it:

```
SHA A reviewed and approved
→ dev pushes SHA B
→ review/CI state not bound to a SHA
→ system merges SHA B
→ unreviewed code enters the base branch
```

This one is provably necessary.

### Decision 2: TL-owned acceptance criteria

The TL owns the acceptance criteria supplied to the reviewer. It composes them
from the run plan, `SliceState.test_plan`, verification commands, boundaries,
and DONE CRITERIA, then injects the criteria when spawning the reviewer. The
dev-leaf may document criteria in the PR body, but it cannot define its own
pass condition as the authoritative source. The reviewer's literal
`## Acceptance Criteria` contract remains the format for presenting the
criteria and findings.

### Same-leaf repair rule

When the TL adjudicates NO-GO or CI failure, repair guidance returns through
`resume_pr` to the same dev-leaf. `resume_pr` preserves the issue, PR, branch,
worktree, ownership chain, and expected head SHA. A new `spawn_leaf` would
create an orphan sibling branch and violate the one-agent-one-branch
invariant. If a distinct repair harness is ever needed, it must be expressed
as `resume_pr(..., repair_harness="dev")`, never as a new leaf.

### A timeout is not an approval

A review timeout with passing CI is not mergeable. A timeout means no one
approved. The controller parks the run at the durable `tl-timeout` gate.

The controller's idle timeout now records `tl-timeout` as pending and returns
`TLFailed`; no subsequent event or gate approval can enter the merge path.

### Per-head review state

Keyed by `head_sha`:

```
current_head_sha
review_state[current_head_sha]
ci_state[current_head_sha]
reviewer_attempt[current_head_sha]
repair_attempts
```

Transitions the controller owns:

| Trigger | Controller action |
|---------|-------------------|
| PR filed / head changed | Clear prior review + CI state; `spawn_reviewer(pr_number, force=false)`; wait |
| Binding reviewer findings arrive | `adjudicate_review(findings, head_sha)`; record the TL's GO/NO-GO decision |
| TL adjudicates NO-GO | `compose_repair`; `resume_pr` on same PR/worktree/branch; new head; spawn reviewer again |
| TL adjudicates GO | Record `review_ok(head_sha)`; wait for CI on the same head |
| CI success/neutral | Record `ci_ok(head_sha)`; if `review_ok(head_sha)` then `merge_pr`, then verify post-merge state |
| CI failure | Record `ci_failed(head_sha)`; compose repair; `resume_pr` on same PR/worktree/branch |
| Review timeout / missing CI / stale reviewer | Park at `tl-timeout`. No merge |

### Repair uses `resume_pr`, never a new leaf

`resume_pr` preserves the issue, PR, branch, worktree, ownership chain, and
expected head SHA. A new dev-leaf creates a second branch and breaks the
one-agent-one-branch invariant. If a distinct repair harness is wanted, express
it as `resume_pr(..., repair_harness="dev")` — never as `spawn_leaf` with a new
name.

### Authority matrix

| Role | Implement | Review | Approve/reject | Merge |
|------|:---------:|:------:|:--------------:|:-----:|
| dev-leaf | yes | no | no | no |
| worker | yes | no | no | no |
| reviewer | no | yes; findings are binding evidence | no | no |
| TL controller | no source edits | adjudicates findings | **yes**, after binding evidence | **yes**, after gates |
| CI | no | no | no | machine gate only |

The controller adjudicates *workflow*. It must not become the reviewer, must
not invent reviewer findings or a verdict, and must require binding reviewer
evidence before a GO decision. Messaging and steering between harness turns
are out of scope for this ADR; that work is tracked separately under Milestone
M10, Agent loop ownership (#723).

---

## Implementation plan

### Phase 1 — Documentation (no code)

Cheapest, unblocks everything else, and removes the timeout-as-approval
language that is currently wrong in three places.

1. **`CLAUDE.md`** — replace "A fresh approved head with passing CI, or an
   allowed review timeout with passing CI, permits `merge_pr`" with "A fresh
   TL-adjudicated GO based on binding reviewer findings, with passing CI,
   permits `merge_pr`. Review timeout parks the slice."
2. **`docs/guides/programming-the-tl.md`** — replace "Four independent checks
   must all hold" with two authorities plus one integrity invariant. Move the
   second-reviewer rules under an explicit "Optional policy" heading. Remove
   the "forbid the timeout-merge escape" paragraph, which presumes the escape
   exists.
3. **`docs/architecture/agent-system.md` + `.html`** — change "the loop the
   watcher + dev + reviewer + controller collectively run" to "the TL runs the
   workflow; the watcher only observes world state". Deprecate `MergeReady` as
   a required signal in the event-vocabulary table; state that the controller
   derives readiness from `pr.review` + `ci.status_changed`. Rewrite the
   "Controller-side merge gates" table added in `5626ab86` to the narrowed set.
4. **`docs/decisions/tl-as-loop.md`** — add: the TL owns review orchestration;
   the watcher is a fact producer; the watcher does not authorize merges.
5. **`docs/decisions/tl-loop-event-bridge.md`** — declare the target event
   contract (`pr.filed`, `pr.updated`/`pr.head_changed`, `pr.review`,
   `ci.status_changed`, `pr.merged`, `pr.merge_failed`) and state that
   `merge_ready` is derived state, not a source event.
6. Set this ADR to Accepted.

**Verify:** `just validate-observability-contracts`; grep for
`review timeout`/`REVIEW TIMEOUT` across `*.md` and confirm no remaining
merge-permitting language.

### Phase 2 — Move review workflow into `tl_loop`

Additive. The controller starts driving the workflow while the watcher still
emits its legacy events, so the two can be diffed against each other before
anything is deleted.

1. Add per-head review state to `tl_loop/state/schema.py`. The slice already
   carries `reviewed_head`; add `ci_state`, `review_state`, and
   `reviewer_attempt` keyed by head SHA, plus `repair_attempts`. Closed keys,
   schema version bump, migration for existing `run.json` files.
2. Add controller transitions for `pr.filed` and `pr.updated`/head change in
   `tl_loop/fsm/transition.py` and `tl_loop/loop/driver.py`. A head change
   clears prior review and CI state for that slice.
3. Add a `spawn_reviewer` effect call to `tl_loop/client/effects.py` and invoke
   it from the head-change transition. Attempt claiming moves from
   `claim_reviewer_attempt` into durable run state.
4. Record review and CI state by head SHA rather than as a bare slice verdict.
5. Route review comments and CI failure to `compose_repair` → `resume_pr`.
   `compose_repair` already exists and already refuses anything but `resume_pr`.
6. Narrow `verify_review` to the canonical rule. Make the extra-review policy
   an explicit optional predicate rather than an inline veto.
7. Keep `adjudicate_review` as the TL's workflow decision over binding reviewer
   findings. Keep `GO-WITH-NITS`: the TL stores its nit reasons in durable,
   head-bound `review_findings` state, so no external Chainlink writer is
   required for merge authorization.
8. Timeout parks with a named gate. No merge path from a timeout.

**Verify:** `just rust-test` unaffected; new Python tests for each transition
keyed by head SHA; a replay fixture where SHA A is approved and SHA B is pushed
must not merge; `just e2e-tl-loop-active` green.

### Phase 3 — Simplify the watcher

Only after Phase 2 is proven in a live run.

1. Stop spawning the reviewer by default — remove
   `should_spawn_reviewer_for_new_head`, `claim_reviewer_attempt`, and the
   `spawn_reviewer_for_pr` call sites (:1005, :2026). Keep the spawn effect
   itself; the controller calls it now.
2. Stop computing authoritative `merge_ready` — remove `ci_mergeable_at` and
   `merge_ready_notified`. Emit the compatibility event behind a temporary flag,
   then delete.
3. Stop composing repair handoffs — remove `parent_repair_handoff_message`,
   `deliver_parent_repair_handoff`, and the seven
   `parent_handoff_fingerprint` sites.
4. Keep: PR state, head SHA, review state, CI state observation; canonical
   ledger emission; `watcher_pr_state` as a read effect.
5. Decide where reviewer disposal lives. `dispose_reviewers_for_pr` (:2029) is
   resource cleanup, not workflow — it can stay, but it currently fires on
   watcher-observed terminal review state, which is a semantic judgment.

**Verify:** `just test`; watcher line count should drop substantially; every
removed behavior must have a Python test that now covers it.

### Phase 4 — TL-specific MCP tool cleanup

These tools were designed for an interactive coordinator that no longer exists.
Each needs a decision: keep, rescope, or remove. Removal is the default when
nothing calls it.

| Tool | Problem | Direction |
|------|---------|-----------|
| `check_inbox` | Idle-loop polling for a coordinator that no longer idles | Keep for human/worker delivery; remove from root/tl role registration |
| `has_pending_work` | Existed only to answer "is the run over?" — now a controller phase predicate | Remove |
| `shutdown_server` | Run termination is `TLDone`/`TLFailed` | Remove from role registration; keep as an operator CLI path |
| `fork_wave` | Spawns a TL-role agent session; a sub-TL is now a nested `tl_run` | Decide whether a TL-role *agent* still has a purpose. If not, remove; if yes, document what it is for |
| `merge_pr` | Still correct, but reachable by a role-scoped agent that must not merge | Keep; confirm role registration matches the authority matrix |
| `poll_workers` | Used by the controller heartbeat and by the old idle protocol | Keep for the heartbeat; remove the idle-protocol framing |

`.exo/roles/devswarm/context/chainlink-tl.md` (132 lines) still opens with the
interactive-TL framing ("You are a TL enhanced with chainlink... across the
cognition tree") and needs the same treatment `root.md` received in `957a921e`.

**Verify:** `just role-hook-tests`; the role × tool matrix in
`docs/architecture/agent-system.md` §2 must match actual registration.

---

## Risks

**Making reviewer evidence binding is the big one.** `adjudicate_review` has
closed output schemas, context-budget handling, replay fixtures, policy gate
integration, and a `GO-WITH-NITS` path that stores nits in per-head run state.
Phase 2 step 7 must decide what survives rather than leaving it half-wired — the TL
must not adjudicate without reviewer evidence or ignore a blocking finding.

**Phase 2 and Phase 3 must not overlap in a live run.** Both components driving
reviewer spawn simultaneously means double-spawn. Gate Phase 2's reviewer spawn
behind a flag until Phase 3 removes the watcher's.

**The schema migration in Phase 2 step 1 touches existing `run.json` files.**
`SCHEMA_VERSION` is currently 1 and `_version` rejects anything else, so a bump
without a migration path breaks resume for in-flight runs.

## Rejected alternatives

**Move Forgejo polling into Python.** Rust owns I/O; this would duplicate the
effect boundary and give Python two jobs.

**Bundle the whole watcher into the TL.** The sensor and the state machine have
genuinely different lifetimes and failure modes. The split is
`watcher = sensor`, `TL = state machine + policy + coordinator`.

**Leave the four-layer gate documented as-is.** Only three conditions are
provably necessary. Documenting four implies an authority model the code does
not have, and it is what produced the split brain in the first place.
