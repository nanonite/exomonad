# Launch-capable effect contract

Status: authoritative for the controller transport and the controller-side
reconciliation of a timed-out blocked-leaf resume.

## Purpose

`resume_blocked_leaf` (and every `spawn_*` effect) mutates durable host state
and can launch an agent process. The host has no request deadline: it queues the
call behind its own work and then runs a handler that performs a route
transition, a preflight, a worktree/tmux launch, and durable launch rows. An
observed live launch queued for ~24 seconds and its handler exceeded 60 seconds.
A client disconnect therefore says nothing about whether the effect happened.

Two rules follow:

1. A launch-capable effect gets a transport budget that covers queueing plus the
   host handler, not the 10 second interactive default.
2. If the client still times out, the journal entry stays `unknown` and is
   reconciled from the host's durable ledger by exact `dispatch_intent_id`. It
   is never relaunched because evidence is absent or ambiguous.

## Timeout policy

| Effect | Budget | Why |
| --- | --- | --- |
| `resume_blocked_leaf` | `LAUNCH_CAPABLE_TIMEOUT_SECONDS` (180s) | route transition + preflight + launch + readiness + launch rows |
| `spawn_leaf`, `spawn_worker`, `spawn_reviewer` | 180s | clone/worktree creation and process launch |
| every other tool | `TransportClient(timeout=...)` (10s default) | ordinary read or bookkeeping call |

`tl_loop/client/transport.py` owns the policy (`LAUNCH_CAPABLE_TOOLS`,
`LAUNCH_CAPABLE_TIMEOUT_SECONDS`, `TransportClient.effect_timeout`). The
constructor accepts `launch_timeout=...` so the budget is tunable without
changing effect code. `tl_transport_timeout_seconds` configures the non-launch
default; the launch budget is a fixed host-contract constant with headroom over
the observed live latency.

## Reconciliation

Controller: `tl_loop/loop/launch_reconciliation.py`
(`reconcile_unknown_blocked_resume`) reads the immutable ledger and the owner
registry; `tl_loop/loop/launch_evidence.py` owns the ledger-row scoping and
evidence predicates it uses. Driver hook: `_reconcile_unknown_launch_actions` in
`tl_loop/loop/driver.py`, run on startup, before every resume attempt, and on
every idle heartbeat tick. It holds the run-scoped
`launch-reconciliation.lock`, so concurrent controller ticks serialize.

`spawn_leaf`/`spawn_worker`/`spawn_reviewer` already have a dispatch lifecycle
that confirms a launch from `agent.spawned` by intent and holds ambiguous
outcomes. This reconciliation covers the blocked-leaf resume, whose action
journal is the only durable record of the request.

The request identity is the journaled `arguments.dispatch_intent_id`. Evidence
must also be newer than `arguments.dispatch_ledger_floor` and, when present,
carry the same `run_id` as the authoritative ledger run.

| Durable host row | Required binding | Decision |
| --- | --- | --- |
| `agent.spawned` | `data.intent_id` equals the journal intent; caller (`agent_id`) is not the owner; `data.child_agent` is the journaled owner; `data.branch` is the journaled branch; `data.spawn_type == "resume_blocked"`; `data.invocation_id` is the owner registry successor whose `prior_invocation_id`, `generation + 1`, `trigger == "resume_blocked"`, branch, worktree, `dispatch_intent` file, and routing ownership all match the journal | `adopt`: the journal entry becomes `confirmed` with the host invocation; the ordinary adoption path checkpoints it and no request is sent |
| `tool.called` with `tool_name == "resume_blocked_leaf"`, `success == false`, `cancelled == false`, and `error` in `PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS` | the row's recorded `arguments` reproduce the journaled predecessor, branch, worktree fingerprint, issue, and `role_route.dispatch_intent_id` / `scope_id`; caller is not the owner; the journal carries a versioned route configuration receipt | `reject`: the entry becomes `rejected` with the host error envelope, which the shared #1590 classifier (`is_proven_prelaunch_role_route_refusal`) recognizes so the newly accepted route may retry |
| no matching row | - | `hold`; the diagnostic names the intent, waited time, searched event types, and the bounded window |
| `agent.spawn_failed` only | - | `hold`; a post-journal terminal failure is not the shared prelaunch refusal |
| more than one launch row, more than one refusal row, or both a launch and a refusal | - | `hold`; conflicting evidence never resolves |
| launch row for a different owner, predecessor, invocation, branch, or intent | - | `hold`; a forged or stale intent is never adopted |

Holding records `launch_reconciliation` on the journal entry (outcome, intent,
diagnostic, evidence labels, evaluation count, timestamp) through
`EffectJournal.resolve_by_key`. Journals and ledger segments are never
hand-edited. Every subsequent tick re-evaluates the entry; the entry is not a
permanent stall. The bounded window (`HOST_EVIDENCE_BOUNDED_WINDOW_SECONDS`,
300s) only changes the diagnostic text: absence of evidence never authorizes a
relaunch.

## Third outcome: a held child effect parks, never fails the root

The two evidence outcomes above decide what the *child controller* does with
its own journal. A child may still be left with one ``unknown`` launch effect
that the host has no row for after every tick. That hold is correct and stays
correct: the child must not relaunch on absence of evidence.

The parent controller used to translate that hold into
``child action journal contains an unreconciled effect`` and then into
``recursive child failed``, which exited the whole root controller and left no
supported resolution. That was the defect fixed by #1595. The third outcome is
now explicit:

* the affected child/slice is **parked** with a durable operator gate, not a
  controller-fatal failure;
* sibling children and independent slices keep their own progress;
* a nested sub-TL parks only its own level, and reports a park upward rather
  than a failure.

The recursive rule is: **a child's fail-closed hold is reported upward as
parked-with-gate, never as `recursive child failed`.** Only the child that owns
the unresolved effect stops.

The parked gate pair is scoped to the child, intent and action
(`tl-held-launch-effect-<child>-<intent-prefix>-abandon` and `...-retry`).
The parked diagnostic names the slice, intent, journal index, last host
evidence seen, and the available operator actions. Both gates are opened
pending once and an answered gate is never re-armed.

An approved answer is only consumed when the same machine proof still holds:

| Answer | Required proof | Effect |
| --- | --- | --- |
| abandon (`...-abandon` approved) | ledger read succeeded with no launch/refusal/failure row for the exact intent; owner invocation is terminal (not live) with no successor; owner, predecessor, branch, worktree and intent all match the journal | append one linked `operator_abandoned` resolution; the slice stays parked until retry is approved; no launch |
| retry (`...-retry` approved) | the abandon proof above | append `operator_abandoned` and `retry_authorized`, then let the ordinary approved blocked-leaf dispatch path make exactly one fresh launch under the current configuration |
| reject | - | record the rejection; the slice stays parked and nothing launches |

Resolutions are append-only records (`kind: launch_resolution`) linked to the
original entry by `resolves_key`. The original `unknown` entry keeps its bytes
and status, and the journal's `pending_entries()` stops reporting it once the
resolution exists. Appends are idempotent per original effect and outcome and
run under the existing `launch-reconciliation.lock`, so repeated
`init --continue`, restarts and concurrent ticks resolve exactly once.

Every other case stays non-overridable even with an approved gate: a live owner
invocation, an `agent.spawned` row for the intent, conflicting or duplicate
evidence, a wrong owner/predecessor/worktree, a forged intent, an unapproved or
foreign gate, and every non-launch effect. Those keep the distinct fail-closed
diagnostic and never reach a launch.

## Shared prelaunch refusal definition

The refusal error set is defined once in
`tl_loop/loop/prelaunch_refusal.py` and imported by both the controller driver
and the launch reconciler. It is not copied. The host half lives in
`rust/exomonad-core/src/handlers/dispatch_authorization.rs`; the cross-language
test in `tl_loop/tests/test_driver.py` fails if the two sets drift. See
`docs/dispatch-prelaunch-refusal-contract.md`.

## Acceptance evidence

| Criterion | Test |
| --- | --- |
| A. slow host completes; adopt once, no second request | `tl_loop/tests/test_driver.py::test_startup_recovery_resumes_approved_blocked_leaf_through_controller[True]`, `test_terminal_provider_recovery_adopts_response_lost_successor_from_host_evidence`, `test_launch_reconciliation.py::test_host_completed_launch_is_adopted_without_a_second_request` |
| B. prelaunch refusal -> rejected, retry allowed | `test_launch_reconciliation.py::test_host_prelaunch_refusal_resolves_rejected_with_the_shared_contract` plus the #1590 retry tests |
| C. no evidence -> held, re-evaluated, diagnostic | `test_launch_reconciliation.py::test_no_host_evidence_holds_then_is_reevaluated_and_adopts`, `test_no_evidence_after_the_bounded_window_stays_held` |
| D. conflicting/duplicate/wrong owner/forged intent -> held | `test_launch_reconciliation.py::test_conflicting_or_duplicate_evidence_stays_held`, `test_wrong_owner_predecessor_or_forged_intent_stays_held`, `test_refusal_with_a_wrong_owner_binding_stays_held`, `test_evidence_before_the_dispatch_floor_is_ignored` |
| E. restart while unknown | `test_launch_reconciliation.py::test_restart_while_unknown_adopts_exactly_once` |
| F. concurrent ticks | `test_launch_reconciliation.py::test_concurrent_reconciliation_ticks_adopt_exactly_once` |
| G. slow host over the production transport | `tl_loop/tests/test_transport.py::TransportTest::test_slow_launch_tool_outlives_the_default_timeout` |
| H. held unknown child effect parks, root does not exit | `tl_loop/tests/test_held_launch_effect_resolution.py::test_a_held_unknown_effect_parks_the_child_without_failing_the_root` |
| I. abandon, retry, reject and the non-overridable negatives | `test_held_launch_effect_resolution.py::test_a_retry_gate_appends_one_abandon_and_one_authorized_retry`, `...::test_abandon_without_retry_stays_parked_and_launches_nothing`, `...::test_a_rejected_retry_gate_keeps_the_slice_parked`, `...::test_negative_owner_proofs_stay_non_overridable`, `...::test_host_evidence_that_is_present_or_conflicting_stays_non_overridable` |
| J. restart and concurrent ticks resolve once | `...::test_repeated_continue_and_repeated_resolution_are_idempotent`, `...::test_concurrent_ticks_resolve_exactly_once` |
