# Human gates and bounded failure

Moved verbatim from the root `CLAUDE.md`.

Review disagreement, missing CI, stale heads, budget exhaustion, liveness
failure, and harness-switch requests become named durable gates or terminal
parking causes. The operator answers a gate explicitly:

```bash
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root --name <gate> --approve
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root --name <gate> --reject
```

The controller resumes from the checkpoint. It does not coax a model to keep
working, silently retry beyond a ceiling, or ask a second interactive TL to
make the decision.

An answer is never rewritten. A gate the controller re-reads on a later
`--continue` keeps the status the operator recorded: `pending` and `rejected`
leave the run gated and are never written back to `pending`. That matters most
for `tl-ordered-child-recovery-<child>`, where `--continue` relaunches an ordered
child only after the durable proof (matching plan manifest, branch, worktree,
parent ownership, accepted dispatch intent, an exit diagnostic bound to that
exact child checkpoint revision, and a retryable exit reason) holds. An
`approved` answer releases the recovery only when the exit-reason classification
is the single unproven fact: approving a question about ownership, publication,
or an unreconciled non-launch effect is not evidence for it, so those proofs stay
hard and the slice is re-decomposed instead.

A held `unknown` launch-capable effect is the third outcome. The child's own hold
is correct and stays fail-closed, but the parent parks only that child/slice
behind two durable gates, `tl-held-launch-effect-<child>-<intent>-abandon` and
`...-retry`, instead of exiting the root controller with
`recursive child failed`. Sibling children and independent slices keep running;
a nested sub-TL parks only its own level. The recursive rule is that a child's
fail-closed hold is reported upward as parked-with-gate, never as a controller
failure. Approving the retry gate consumes the answer only after re-proving that
the ledger has no launch/refusal/failure row for the exact intent and that the
journaled owner invocation is terminal with no successor and matching owner,
predecessor, branch, worktree and intent. It then appends append-only
`operator_abandoned` + `retry_authorized` records that leave the original
`unknown` entry untouched, and the ordinary approved blocked-leaf path makes
exactly one fresh launch. Abandon records only the abandonment and leaves the
slice parked; reject launches nothing. A live invocation, a launch row for the
intent, conflicting or duplicate evidence, a wrong owner/predecessor/worktree, a
forged intent, a foreign or unapproved gate, and every non-launch effect stay
non-overridable. Contract: `docs/launch-capable-effect-contract.md`.

An approved recovery whose child controller has exited relaunches that child
with a fresh invocation identity, because relaunching against the recorded one
would deliver to the pane that already exited. Branch, worktree, controller
identity, PR, head, and publication ownership are preserved, so the child is
reopened rather than duplicated.

A dispatch failure is one of those bounded failures, and it is classified by a
stable machine code rather than by a message. The code is the `code` the runtime
recorded on the correlated `agent.spawn_failed` ledger event. **Only a refusal
that proves nothing was created is retryable**: a transient code —
`worktree.branch_exists` (a creation race) or `worktree.lifecycle_lock_timeout`
(the decision never ran) — schedules a durable `dispatch_retry_scheduled`
boundary with bounded exponential backoff and is re-driven idempotently, so the
only recovery signal is never traded for a duplicate leaf, branch, worktree, or
PR. The boundary carries no intent, because nothing was created; a restart
inside the window resumes the same boundary and issues no effect. An
**ambiguous** code — currently `dispatch.transport_timeout`, which wraps the
whole server-side spawn and so can fire after the worktree, identity, and tmux
window exist — is not a failure: the slice holds at `dispatch_unconfirmed` with
its intent intact and is resolved by matching evidence or verified owner
reconciliation, never by a fresh spawn. Every other code, including one the
runtime never recorded, is terminal:
`worktree.branch_ownership_conflict` parks immediately on
`tl-dispatch-ownership-conflict-<slice>`, and exhausting `tl_dispatch_retry_limit`
(default 3) opens `tl-dispatch-failed-<slice>` keeping the machine code. Both
gates are scoped to the slice, the way `tl-ordered-child-recovery-<child>` is
scoped to its child: one exhaustion is one pending gate and one
`tl.gate_opened`, so a second parked slice cannot hide behind the first one's
answered gate, and answering one slice's gate never resolves another's. The
scope also **isolates** the failure (#1134): the exhausted slice is parked and
the run *holds* behind its open gate rather than stopping, so siblings that
refusal says nothing about still dispatch in the same pass and each one that
exhausts opens its own gate. The run still ends on the failed terminal phase
with every gate pending, so a restart re-enters the hold and re-announces
nothing; a failed phase that no open per-slice dispatch gate explains is not a
hold and still stops the dispatch pass immediately. A
checkpoint written before per-slice naming has its pending run-global
`tl-dispatch-failed` / `tl-dispatch-ownership-conflict` gate migrated onto the
single slice parked on `dispatch_failed`, preserving its pending status; an
ambiguous one fails closed and names the candidates rather than guessing. The
budget is
operator-configurable (see Configuration). Elapsed time and silence are never
evidence of failure, and abandoned durable guidance is re-queued with its
`batch_id` recorded rather than dropped. See
[tl_loop/CLAUDE.md](tl_loop/CLAUDE.md) for the full table and the rest of the
knobs.
