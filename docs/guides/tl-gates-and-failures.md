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
behind three durable gates,
`tl-held-launch-effect-<child>-<intent-prefix>-abandon`, `...-retry` and
`...-skip`, instead of exiting the root controller with
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

## Operator walkthrough: a parked or skipped stage

This is the single operator procedure for a stage whose child journal holds one
`unknown` launch-capable effect. The machine proofs are defined in
[`docs/launch-capable-effect-contract.md`](../launch-capable-effect-contract.md);
this section is what the operator sees, decides, and does next.

### Recognising a parked stage

The controller does not fail the run. It parks only that stage and logs
`human gate pending`. Read the durable status:

```bash
python3 -m tl_loop status --project-root . --run-id root
```

The status document shows:

| Field | Value at a held-launch park |
| --- | --- |
| `slices.<stage>.status` | `parked` |
| `slices.<stage>.park_cause` | `human_decision_required` |
| `park_causes` | maps that stage to `human_decision_required` |
| `gates` | three `pending` names scoped to the child and intent: `tl-held-launch-effect-<child>-<intent-prefix>-abandon`, `...-retry`, `...-skip` |
| `next_transition` | `answer_gate:<first pending gate>` |
| `current_order` | the parked stage's order |

`<intent-prefix>` is the first 12 alphanumeric characters of the child's
`dispatch_intent_id` suffix. `status` lists only each gate's name and
`pending`/`approved`/`rejected` status; the durable run state keeps the fuller
diagnostic: `park_audit.recovery_action = held_launch_effect`,
`park_audit.reason` (the intent, journal index, owner, and last host evidence),
and `park_audit.launch_evidence`, which carries the exact intent and the three
gate names. `park_audit.available_actions` is `park`, `abandon-and-skip`,
`retry`. A sibling stage and a nested sub-TL are not parked by this one.

### Deciding a parked stage

The three gates are independent durable records; answering one does not answer
another, except that abandon-and-skip answers the abandon gate and then the skip
gate. The commands below use the source-checkout form; the installed archive is
`python3 ~/.exo/tl_loop.pyz`, with the same subcommands and flags.

| Decision | Exact CLI | Slice | Journal | Dependents and next stage |
| --- | --- | --- | --- | --- |
| Park | `gate --name <gate> --reject` on `...-abandon` or `...-retry` | stays `PARKED` | nothing appended | unchanged; same order held |
| Retry | `gate --name ...-retry --approve` | reopens as `SPAWNED` | `operator_abandoned` + `retry_authorized` appended, linked to the original entry | not evaluated; the same stage launches exactly once more |
| Abandon | `gate --name ...-abandon --approve` | stays `PARKED` | one `operator_abandoned` appended | unchanged; same order held |
| Abandon-and-skip | `gate --name ...-skip --approve --reason <reason> --dependency-policy <policy> [--dependent <stage> ...]` | `SKIPPED`, `park_cause` cleared, no PR or post-merge | `operator_skipped` appended (plus `operator_abandoned` when abandon was approved first) | scope reopens at the next eligible stage; every dependent follows the policy below |

Park (rejecting either the abandon or the retry gate):

```bash
python3 -m tl_loop gate --project-root . --run-id root \
  --name tl-held-launch-effect-<child>-<intent-prefix>-retry --reject
```

Retry:

```bash
python3 -m tl_loop gate --project-root . --run-id root \
  --name tl-held-launch-effect-<child>-<intent-prefix>-retry --approve
```

Abandon:

```bash
python3 -m tl_loop gate --project-root . --run-id root \
  --name tl-held-launch-effect-<child>-<intent-prefix>-abandon --approve
```

Abandon-and-skip (a prior abandon is optional; skip alone is enough):

```bash
python3 -m tl_loop gate --project-root . --run-id root \
  --name tl-held-launch-effect-<child>-<intent-prefix>-skip --approve \
  --reason "the child record is unrecoverable; re-plan it in a fresh context" \
  --dependency-policy block
# require-named adds one or more:
#   --dependent <stage>
```

A gate stored by a direct child scope is answered with `--parent-run-id <root>`
and `--run-id <child>` instead of the top-level run id.

The CLI enforces the decision shape: a `-skip` gate accepts only `--approve`,
`--reason` must be non-empty, `--dependency-policy` is required and is one of
`proceed`, `block`, `require-named`, and `--dependent` is repeatable and only
valid with `require-named`. `--reason`, `--dependency-policy`, and `--dependent`
are refused on every non-skip gate. The rejection of an abandon or retry gate
is recorded on that gate; no journal resolution is appended.

A skip answer is consumed only while the abandon proofs still hold: the ledger
read succeeded with no launch, refusal, or failure row for the exact intent; the
owner invocation is terminal with no successor; and the owner, predecessor,
branch, worktree, and intent all match the journal. A live owner, a launch row,
duplicate or conflicting evidence, a forged intent, or a foreign or unapproved
gate stays held and consumes nothing. Approving retry and skip together is
conflicting evidence and also consumes nothing. An answer that widens the plan's
declared policy, records no policy, names no dependent under `require-named`, or
names a node that is not a dependent is refused and consumes nothing. A skip,
abandon, or retry is appended exactly once across restarts and concurrent ticks;
the original journal entry, ledger rows, proofs, and plan snapshot digest stay
byte-identical.

### Dependency policies

The stage's declared `skip_dependents` is the floor. Absence means `block`, and
an answer may only tighten it: `proceed` < `require-named` < `block`. The
effective policy is the stricter of the declaration and the answer.

| Policy | Dependents | Verified end state (plan A -> B -> C, sibling D in A's stage) |
| --- | --- | --- |
| `proceed` | every dependent runs although the prerequisite produced nothing | B, C, and D `merged`; the run reaches `TLDone` |
| `block` | every dependent is held | B and C `blocked` with `blocked_by` naming the skipped stage and a `park_audit` carrying the skip gate and policy reason; D `merged`; the run stays held in `TLRunning` at that stage, not terminal |
| `require-named` | only named dependents run; every unnamed dependent is held | naming B: B and D `merged`; C `blocked` with the "unnamed in the skip decision" reason and `blocked_by` naming the skipped stage; the run advances to C's stage and stays held in `TLRunning` |

A held dependent is never dispatched, merged, or released silently. The ordered
stage path does not dispatch a `BLOCKED` child, so a later tick cannot override
the held verdict. `tl_loop/tests/test_skip_policy_end_to_end.py` proves these
end states through the production `run_tl_loop` path;
`tl_loop/tests/test_parked_stage_skip.py` proves the decision, diagnostics, and
refusals above.

### After a skip: the post-skip workflow

For the skipped stage the run's plan is considered finished. The stage is terminal
`SKIPPED`; the controller never relaunches it and never runs its post-merge
path. The rest of the run follows the dependency policy: under `proceed` it can
reach `TLDone`; under `block` or `require-named` it holds in `TLRunning` at the
first withheld stage.

1. **The controller leaves the issue alone.** A skip is not a merge, so no
   post-merge recovery runs for it and the skipped stage's linked Chainlink
   issue stays open. A following stage's own linked issue closes independently
   when that stage merges, and an unlinked stage is unaffected. Proved by
   `test_k_a_skip_does_not_close_the_skipped_stages_linked_issue`,
   `test_k_a_skipped_stage_never_runs_the_post_merge_issue_close_path`,
   `test_k_the_next_stages_linked_issue_closes_independently`, and
   `test_k_the_live_controller_does_not_close_the_skipped_issue` in
   `tl_loop/tests/test_parked_stage_skip.py`.

2. **Handle the issue in Chainlink, by hand.** The controller has no command
   that closes, reopens, or rewrites a skipped stage's issue. Either close the
   issue with no PR (the skipped work produces none), or split the work into
   smaller sub-issues and close the original. This is an operator action in
   Chainlink, outside the controller.

3. **Plan the repair in a new `plan.json`.** Put the repair slices under
   whichever issue remains. A new run (new run id and new `plan.json`) may bind
   the still-open issue the older run's skipped stage referenced and a freshly
   created sub-issue. Issue admission has no cross-run issue-state check; the
   new run's slices carry both bindings, dispatch is admitted, and the ordinary
   post-merge close path closes the older issue under the new run. The older
   run's state, journal, and checkpoint bytes stay unchanged. Admission also
   accepts an issue the operator already closed; the close path observes
   `closed` and records `already_closed` without issuing a second close. Proved
   by `test_a_new_plan_binds_the_skipped_issue_and_a_fresh_sub_issue`,
   `test_a_plan_binding_an_already_closed_issue_is_adopted`, and
   `test_plan_validation_binds_the_same_issue_in_a_new_manifest` in
   `tl_loop/tests/test_skip_policy_end_to_end.py`.

There is no in-manifest rebind. Within one manifest an issue id is bound to one
node, so a replacement node cannot claim the skipped node's
`chainlink_issue_id`; `test_g_reusing_the_replaced_issue_binding_is_refused` in
`tl_loop/tests/test_plan_revision_supersession.py` refuses exactly that. No
command or plan field transfers the skipped stage's issue, identity, branch,
worktree, or history to a replacement. If the repair needs that issue, it goes
in a new plan as above.

#### What `plan revise` supersession is and is not for

`exomonad plan revise` can add a new future node that declares
`"supersedes": "<old stage>"`. The old declaration stays byte-for-byte; the
manifest records `superseded_nodes: {old -> new}`, the dry run reports the
replacement and the `rebound_dependents`, and apply writes that report into the
audit entry. The replaced node must already be decided (a `PENDING` or `READY`
node is refused by `superseded_node_still_pending`), and a skip is such a
decision. The replacement gets its own name, branch, and worktree and cannot
reuse the replaced node's identity.

It is for a same-run substitute slice when the run is still active and the
skipped stage's issue does not need to move. It is not a rebind: it cannot take
the skipped stage's `chainlink_issue_id`, cannot delete or rewrite the old
declaration, and does not touch Chainlink. It is also not the post-skip issue
workflow: closing or splitting the issue and binding the repair under it is the
new-plan path above. See
[`programming-the-tl.md`](programming-the-tl.md#superseding-a-dispatched-node)
for the plan-authoring mechanics and
`tl_loop/tests/test_plan_revision_supersession.py` for the proved behaviour.

The compiled acceptance for the skip path is the `skip-held-stage` case in
`tests/e2e/blocked-resume-host/`, which also proves that a real
`exomonad init --continue` restart adds no launch, no owner window, and no
second skip record.

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
[`tl_loop/CLAUDE.md`](../../tl_loop/CLAUDE.md) for the full table and the rest
of the knobs.
