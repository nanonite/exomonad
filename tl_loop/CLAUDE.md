# Programmatic TL Controller

`tl_loop/` is the programmatic tech-lead controller that replaces the
interactive TL orchestration loop. It owns the controller finite state machine
and calls the Rust ExoMonad runtime over its Unix-domain socket (UDS) boundary.

This package is the shipped controller for the M8 TL-as-loop architecture.
Runtime dependencies remain standard-library-only; development tools are
declared in `pyproject.toml`. The controller is launched by the default TL
window and can also be run directly for bounded tests and replay.

All I/O stays in Rust. Python owns controller decisions and pure state/event
transitions, while Rust remains responsible for sockets, processes, files,
ledger access, agent lifecycle, and every other external effect.

The runtime creates per-run state under `.exo/tl-loop/<run_id>/`. That directory
is runtime state, not Python source, and must never be used as the package code
location.

## RLM judgment boundary

The tl_loop.rlm boundary is for bounded structured judgments only. Its
backend receives a stateless RlmRequest with tools=() and cannot be given
an effect client, agent spawner, or filesystem capability. Responses are
validated with closed-key output schemas; invalid responses retry at most three
attempts and then raise JudgmentFailed.

Each attempt is recorded with the model, input hash, token counts, latency,
attempt number, replay flag, and redacted validated result. RlmCallStore
commits the role token charge and event record together. Replay entries are
keyed by the canonical hash of the judgment name, inputs, model, and output
schema, so hermetic tests can avoid network access.

Every model choice supplies its resolved context_length. RLM reserves
floor(context_length * 0.8) using integer arithmetic. Plain input mappings
are one required section; callers that need compaction pass the explicit
sections envelope with name, content, priority, and required fields. Sections
are rendered in descending priority and optional sections are removed in
ascending priority until the deterministic prompt fits. Required overflow
raises ContextOverflow instead of truncating content. Provider token counters
may be injected; otherwise canonical JSON is counted at four UTF-8 characters
per token, and the method, final count, budget, and dropped section names are
recorded in every RLM event.

## RLM decomposition boundary

tl_loop.rlm.decompose.decompose is the only M6.3 entry point for turning a
root specification into SliceSpec records. It receives the resolved model
choice by dependency injection; the root specification cannot select a
harness, model, budget, or parallelism. The model returns only the closed
slices schema. Python rejects duplicate or overlapping ownership paths,
unknown dependencies, cycles, missing test plans, and repository-escaping
paths. Each cross-field violation is fed back with a retry ordinal so replay
does not suppress a fresh corrective attempt. Exhaustion raises
DecompositionParked with ParkCause.RETRIES_EXHAUSTED; no malformed
decomposition reaches run state.

## RLM review adjudication boundary

tl_loop.rlm.adjudicate.adjudicate_review receives the diff, comments,
criteria, and exact reviewed head through an injected model choice. The diff
and criteria remain required RLM sections, so ContextOverflow stops the call
before a compacted-away diff can be judged. The model output is closed to GO,
GO-WITH-NITS, and NO-GO with structured reasons and an echoed head. Python
loads the canonical review policy and applies minimum-round, external-path,
line-count, and complexity gates; a GO behind a gate is marked
second_review_required and is not mergeable. GO-WITH-NITS remains mergeable
and its nit reasons are stored in durable per-head `review_findings` state.

## Checkpoint and resume layout

Each run is persisted at `.exo/tl-loop/<run_id>/run.json`; the shared writer
lock is `.exo/tl-loop/run.lock`. `tl_loop.state.store.resume()` reconstructs
the FSM, slice map, budget ledger, and event-log replay offset from that file.
Resume treats the checkpoint and event log as authoritative and performs no
server or network query.

## Long-running wave goals and heartbeats

RunState.goals is optional durable metadata for a long-running wave:
objective, deadline, completion predicate, and the last heartbeat/progress
timestamps. HeartbeatConfig supplies an explicit idle interval and stall
threshold. An idle heartbeat calls only the read-side poll_workers and
watcher_pr_state effects, then persists reconciled slice state through the
shared writer. Synthetic heartbeat events are deterministic observations; they
do not consume ledger sequence numbers or charge budgets.

Dead panes remain authoritative process failures and use the existing M5.3 park
path with stall_detected, including needs-human issue creation and dependent
blocking. A live pane with no progress only emits a wave.stalled observation;
elapsed time does not park or fail the slice. The controller remains active
until an authoritative event, explicit cancellation, or an unrecoverable
integrity error resolves it. Repeated heartbeats must be safe to run because
terminal slices are no longer polled and unchanged PR observations produce no
new synthetic event.

## Ledger-backed event projection

The immutable ledger at `.exo/ledger/segments/` is the TL loop's event storage
layer. `tl_loop/events/envelope.py` is a read-only typed projection of Rust's
`LedgerEvent`; it does not create an `events.log`, compatibility log, or any
other second durable event path. The loop never writes ledger segments. Its
closed event kinds map only onto event types already present in the
observability allowlist, and absent review head SHAs remain absent for the
server-emission findings tracked by M2.7.

`tl_loop/events/reader.py` replays those projections by global `run_seq` across
lexically ordered segments and applies the ledger's supersession and sequence
status semantics. `LedgerQueue` is an in-process bounded tailer; handling is
at-least-once and a consumer acknowledges only after successful handling.
Acknowledgement persists the global `run_seq` in the run-state cursor through
the single state writer, so restart begins at `cursor + 1`. No queue or event
log file is created. A ledger `run_id` mismatch is retained as a reader finding
and surfaced in controller diagnostics rather than being reported only as
silence.

## Durable child-dispatch protocol

Every worker or leaf spawn is a durable two-boundary operation. Before calling
the external spawn effect, the controller assigns a unique `intent_id` and
checkpoints the slice as `dispatching`, then emits `tl.dispatch_intended` and
`tl.spawn_requested`. A successful tool response is only an accepted request;
the slice remains `dispatch_unconfirmed` until the correlated `agent.spawned`
ledger event carries the same `intent_id`. That event is the authoritative
transition to `spawned` and records its `run_seq` as
`dispatch_authoritative_event_seq`.

An explicit tool rejection becomes `dispatch_failed` and opens the named
`tl-dispatch-failed` gate. An accepted request with delayed evidence remains
`dispatch_unconfirmed` indefinitely; `dispatch_timeout` bounds only the
transport operation and never creates a lifecycle failure. The persisted intent
and last boundary remain visible until a matching event, verified owner
reconciliation, explicit cancellation, or human escalation resolves the slice.

On restart, `dispatching` and `dispatch_unconfirmed` slices are reconciled by
their persisted intent IDs before new effects are considered. Reconciliation
never issues a second spawn for an existing intent; it waits for matching
evidence or an explicit resolution. Controller boundary events are limited to
scalar dimensions and are written by Rust through the `tl` event allowlist.

Recursive sub-TL controllers remain supervised until their own authoritative
terminal phase. Parent joins and configured leaf/reviewer session-age
thresholds are observational only; they do not terminate an owned child or
invocation. Explicit cancellation and verified dead-process cleanup remain
destructive paths.

The durable goals/read model exposes controller start time, elapsed wall time,
task dispatch start times, the last authoritative event sequence, and the last
observed progress time. Heartbeat intervals log bounded waiting observations;
these fields and logs are telemetry only and never change lifecycle status.

## Selector budget ledger

The selector estimates a spawn before it is written to run state. The estimator
inputs are the classified difficulty, test-step count, path count, and harness
rate. The checked-in harness policy supplies the rate through cost_rank; a
rank of 1 is the baseline. The formula is:

    ceil((base(difficulty) + 50 * test_steps + 100 * paths + 50 * dependencies) * harness_rate)

The difficulty bases are 100 tokens for trivial, 500 for standard, and 1,000
for hard. Dependencies are included because they add context to a slice.
HarnessChoice.estimated_cost is the reservation charged to both the selected
role and harness. charge_spawn must run in the same atomic
tl_loop.state.write.apply mutation that records the spawn, so concurrent
selectors cannot consume the same remaining ceiling.

A child completion reconciles the reservation with authoritative usage. The
caller passes Chainlink usage first, or the harness-reported usage when
Chainlink has none. If neither source reports tokens, the charge persists
actual="unknown" and conservatively applies its estimate to spent counters; it
never claims the estimate was actual usage. A measured estimate delta is
flagged when its absolute value exceeds 20% of the estimate.

A selector result of None with SelectionFailure.OVER_BUDGET is a bounded needs-human parking signal; the controller must not widen the allowlist or silently raise a ceiling to continue.

## FSM parity fixture

`tl_loop/fsm/` is a pure port of `.exo/roles/devswarm/TLPhase.hs`. The golden
fixture is generated by the Haskell role test exporter and includes the Git blob
hash of that source file. Regenerate it after any TLPhase change with:

```bash
just tl-loop-golden
```

The Python test suite rejects a stale fixture, including when the Haskell source
changes without regeneration.

The phase-level predicates in `tl_loop/fsm/terminal.py` are the authoritative
terminal mechanism for the programmatic TL. The WASM TL role no longer carries
a coordination stop hook; its worker and reviewer lifecycle hooks remain
independent. The Python loop owns terminal decisions without copying nudge prose
or external checks for uncommitted work and missing PRs.

## RLM repair boundary

tl_loop.rlm.repair.compose_repair is the only repair handoff path for a
NO-GO review. It calls watcher_pr_state first and requires the existing PR to
be open, unmerged, and identified by both head branch and SHA. The RLM receives
the NO-GO reasons as its primary required section and returns exactly the seven
RepairHandoff sections. Python retries semantic path-boundary violations,
dispatches only through resume_pr, and increments the owning slice's attempts
once after a successful dispatch. No repair handoff creates a branch, leaf
name, or agent type.

## Recursive sub-TL ownership

`WorkPlan.sub_tls` runs a child `tl_run` directly, without `fork_wave` or a
Claude session. Each direct child has a positive sibling-scoped `order`;
children with the same order form one `OrderedStage`, and recursive children
restart their order at `1`. A child checkpoint lives at
`.exo/tl-loop/<parent_run_id>/<sub_tl_id>/run.json`; a grandchild nests below
that child directory. Parent state contains only its direct sub-TL slice and
the child terminal result.

The ordered integration contract distinguishes the child result from the
parent fold. `AggregateCandidate` binds a child PR to its head, patch digest,
and original base. `CodeReviewEvidence` is head/patch-bound, while
`IntegrationEvidence` is base/head/tree/CI-bound. The centralized integration
transition table rejects illegal lifecycle edges; base invalidation enters
`NEEDS_BASE_REVALIDATION`, while head invalidation enters aggregate repair.
Legacy sub-TL plans without `order` remain one order-1 stage.

Branches use the coordinate form `{parent}.{name}`. A child PR targets its
parent branch, recorded as the child slice `base_ref`. Run state records the
owner branch, owner worktree, parent lineage, and recursion depth. Creating a
live run that claims an already-owned worktree fails closed. `max_depth` parks
the attempted recursive slice with `schedule_deadlock` and raises
`DepthLimitExceeded`.

Ledger readers may set `scope_run_id` and `scope_agent_id`. An agent scope
includes the agent's own events and its directly spawned children, so a root
reader does not consume a grandchild review event.

## Learned dispatch policy

`tl_loop.select.learned_policy.DispatchPolicyStore` persists optional learned
dispatch data at `.exo/tl-loop/dispatch-policy.json`. A missing document is
the empty version-one policy. Mutations use the M2.2 atomic writer, snapshot
the prior revision under `.exo/tl-loop/dispatch-policy.snapshots/`, and append
a trigger to durable history. `rollback(revision)` restores the snapshot's
decomposition, preferences, and repair patterns while recording a new
rollback revision.

Learned harness preferences are validated against the human-authored
`.exo/harness_policy.toml` allowlist. The M4 selector receives a validated
policy by dependency injection and may use it only after authoritative cost
rank, capability, and budget filtering; it cannot widen an allowlist or
change any ceiling.

## Evidence-gated wave refinement

`tl_loop.harness.refine.maybe_refine` is callable only when the durable FSM
phase is `TLAllMerged`, `TLDone`, or `TLFailed`. It reads the immutable
`LedgerReader` projection (or an equivalent sequence-bearing event iterable),
refuses hard findings and partial sequence ranges, and never refines during a
live wave.

The closed triggers are repeated task-class failure, a repeated successful
tactic, repeated delegation of an allowed role, and repeated behavior policy.
The default threshold is two observations and is configurable. Every learned
entry stores the contributing `run_seq` values in the policy `evidence` map;
entries without evidence fail validation. Capability pass/fail aggregates are
stored separately with their own sequence evidence and remain bounded by the
human-authored harness allowlist.
