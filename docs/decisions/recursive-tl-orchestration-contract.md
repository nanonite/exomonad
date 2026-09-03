# ADR: Recursive TL orchestration contract

Date: 2026-08-29

Status: Accepted

Issue: #1048

Parent: #1047

Last reconciled: 2026-09-03 in #1058

## ANTI-PATTERNS

- Do not implement a second orchestration algebra in a scheduler, watcher, or
  effect client. The recursive scope and its transitions are one contract.
- Do not flatten a recursive plan into a global DAG. Order is relative to one
  parent and nested plans establish a new order scope.
- Do not treat numeric order as merge priority. An order is a dependency
  barrier; same-order children form one parallel block.
- Do not let a child TL merge, review, revalidate, or repair its aggregate PR.
  Those responsibilities belong to the direct parent that owns the target
  branch.
- Do not release a stage when a remote merge is merely observed. Release
  requires the durable post-merge bookkeeping push to the parent branch.
- Do not retain `TLDispatching`, `TLMerging`, or `TLWaiting` as canonical
  top-level phases. They are legacy vocabulary, not a second source of truth
  for the ordered plan. `TLPRFiled` is the typed non-root terminal handoff.
- Do not acknowledge a ledger event by mutating a state field outside the
  transition table. Live observations and replayed observations must select the
  same typed event and the same reducer arm.
- Do not infer recovery from a missing notification or redispatch an external
  effect without its durable intent and effect-journal guard.

## READ FIRST

The implementation work in later subissues must begin with these sources:

- `README.md` — ordered recursive plan rules and compatibility behavior.
- `docs/guides/programming-the-tl.md` — recursive branches, aggregate PR
  ownership, integration lifecycle, and recovery vocabulary.
- `tl_loop/loop/driver.py` — `WorkPlan`, `SubTLTask`, stage normalization,
  branch/worktree derivation, and current runtime mutation sites.
- `tl_loop/state/schema.py` — `RunState`, `SliceStatus`, `ParkCause`, review
  evidence, waiting-set, and durable version fields.
- `tl_loop/state/slice_transition.py` — slice event reducers and their guards.
- `tl_loop/fsm/event.py`, `tl_loop/fsm/phase.py`, and
  `tl_loop/fsm/transition.py` — the existing pure TL phase alphabet and table.
- `tl_loop/fsm/terminal.py` — waiting and terminal phase predicates.
- `tl_loop/loop/fsm.py` — the Mealy `step(state, event)` boundary and watcher
  or heartbeat observation events.
- `tl_loop/ordered.py` — ordered integration lifecycle, evidence objects, and
  transition table.
- `tl_loop/tests/test_fsm.py`, `tl_loop/tests/test_mealy_fsm.py`,
  `tl_loop/tests/test_ordered_contracts.py`, and
  `tl_loop/tests/test_slice_transition.py` — executable compatibility and
  guard coverage.
- `tests/e2e/ordered-recursive/` — real-server transport and recursive fixture
  conventions.

## Context

The controller currently has useful pieces of the model, but they are exposed
through several boundaries: `WorkPlan` describes recursive children,
`tl_loop.fsm` describes TL phases, `slice_transition` owns slice mutations,
and `ordered.py` describes aggregate integration. The driver still contains
coordination logic that can make these boundaries look like independent state
machines.

Epic #1047 restores recursive, replayable orchestration. This ADR freezes the
meaning of a scope, the owner of each responsibility, and the durable evidence
required by each transition. #1049 and later issues may change representation
or migrate checkpoints, but they must not change these semantics.

## REQUIRED MODEL

### Recursive scope algebra

One TL owns exactly one orchestration scope:

```text
TL(scope, parent coordinates, plan, branch, checkpoint)
  = Sequence(
      Order(1) = Parallel(direct children),
      Order(2) = Parallel(direct children),
      ...,
      Order(N) = Parallel(direct children)
    )
```

The direct children of one plan are grouped by their positive integer `order`.
All children in one group are eligible together and are bounded by the
controller's parallelism ceiling. The next group is not eligible until every
child in the current group has reached `PostMergeComplete`, including the
confirmed bookkeeping push into the parent's branch.

The order scope resets at every nested `plan` boundary. A child named
`build` in two different scopes is therefore identified by its path, such as
`root.build` and `root.integration.build`, not by a global node ID. A plan with
no explicit orders retains the legacy single-stage behavior at that scope:
all direct sub-TLs are order 1. Once ordered mode is selected, every direct
sibling declares an order and the values are contiguous from 1.

The lexicographic child/sub-TL ID order within an order group is the stable
integration order. It is deterministic metadata, independent of plan source
order and review arrival, and it is not a priority: no child in the same
parallel block is preferred for dispatch, review, or merge.

### Coordinates and ownership

| Object | Durable identity | Branch/worktree | Owns | Does not own |
| --- | --- | --- | --- | --- |
| Root TL | root run ID and root scope path | repository root branch/worktree | root plan, direct-child aggregate review/revalidation/merge/post-merge, stage barriers, root-branch finalization | an unrelated scope; a root “final PR” |
| Non-root TL | recursive scope path, run ID, direct parent run ID | `{parent-branch}.{child-name}` and child worktree | its direct plan, nested child integration, and aggregate publication handoff | review, revalidation, merge, or post-merge recovery of its own aggregate PR; those belong to its direct parent |
| Worker slice | slice ID, invocation, attempt | worker execution boundary | bounded ephemeral work | a PR or parent integration |
| Leaf slice | slice ID, invocation, attempt, PR/head | child branch/worktree under its owning scope | implementation and publication evidence | aggregate integration |
| Sub-TL slice | recursive path, child run, aggregate PR/head | child branch/worktree under its direct parent | durable child handoff and scope result | merging its aggregate PR |
| Direct parent | parent run and parent branch | direct parent branch | aggregate PR review, exact-head revalidation, compare-guarded merge, post-merge recovery | unrelated repository lanes |
| Repository lane | parent branch plus lane epoch | one parent branch/worktree | serialized integration and bookkeeping writes | parallel writes to that branch |

The root is the direct parent of its direct sub-TLs. It therefore owns their
aggregate PR review, exact-head revalidation, merge, and post-merge recovery
just as any other direct parent does. Root finalization is instead the
root-branch/local-checkout operation; the root does not create an “own final
PR” as a substitute for that operation. A non-root TL publishes its aggregate
PR to its direct parent and stops at a durable handoff; its parent continues
the aggregate lifecycle.

Child branches are derived only from their direct parent branch and name. An
aggregate PR always targets the direct parent branch. A nested child never
targets the root branch merely because it can see that branch.

### Canonical typed event union

The implementation may retain wire-specific event types during migration, but
each production input maps to one constructor in this canonical union before
state is changed:

```text
TLOrchestrationEvent =
    PlanLoaded { scope_path, plan_digest, direct_children, orders }
  | StageReleased { order, child_ids }
  | ChildDispatchRequested { child_id, invocation_id, attempt, intent_id }
  | ChildSpawned { child_id, invocation_id, attempt, branch, worktree }
  | ChildTerminal { child_id, outcome, evidence }
  | PublicationFiled { child_id, pr_number, head_sha, base_branch, digest }
  | ReviewObserved { child_id, review_id, verdict, head_sha, evidence }
  | CIObserved { child_id, head_sha, status, evidence }
  | BaseInvalidated { child_id, expected_base, observed_base }
  | IntegrationValidated { child_id, base_sha, head_sha, tree_sha, ci }
  | MergeRequested { child_id, pr_number, expected_head_sha, intent_id }
  | MergeAdopted { child_id, pr_number, head_sha, journal_id }
  | PostMergeObserved { child_id, checkpoint }
  | PostMergeComplete { child_id, merge_journal, push_intent, bookkeeping_commit, push_receipt }
  | RepairRequested { child_id, reason, findings, attempt }
  | ParkRequested { child_id, cause, diagnostic, resume_condition }
  | RecoveryObserved { child_id, journal_id, disposition }
  | Heartbeat { observed_at }
```

Every constructor carries the scope path and child ID needed to reject an
observation from a sibling scope. Publication, review, CI, integration, and
merge events also carry exact PR/head/base identity. External effects carry a
durable `intent_id`; recovery events carry the effect-journal identity.

The current event mapping is:

| Current producer | Canonical constructor | Reducer boundary |
| --- | --- | --- |
| `tl_loop/fsm/event.py` lifecycle events | `ChildSpawned`, `ChildTerminal`, `PublicationFiled`, or `MergeAdopted` as applicable | `transition` in `tl_loop/fsm/orchestration.py`; legacy adapter only until #1050 |
| `tl_loop/loop/fsm.py` watcher observations | `ReviewObserved`, `CIObserved`, `PublicationFiled`, or `Heartbeat` | the same `transition`/`step` reducer boundary |
| `tl_loop/loop/fsm.py` heartbeat | `Heartbeat` | the same orchestration reducer boundary |
| `tl_loop/state/slice_transition.py` slice events | corresponding child, review, merge, repair, park, or recovery constructor | the owning reducer, with orchestration events delegated to `transition` |
| `tl_loop/ordered.py` `IntegrationTransition` | `StageReleased`, `IntegrationValidated`, `MergeRequested`, `MergeAdopted`, or recovery constructor | canonical orchestration transition; adapter removed by #1050/#1054 |
| ledger replay | the same canonical constructor selected by live projection | the same reducer arm and event payload as live input |

The mapping is semantic, not a second interpretation of the wire payload. A
replayed watcher snapshot and a live watcher event with the same evidence must
construct the same typed event and produce the same transition and durable
state. In particular, both review paths use `ReviewObserved`; they cannot carry
separate replay-only or live-only authorization logic.

## Transition contracts

### TL phase contract

The canonical top-level reducer is `tl_loop/fsm/orchestration.py` and mirrors
the pure `tl_loop/fsm/transition.py` style: typed phase values, typed events,
and one `transition(state, event) -> state` function. Its common path is:

```text
TLPlanning -> TLRunning -> TLAllMerged -> TLFinalizing
                                      -> TLDone       (root)
                                      -> TLPRFiled    (non-root)
```

`TLFailed` and `TLParked` are terminal outcomes. `TLDispatching`, `TLMerging`,
and `TLWaiting` are legacy storage/adapter vocabulary and are not canonical
top-level phases. `TLPRFiled` is canonical for a completed non-root aggregate
publication handoff. No new production transition may construct the three
legacy phases.

The target meaning and guarded successors are:

| Constructor | Invariant | Legal successors | Durable payload | Owner |
| --- | --- | --- | --- | --- |
| `TLPlanning` | recursive manifest is loaded; no active order is released | `StageReleased(order=1)` for the first ordered sub-TL stage (while direct work remains parallel), or `StageReleased(order=0)` when no ordered stage exists -> `TLRunning`; an empty plan's guarded `StageReleased(order=0, child_ids=())` -> `TLAllMerged`; `FailureRecorded`/`ParkRequested` -> terminal | scope path, immutable plan digest, typed direct-child records, parallel worker/leaf IDs, ordered sub-TL groups | owning TL |
| `TLRunning` | current order, typed active/completed children, and all durable evidence remain tracked; numeric progression is internal | worker completion only for a worker; the guarded post-merge sequence advances PR children; failure/park -> terminal | current order, pending/completed `ChildRecord`s, post-merge states, dispatch intents, review/merge evidence, lane bindings | owning TL and direct-parent lane |
| `TLAllMerged` | every direct child satisfied its own completion contract; remote merge alone is insufficient | `FinalizationRequested` -> `TLFinalizing`; failure/park -> terminal | scope path, immutable plan digest, per-child completion confirmations and evidence | owning TL |
| `TLFinalizing` | root finalizes its root branch/local checkout; non-root finalizes aggregate publication/handoff | root matching `FinalizationComplete` -> `TLDone`; non-root matching event -> `TLPRFiled`; failure/park -> terminal | role-specific finalization and handoff evidence | root TL or non-root publication boundary |
| `TLDone` | scope finalization and required bookkeeping are complete | none | finalization evidence | owning TL |
| `TLPRFiled` | non-root aggregate PR and handoff are durably published | none; its direct parent owns the aggregate lifecycle | aggregate PR, head, base, parent branch, handoff, scope path, plan digest | non-root publication boundary |
| `TLFailed` | no automatic successor is safe without an explicit recovery decision | none; operator recovery is a new guarded event | failure, cause, last evidence, journal IDs | owning TL plus operator |
| `TLParked` | a durable external or human gate prevents progression | none; operator recovery is a new guarded event | park cause, diagnostic, resume condition | owning TL plus operator |

`TLRunning` contains the ordered barrier: same-order children may complete in
parallel, but the next numeric order is not released until every current child
reaches `PostMergeComplete`. Direct workers and leaves remain in an independent
`parallel_pending` set; they may finish before, during, or after numeric order
1, and only finalization waits for both sets. `MergeAdopted` and
`PARENT_BRANCH_SYNCED` are intermediate observations and cannot remove a child
from the active barrier. Terminal phases are closed to automatic successors.

### Root and non-root terminal contract

The phase data type is shared, but terminal interpretation is scoped:

| Scope | Terminal condition | Required handoff |
| --- | --- | --- |
| Root | all direct worker/leaf work, ordered stages, and root-branch/local-checkout finalization are complete | none; emit `TLDone` |
| Non-root | all direct worker/leaf work and ordered children are integrated enough to publish one aggregate PR and its publication handoff is durable | report PR/head/base and scope evidence to the direct parent; that direct parent owns review, revalidation, merge, and post-merge recovery of the aggregate PR |

`TLAllMerged` means all direct plan work has completed its own completion
contract (worker result or PR post-merge completion);
it is not a per-order remote-merge barrier and it does not return to
dispatching. Numeric-order progression belongs inside `TLRunning`. A parent
keeps a child scope active until its `PostMergeComplete` event (or, for a
non-root child, until its own aggregate publication handoff is durably
complete).

### Direct child and slice contract

The logical slice lifecycle is:

```text
Pending -> Ready -> Dispatching -> Spawned -> InReview
        -> Repairing (zero or more bounded rounds)
        -> MergeEligible -> MergeRequested -> RemoteMerged
        -> PostMerge -> PostMergeComplete
```

| State | Invariant | Legal successor | Durable payload | Owner |
| --- | --- | --- | --- | --- |
| `Pending` | known slice, not eligible for this stage | `Ready` or `Blocked` | scope path, dependencies, plan digest | owning TL |
| `Ready` | dependencies and budget permit dispatch | `Dispatching` or `Blocked` | order, eligibility observation | owning TL |
| `Dispatching` | one dispatch intent is durable and unresolved | `Spawned`, `Failed`, or recovery | intent, invocation, attempt, harness | owning TL |
| `Spawned` | invocation is correlated to the slice | `InReview`, `Failed`, or `Blocked` | invocation, branch/worktree, handoff | owning TL |
| `InReview` | exact publication exists and review/CI gates are active | `Repairing`, `MergeEligible`, or `Blocked` | PR/head/base, review IDs, CI evidence | direct parent for aggregate; owning TL for leaf |
| `Repairing` | a bounded repair intent is active | `InReview`, `Failed`, or `Blocked` | findings, repair attempt, intent/journal | owner of the failing review |
| `MergeEligible` | all exact-head, ownership, review, CI, and base gates pass | `MergeRequested` or `Blocked` | expected head/base, gate evidence | direct parent |
| `MergeRequested` | compare-guarded merge intent is durable | `RemoteMerged`, `Blocked`, or recovery | PR, expected head, intent/journal | direct parent lane |
| `RemoteMerged` | Forgejo confirms the merge or an existing merge is adopted | `PostMerge` or recovery | PR/head, merge journal, observed base | direct parent |
| `PostMerge` | bookkeeping is in progress after merge adoption | `PostMergeComplete` or recovery | issue/changelog/push intents and results | direct parent lane |
| `PostMergeComplete` | issue, changelog, and parent push are confirmed by correlated receipt evidence | `TLRunning` advances to the next order or `TLAllMerged` when no work remains | cumulative predecessor evidence plus repository, parent branch, child, lane epoch, push intent/journal/receipt, expected base, pushed commit, remote head, and ancestry proof | owning TL / direct parent |
| `Failed` | automatic progression is unsafe | explicit operator recovery only | reason, evidence, journal IDs | owning TL and operator |
| `Blocked` | a durable human/external gate prevents progress | `Ready`, `Repairing`, or `Failed` after a gate | `ParkCause`, diagnostic, resume condition | owning TL and operator |

`WorkerTask`, `LeafTask`, and `SubTLTask` all use the same lifecycle boundary,
but their completion payload and barrier semantics differ:

| Kind | Completion evidence | Next owner |
| --- | --- | --- |
| Worker | terminal worker result and invocation/attempt | owning TL |
| Leaf | exact implementation PR/head, review/CI evidence, and post-merge push | direct parent integration lane |
| Sub-TL | child scope manifest digest, aggregate PR/head, and durable handoff | direct parent review/integration lane |

Direct workers and leaves are an independent parallel set, regardless of
whether the same scope also has ordered sub-TL stages. They are not assigned a
numeric sub-TL order. When ordered sub-TLs exist, the parent may release order
1 immediately while direct work remains pending; when no ordered sub-TLs exist,
the direct set is the order-0 stage. A worker satisfies its barrier with
`WorkerCompleted` and has no publication or post-merge phase. A leaf requires
its exact PR/head review and CI gates and satisfies its barrier only with
`PostMergeComplete`. Finalization waits for both the direct set and all
ordered stages. A sub-TL satisfies its parent's barrier only after its
aggregate publication handoff is durable; its parent owns the subsequent
aggregate integration lifecycle.

The current `SliceStatus` values (`pending`, `ready`, dispatch states,
`spawned`, `in_review`, `repairing`, `merged`, `failed`, `parked`, and
`blocked`) are retained as storage compatibility tags until the later slice
and post-merge refactors introduce explicit merge-adoption and bookkeeping
states. A status of `merged` must always be accompanied by the post-merge
record; it cannot by itself release an ordered stage.

The executable `PostMergeState` is the authority for the post-merge rows
below. It is not legal to synthesize `PostMergeComplete` from a `merged`
status or to skip an intermediate effect state.

### Integration contract

`tl_loop.ordered.IntegrationLifecycle` is the authoritative lifecycle for a
parent folding a direct child's result:

```text
RUNNING
  -> CHILDREN_MERGED
  -> AGGREGATE_PR_OPEN
  -> CODE_REVIEWED
  -> READY_FOR_INTEGRATION
  -> INTEGRATION_VALIDATED
  -> MERGING
  -> MERGED
```

The failure and recovery branches are explicit: base movement enters
`NEEDS_BASE_REVALIDATION`, head evidence invalidation enters
`REPAIRING_AGGREGATE`, a real conflict enters `INTEGRATION_CONFLICT`, and
operator decisions enter `FAILED` or `PARKED`. All transitions go through
`transition_integration`; callers must not branch on lifecycle strings.

`MERGED` records remote merge adoption only. It is not the stage-release
condition until the post-merge contract below reaches `COMPLETE`.

The complete integration table is:

| Lifecycle | Legal events/successors | Required payload | Owner |
| --- | --- | --- | --- |
| `RUNNING` | `CHILDREN_MERGED` -> `CHILDREN_MERGED`; `FAILED` -> `FAILED`; `PARKED` -> `PARKED` | child outcomes, scope path, reason when terminal | direct parent |
| `CHILDREN_MERGED` | `AGGREGATE_PR_OPENED` -> `AGGREGATE_PR_OPEN`; `FAILED`; `PARKED` | child post-merge confirmations, parent branch | direct parent |
| `AGGREGATE_PR_OPEN` | `CODE_REVIEW_ACCEPTED` -> `CODE_REVIEWED`; `REPAIR_STARTED` -> `REPAIRING_AGGREGATE`; `FAILED`; `PARKED` | aggregate PR/head, owner, review evidence | direct parent |
| `CODE_REVIEWED` | `CODE_REVIEW_ACCEPTED` -> `READY_FOR_INTEGRATION`; base/head invalidation -> revalidation/repair; `REPAIR_STARTED`; `FAILED`; `PARKED` | exact review/head/base evidence | direct parent |
| `READY_FOR_INTEGRATION` | base/head invalidation; `INTEGRATION_VALIDATED`; `INTEGRATION_CONFLICT`; `FAILED`; `PARKED` | candidate, exact base/head, CI and tree evidence | direct parent lane |
| `NEEDS_BASE_REVALIDATION` | `INTEGRATION_VALIDATED`; repeated base invalidation; `INTEGRATION_CONFLICT`; `FAILED`; `PARKED` | expected/observed base and revalidation count | direct parent lane |
| `INTEGRATION_VALIDATED` | `MERGE_STARTED` -> `MERGING`; base invalidation; `INTEGRATION_CONFLICT`; `FAILED` | compare head, validated base, merge intent | direct parent lane |
| `MERGING` | `MERGED`; base invalidation; `INTEGRATION_CONFLICT`; `FAILED` | merge intent, expected head, server result/journal | direct parent lane |
| `REPAIRING_AGGREGATE` | `REPAIR_COMPLETED` -> `AGGREGATE_PR_OPEN`; head invalidation; `FAILED`; `PARKED` | repair intent, findings, new head evidence | direct parent |
| `INTEGRATION_CONFLICT` | `REPAIR_STARTED`; base invalidation; `FAILED`; `PARKED` | conflict details, base/head, recovery intent | direct parent lane |
| `MERGED` | none; post-merge FSM continues separately | confirmed merge journal and PR/head | direct parent lane |
| `FAILED` | none without operator recovery | failure reason and last valid evidence | direct parent/operator |
| `PARKED` | none without operator recovery | park cause, diagnostic, resume condition | direct parent/operator |

### Post-merge recovery contract

Post-merge is a per-slice durable FSM owned by the direct parent:

```text
RemoteMerged
  -> MergeAdopted
  -> PARENT_BRANCH_SYNCED
  -> IssueClosePending
  -> IssueCloseConfirmed
  -> ChangelogPending
  -> ChangelogCommitted
  -> ParentPushPending
  -> PostMergeComplete
```

Each step is idempotent and carries its own journal/evidence identity. Every
checkpoint retains all evidence from its predecessors; a later phase cannot be
constructed from only its local intent fields. Merge
adoption confirms the existing merge journal and records the slice as merged,
but keeps the slice and its waiting handle active in `TLRunning`. The
`PARENT_BRANCH_SYNCED` checkpoint must precede Chainlink closure and changelog
bookkeeping. Only `PostMergeComplete` atomically clears the pending action,
removes the waiting handle, and derives either the remaining `TLRunning`
barrier or `TLAllMerged`. A restart must adopt a confirmed merge and never
redispatch `merge_pr`.

| State | Invariant | Legal successor | Durable payload | Owner |
| --- | --- | --- | --- | --- |
| `RemoteMerged` | remote merge is confirmed but local adoption may be incomplete | `MergeAdopted` or `Recovery` | PR/head, merge response, journal ID | parent lane |
| `MergeAdopted` | merge journal is adopted while the active slice/barrier is retained | `PARENT_BRANCH_SYNCED` or `Recovery` | state version, merge journal, scope path, PR/head | parent lane |
| `PARENT_BRANCH_SYNCED` | parent branch contains the confirmed merge integration | `IssueClosePending` or `Recovery` | parent branch, commit SHA, expected base, lane epoch | parent lane |
| `IssueClosePending` | issue-close intent is durable | `IssueCloseConfirmed` or `Recovery` | issue ID, intent ID | parent lane |
| `IssueCloseConfirmed` | Chainlink closure is observed | `ChangelogPending` or `Recovery` | issue result, journal ID | parent lane |
| `ChangelogPending` | changelog edit/commit intent is durable | `ChangelogCommitted` or `Recovery` | commit/tree identity, intent ID | parent lane |
| `ChangelogCommitted` | local bookkeeping commit exists | `ParentPushPending` or `Recovery` | commit SHA, parent branch | parent lane |
| `ParentPushPending` | bookkeeping push intent is durable | `PostMergeComplete` or explicit rebuild/recovery | parent branch, expected base, push intent and journal | parent lane |
| `PostMergeRebuildRequested` | a compare/push failure has complete recovery evidence and the adopted merge remains an ancestor | `ChangelogPending` with a strictly newer generation and new intent | failed push result, observed remote head, new base SHA, merge-ancestor proof, prior push intent/journal, recovery reason | parent lane |
| `PostMergeComplete` | all required bookkeeping is confirmed by a correlated remote push receipt | none; releases stage | cumulative evidence plus repository, parent branch, child, lane epoch, push intent/journal/receipt, expected base, pushed commit, observed remote head, and ancestry proof | owning TL |
| `Recovery` | an effect is unknown or a bookkeeping step needs reconciliation | resume its durable step, `Parked`, or operator gate | journal state, diagnostic, gate ID | parent lane/operator |

Bookkeeping failure leaves the slice in post-merge recovery, not in a falsely
successful terminal state. The parent branch is not released to the next
ordered stage until the changelog and issue bookkeeping has been pushed and
confirmed. If a legitimate rebase changes the bookkeeping commit or expected
base, the reducer requires an explicit recovery generation/rebuild event before
new changelog and push evidence can be accepted; it never accepts an implicit
commit substitution.

### Repository-lane contract

There is one durable integration lane per repository and parent branch:

```text
Idle -> Reserved -> Integrating -> Bookkeeping -> Idle
                         \-> Parked/Recovering
```

The lane key is the pair `(repository, parent branch)`. A lane reservation
contains the scope path, owner run, lane epoch, current child ID, expected base
SHA, and intent/journal IDs. The lane serializes aggregate PR updates, exact
head revalidation, compare-and-swap merge requests, issue closure, changelog
commit, and parent-branch push. Same-order children may execute in parallel,
but their integration operations enter this lane in stable child-ID order.
`LaneReleased` requires a `PushReceipt` correlated to the child, lane epoch,
push intent, expected base, pushed commit, remote head, and ancestry proof;
non-empty commit text is not release evidence.

| Lane state | Invariant | Legal successor | Durable payload | Owner |
| --- | --- | --- | --- | --- |
| `Idle` | no child owns the branch | `Reserved` | branch/repository identity | parent TL |
| `Reserved` | one lease has exclusive lane ownership | `Integrating` or `Recovery` | scope, run, lane epoch, child ID | parent TL |
| `Integrating` | one aggregate update/revalidation/merge is active | `Bookkeeping` or `Recovery` | base/head, intent/journal, evidence | parent TL |
| `Bookkeeping` | merge adopted; issue/changelog/push sequence is active | `Idle` only with a correlated confirmed push receipt, or `Recovery` | bookkeeping intents/results and receipt evidence | parent TL |
| `Recovery` | lane effect outcome needs durable reconciliation | `Reserved`, `Parked`, or operator gate | journal state, lease epoch, diagnostic | parent TL/operator |
| `Parked` | lane cannot safely progress automatically | explicit operator recovery only | cause, blocked child, resume condition | parent TL/operator |

### Failure, park, and recovery contract

Failure is typed and durable. An automatic failure records the scope path,
child/slice ID, last valid state, evidence identity, and reason. A parked slice
records a `ParkCause` (for example `review_stuck`, `schedule_deadlock`,
`pr_head_unreachable`, `publication_ownership_unresolved`, or
`durable_write_failed`), diagnostic details, and a resumable condition.

Recovery is a new reducer event under the same ownership boundary. It may
replay authoritative evidence, resume an existing effect intent, or wait for a
human gate. It may not silently create a new external effect merely because a
notification cursor advanced. Every retry must be justified by the effect
journal and the applicable duplicate-effect guard.

| Recovery state | Invariant | Legal successor | Durable payload | Owner |
| --- | --- | --- | --- | --- |
| `FailureRecorded` | a typed failure stopped automatic progression | `Parked` or `RecoveryRequested` | last valid state, reason, evidence | owning TL |
| `Parked` | a human/external condition must change | `RecoveryRequested` or no-op | `ParkCause`, diagnostic, resume condition | owning TL/operator |
| `RecoveryRequested` | an explicit recovery event is being reduced | `EvidenceReplayed`, `EffectResumed`, or `Parked` | request ID, scope, journal IDs | owning TL |
| `EvidenceReplayed` | authoritative observations were applied without new effects | normal guarded state or `Parked` | event identity, evidence digest, version | owning TL |
| `EffectResumed` | an existing intent was explicitly reconciled/resumed | normal effect successor or `Parked` | intent/journal, operator decision | owning TL/lane |

## Atomic boundaries and guards

The following guards are part of the contract and must survive every migration:

- `_derive_slice_action`'s double-dispatch protection remains in the reducer;
  a durable dispatch attempt/reviewer attempt cannot be recreated from a stale
  observation.
- Exact PR/head binding, authenticated shared reviewer-account evidence,
  reviewer-vs-author self-approval rejection, and publication ownership remain
  mandatory for merge readiness.
- A merge request always carries the authenticated observed head as mandatory
  compare-and-swap evidence.
- A stale base or head invalidates integration evidence before merge; it does
  not get converted into a successful terminal state.
- A remote merge plus its confirmed journal entry is adopted before review
  freshness checks, and the adoption plus waiting-set/FSM update is one durable
  transition.
- Review, CI, publication, and post-merge evidence is scoped to the recursive
  child path and exact identity. A sibling event cannot advance this scope.

## Legacy phase inventory and migration boundary

`TLDispatching`, `TLMerging`, and `TLWaiting` have no canonical top-level
responsibility in this contract. They are retained only as storage
compatibility values while the production callers are migrated. `TLPRFiled` is
canonical for a non-root aggregate handoff and carries its required payload.
There is no production constructor or persisted target payload that gives either
`TLDispatching` or `TLMerging` unique responsibility, so the ADR does not
justify preserving them as target phases.

The target constructors and durable payloads live in
`tl_loop/fsm/orchestration.py`. The root and non-root drivers now route
production transitions through the target reducers. `tl_loop/fsm/transition.py`,
`tl_loop/ordered.py`, and legacy phase values remain compatibility adapters for
old checkpoints and inputs only; they must not assert or extend the canonical
phase graph. Slice, post-merge, and repository-lane recovery likewise use the
typed reducers delivered by #1051 and #1054. New code must not construct a
second legacy state machine.

## Migration boundaries for the epic

| Issue | Contracted responsibility |
| --- | --- |
| #1049 | persist the immutable recursive manifest, scope coordinates, and checkpoint migration |
| #1050 | refactor root/non-root TL FSMs around this phase and event contract |
| #1051 | separate slice integration from the post-merge recovery FSM |
| #1052 | schedule ordered recursive children and direct-parent aggregate PR integration |
| #1053 | project ledger evidence into the same reducer events as live observations |
| #1054 | implement serialized parent-branch integration/bookkeeping lanes |
| #1055 | expose scope, ownership, lane, and recovery diagnostics |
| #1056 | add exhaustive transition, replay, and migration model coverage |
| #1057 | prove recursive crash recovery and exactly-once convergence with real Forgejo |
| #1058 | reconcile superseded recovery tasks and publish the final contract |

The original #1048 scope deferred manifest persistence, production scheduling,
and real-server convergence. Those concerns were subsequently delivered by
#1049–#1057 and are reconciled in
`docs/decisions/tl-recovery-backlog-reconciliation.md`. This ADR remains the
semantic authority: implementation and acceptance evidence must satisfy it,
but a follow-up issue must not use the historical deferral as permission to
invent a competing contract.

## Verification obligations

The executable contract tests in
`tl_loop/tests/test_recursive_orchestration_contract.py` and the existing
ordered-contract tests must prove:

1. recursive order scopes reset at nested plans;
2. same-order siblings use stable child-ID order independent of source or
   review arrival, and numeric orders form barriers inside `TLRunning`;
3. direct workers and leaves remain a parallel pre-stage alongside ordered
   sub-TL groups, with worker completion distinct from PR-child completion;
4. `MergeAdopted` and `PARENT_BRANCH_SYNCED` retain the active slice until
   `PostMergeComplete`, which alone releases the stage;
5. root finalization and non-root aggregate publication use distinct evidence,
   and direct-parent branch/worktree ownership remains relative;
6. repository lanes cannot release a parent branch before a confirmed push
receipt correlated to repository, parent branch, child, epoch, intent, journal,
base, commit, remote head, and ancestry, and lane terminal/recovery edges are
explicit;
7. review and integration evidence preserves exact head, base, PR, patch, CI,
   and journal dimensions, including distinct base- versus head-invalidation
   recovery;
8. replay/live inputs construct the same typed event and reduce identically;
9. terminal phases reject every automatic event, and pure transition modules
   do not import effect clients.
10. empty plans have an explicit guarded successor, bookkeeping commit changes
    require an explicit rebuild generation, and fabricated completion evidence
    is rejected.

Run the complete TL test target after any contract change:

```bash
just tl-loop-test
```

The production event inventory above must be reviewed whenever a new event
constructor is added. The later real-server acceptance run must additionally
exercise nested scopes, repeated continuation, crash-window recovery, stale
evidence, one merge journal entry, and post-merge bookkeeping.

## DONE CRITERIA

- The recursive algebra, ownership boundaries, root/non-root terminals,
  integration/post-merge/repository-lane FSMs, failure/recovery behavior, and
  atomic handoffs are specified here without relying on driver folklore.
- The target phase/event alphabet, ordered integration lifecycle, and
  repository-lane lifecycle each have a named executable transition table or
  a named adapter into one; no wildcard terminal compatibility behavior is
  asserted as valid.
- A later subissue can implement persistence or scheduling without inventing
  whether order is parallel or sequential, who owns aggregate PRs, or when a
  stage is released.
- The contract tests pass with `just tl-loop-test`.
- Real Forgejo and captured-workspace convergence evidence belongs to #1057,
  whose tracker entry is closed. This ADR does not replace those acceptance
  artifacts or claim that an unavailable external environment was run locally.
