# TL recovery backlog reconciliation

Date: 2026-09-03

Status: Active review

Issue: #1058

Parent: #1047

## Purpose

This record is the backlog and operator companion to the recursive
orchestration ADR. It reconciles the earlier event-driven merge and review
issues with the delivered recursive contract and records which evidence is
still independent work.

The tracker is authoritative for issue status. Closing or superseding an issue
never deletes its description, findings, or comments. User-visible
implementation entries already recorded in CHANGELOG.md are not duplicated
for bookkeeping-only backlog cleanup.

## Accepted architecture

The following invariants are now the single contract for root and non-root
controllers:

- TLPlanning -> TLRunning -> TLAllMerged -> TLFinalizing is the canonical
  scope path. Root finalization ends at TLDone; non-root finalization ends at
  TLPRFiled.
- The immutable recursive plan_manifest in run.json contains the complete
  scope/node tree, direct-parent coordinates, stable order groups, and nested
  manifest data. It is the continuation authority after the initial plan is
  loaded.
- TLRunning derives runnable and remaining work from the manifest and typed
  child records. Direct workers/leaves are parallel to the current numeric
  sub-TL order; numeric orders are barriers, and same-order integration is
  stable by child ID.
- A PR child remains active through
  REMOTE_MERGE_ADOPTED -> PARENT_BRANCH_SYNCED -> ISSUE_CLOSE_CONFIRMED ->
  CHANGELOG_COMMITTED -> PARENT_PUSH_PENDING -> COMPLETE. A stage cannot be
  released from remote merge alone.
- A repository lane is keyed by repository and parent branch. Its epoch,
  child, push intent, journal, expected base, pushed commit, remote head, and
  ancestry receipt must correlate before release. Unknown effects require
  reconciliation or an explicit operator decision; they are never
  automatically redispatched.
- Live watcher observations and ledger replay construct the same typed reducer
  events. Exact-head review evidence, authenticated shared reviewer-account
  proof, self-approval rejection, publication ownership, and mandatory merge
  compare-and-swap remain final gates.

The executable sources for these rules are
tl_loop/fsm/orchestration.py, tl_loop/fsm/scope.py,
tl_loop/fsm/post_merge.py, tl_loop/fsm/lane.py,
tl_loop/state/plan_manifest.py, tl_loop/state/store.py, and
tl_loop/state/slice_transition.py. The production driver and the read/replay
models are adapters over those authorities, not alternate state machines.

## Disposition matrix

| Issue | Disposition | Evidence and remaining action |
| --- | --- | --- |
| #1047 | Open — retain | The recursive epic remains open for final human review of this reconciliation record. |
| #1048 | Accepted — closed | Contract and reducer structure were accepted; the final module split and rebuilt-push identity guard are recorded in its tracker notes. |
| #1049 | Accepted — closed | Immutable recursive manifests, nested declarations, additive revisions, bidirectional bindings, and null-manifest migration are recorded in its tracker notes. |
| #1050 | Accepted — closed | Root/non-root Mealy routing, durable finalization, production non-root handoff, and replay idempotence are recorded in its tracker notes. |
| #1051 | Accepted — closed | Per-slice post-merge effects, checkpoint boundaries, remote-advance rebuild, fresh effect identities, and recovered commit evidence are recorded in its tracker notes. |
| #1052 | Accepted — closed | Scoped order scheduling, direct-parent aggregate ownership, required aggregate evidence, and mock recursive integration are recorded in its tracker notes. |
| #1053 | Accepted — closed | Terminal no-op continuation, live/replay reducer equivalence, crash-window probing, and recursive resume coverage are recorded in its tracker notes. |
| #1054 | Accepted — closed | Durable repository lanes, unknown-merge recovery, journal-less abandonment, and collision-free occurrence gates are recorded in its tracker notes. |
| #1055 | Accepted — closed | Body-free hierarchical diagnostics and consistent top-level/scope guidance are recorded in its tracker notes. |
| #1056 | Accepted — closed | Exhaustive recursive, slice, lane, integration, replay, and phase-valid migration coverage are recorded in its tracker notes. |
| #1057 | Accepted — closed | The tracker records the final recursive crash-convergence task closed at 139e200c; its harness and acceptance evidence remain under tests/e2e/recursive-crash-convergence/. |
| #975 | Superseded implementation — retain for review | Its event-driven merge objective is distributed across #1048–#1057. Preserve its original defect record and any independent acceptance work; no second merge architecture should be implemented under this issue. |
| #984 | Superseded implementation — retain for review | Its merge-only real-server acceptance is covered by the broader #1057 recursive crash/convergence harness. Its historical timeout and direct-leaf findings remain in the issue comments and are not erased. |
| #1039 | Satisfied by #1046 and #1057 — retain for review | Exact-head evidence, reviewer attribution, WASM preservation, final merge gates, and durable bookkeeping are covered by the revalidation path and final convergence task. |
| #1046 | Satisfied by #1057 — retain for review | Durable review revalidation, superseding-review ordering, fail-closed freshness, merged adoption, and migration recovery are covered by the closed convergence task. |
| #1040 | Superseded by #1046/#1057 — retain for review | Shared reviewer-account authorization and snapshot recovery are part of the final review/revalidation and convergence contract; the original issue remains available as defect history. |
| #1041 | Superseded by #1050/#1053 — retain for review | The single slice_transition reducer and live/replay routing are now part of the canonical implementation; the original issue remains available as defect history. |
| #1043 | Superseded by #1046 — retain for review | Its proposed ordinary-event stale-verdict replay is intentionally replaced by the explicit RevalidateReview transition; its original design record is preserved. |
| #1027 | Satisfied by #1039/#1057 — retain for review | Durable watcher author attribution and the authenticated reviewer path are included in the final exact-head acceptance chain; the original issue remains available as defect history. |
| #1029 | Satisfied by #1039/#1057 — retain for review | Forgejo review deserialization compatibility is covered by the corrected transport and final acceptance path; the original issue remains available as defect history. |
| #1026 | Retain as independent | Its broader event-alphabet migration is not required to invent or alter the recursive contract; keep any remaining migration work scoped to that issue. |
| #989 | Retain as independent | The earlier restart epic has additional historical acceptance dependencies outside the recursive contract; do not claim #1057 closes them wholesale. |
| #1006 | Retain as independent | Its single-path review documentation and acceptance graph remain a separate backlog item unless its own dependencies are proven. |
| #952, #962, #907, #901 | Retain as independent | These older recovery/real-server matrices have distinct fixtures or runtime boundaries. #1057 does not erase their evidence or silently close their remaining acceptance work. |

The retain entries are deliberately not treated as blockers for the completed
recursive implementation. They remain visible because their own acceptance
criteria are not identical to #1057’s. Any future closure must cite its own
run evidence.

## Operator continuation contract

1. On a new run, the controller canonicalizes .exo/tl-loop/plan.json into
   run.json.plan_manifest. On continuation, omit the external plan when
   possible; the controller reconstructs the complete recursive WorkPlan
   from the persisted manifest before scheduling.
2. Do not restore, hand-edit, or delete the external plan to change a run.
   Missing external plan data does not replace the manifest. A changed plan
   without a strictly higher explicit revision fails closed. Revisions are
   additive-only and may not mutate dispatched ownership, parent scope, branch
   targets, or completed history.
3. Use status or /control to inspect scope_path, manifest_revision,
   current_order, typed child lifecycle, post-merge phase, lane identity,
   cursor, journal state, and next_transition. These are read-only
   projections and may be stale; check the cursor before answering a gate.
4. Use the named gate command or /control gate route for an existing operator
   decision. Never repair run.json by hand. A merge recovery gate is
   occurrence-scoped by canonical digest, so an approval cannot carry to a
   later intent or lane epoch.
5. A confirmed remote merge is adopted before review freshness checks, but it
   does not release the child or stage until the correlated post-merge push
   receipt proves remote bookkeeping. A compare-guard failure or unknown
   result parks/reconciles the exact occurrence; it never authorizes a blind
   retry.

## Verification record

The implementation chain was verified with the repository’s canonical targets,
including just tl-loop-test, just tl-loop-lint, just proto-check, Ruff
formatting, and git diff --check. The final recursive acceptance artifacts and
machine-readable harness checks live under
tests/e2e/recursive-crash-convergence/; unavailable external Forgejo or
captured-workspace environments must be reported as unavailable rather than
replaced by a mock claim.
