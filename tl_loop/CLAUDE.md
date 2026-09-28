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

When controller startup captures a plan, it passes the exact bytes to
`exomonad record-plan-snapshot`. Rust holds the plan transition lock, rejects
an existing transition journal, and owns snapshot and digest persistence.
Python does not write either identity file, including after `--wait-for-plan`.

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

Review submission and validation are separate durable facts. A slice's
`review_evidence` records the Forgejo review identity, exact PR head, verdict,
immutable `submitted_at`, and renewable `validated_at`. Controller startup may
revalidate an expired exact-head verdict from the authoritative watcher
snapshot; it must not create a new review round, charge budget, or spawn a
reviewer. The pure `RevalidateReview` and `ReviewValidated` transitions update
only validation state and advance the run `state_version` once. Legacy
verdicts without validation evidence are marked `review_validation_required`
by migration and remain quiescent until authenticated, exact-head evidence is
observed. Newer same-head review IDs supersede older evidence, while older or
already-fresh observations are no-ops.

## Checkpoint and resume layout

Each run is persisted at `.exo/tl-loop/<run_id>/run.json`; the shared writer
lock is `.exo/tl-loop/run.lock`. `tl_loop.state.store.resume()` reconstructs
the immutable recursive `plan_manifest`, FSM, slice map, budget ledger, and
event-log replay offset from that file. The manifest records the complete
scope/node declaration, stable source ordering, branch ownership, integration
targets, ordered stages, and nested child-manifest digests. Every persisted
slice carries its exact manifest node ID and revision. Once a run exists,
external `plan.json` is only a candidate: an equal canonical digest is
accepted, a changed plan requires an explicit monotonic revision, and a
revision that mutates dispatched ownership or completed history fails closed.
Resume treats the checkpoint and event log as authoritative and performs no
server or network query. Legacy checkpoints receive deterministic manifest
bindings; ambiguous child kind or ownership is recorded as an actionable
recovery gate rather than guessed.

Active legacy manifests are upgraded only through
`tl_loop.state.legacy_manifest.reconcile_legacy_manifest`. The external plan
is a candidate, never authority: each existing scope/name binding must be
proved by the confirmed spawn intent and result, immutable declaration,
branch/worktree, publication and handoff coordinates, exact-head review, and
the current action journal. Nested scopes additionally require the child
checkpoint and its complete manifest digest. The atomic store mutation then
rebinds every slice to the canonical node and revision while retaining runtime
evidence and writes a bounded `legacy_manifest_migration` proof record. Missing
or conflicting evidence creates a deterministic `plan-manifest-migration`
gate and leaves the legacy checkpoint untouched; only a proven migration may
clear those gates.

## Durable post-merge recovery boundaries

Remote merge adoption opens a per-slice recovery FSM; it does not complete the
slice. The controller checkpoints after each boundary: parent-branch sync,
Chainlink issue closure, changelog commit, and compare-guarded parent push.
Each boundary is a distinct EffectJournal operation and must return an
authoritative receipt before the next transition. The WASM tools perform the
Git operation and verification; Python must never manufacture parent commits,
remote heads, ancestry proofs, or push receipts. Parent pushes use
`force-with-lease` against the persisted synchronized remote head and remain
pending when that compare fails, so restart can reconcile the exact durable
intent without redispatching an already confirmed effect.

## Repository identity resolution

`RunState.repository_identity` (owner, repo, base branch, forge host,
sanitized remote URL) is static run configuration -- identical for every
slice, never changing during a run -- resolved through the `repository_identity`
effect (`agent.repository_identity`), never through the watcher: the watcher
relays live per-PR observations, and identity is neither live nor per-PR.

A caller-supplied `TLLoopConfig.repository_identity` is persisted once at
continuation and any mismatch against an already-persisted value raises
(`"continuation repository identity differs from the checkpoint"`) -- this
path is unchanged by #1062. A checkpoint that predates this field (or was
never supplied one) is healed lazily: `_reconcile_merged_slice` catches the
exact `_repository_identity` fail-closed error the first time a merge
adoption actually needs it, resolves the effect once, persists it, and
retries the same adoption within the same call. Resolution is *not*
attempted proactively on every continuation -- a run whose slices never reach
merge adoption (e.g. a crash-restart mid-dispatch) must never pay for or gate
on an effect it doesn't need, and unconditional resolution at startup
regressed exactly that invariant during development. Failure to resolve
opens the named `tl-repository-identity` gate rather than raising or
guessing an owner/repo; the blocked slice recovers through this same
lazy path on a later call once the effect succeeds, with no second post-merge
entry point.

## Direct-leaf/worker scope draining

`WorkPlan.sub_tls` empty is the direct-leaf/worker scope shape. It has no
recursive sub-child dispatch to await, so once the ledger event source is
empty, remaining work (post-merge boundary steps, scope finalization) is
entirely internal to already-persisted state -- there is no future event
that will ever nudge it. `_run_loop`'s "no event" branch calls
`_drain_direct_scope_convergence` for this shape only; it repeats
`_apply_convergence` with a fresh `ConvergenceTracker` per step (a fresh
tracker sidesteps the shared tracker's repeated-action/state_version dedup
guard, since most post-merge boundary steps do not bump `state_version`)
until `derive_next_action` reports `Quiescent`.

Each step compares persisted content -- every `RunState` field except the
`version`/`revision`/`state_version` write counters, which a checkpoint (or,
for `state_version`, `_apply_convergence`'s own `InternalTransition`
handling) can bump even when nothing meaningful changed -- before and after.
A step whose content doesn't
move is a non-progressing action and raises `TLLoopError` immediately,
rather than letting a later empty poll silently re-attempt it forever. A
step that does move content is real progress and never raises merely for
running long: exhausting `DIRECT_SCOPE_DRAIN_STEP_LIMIT` (a per-call
fairness cap, deliberately distinct from `MAX_CONVERGENCE_STEPS`, which
bounds only the internal steps *within* one `_apply_convergence` call to
reach a single action or wait state) just returns the partially-drained
state for the next empty poll to continue -- a scope needing many action
boundaries (e.g. several leaves' post-merge sequences) must never be
mistaken for a stuck one. This is a parallel, whole-scope-level drain next
to `_run_sub_tls`'s own per-child `_drain_post_merge_recovery` loop for the
`sub_tls` shape; the two are gated on disjoint plan shapes and neither
substitutes for the other.

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
server-emission findings tracked by M2.7. `agent.spawn_failed` is one of them:
it is the runtime's refusal record, projected so the controller can read its
machine code rather than infer one.

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

An accepted request with delayed evidence remains `dispatch_unconfirmed`
indefinitely; `dispatch_timeout` bounds only the transport operation and never
creates a lifecycle failure. The persisted intent and last boundary remain
visible until a matching event, verified owner reconciliation, explicit
cancellation, or human escalation resolves the slice.

On restart, `dispatching` and `dispatch_unconfirmed` slices are reconciled by
their persisted intent IDs before new effects are considered. Reconciliation
never issues a second spawn for an existing intent; it waits for matching
evidence or an explicit resolution. Controller boundary events are limited to
scalar dimensions and are written by Rust through the `tl` event allowlist.

### Dispatch attempt identity

`DispatchAttempt` is the one durable identity of a spawn: the `intent_id`, the
instant it was minted, its attempt number, the run's `controller_epoch`, its
`dispatch_generation`, and the `ledger_floor` a refusal for it is correlated
from. Exactly three sources may produce one, and each is a single method, so a
newly added field cannot be dropped by a hand-copied reconstruction:

- `_new_dispatch_attempt` mints it, with the run's current controller epoch and
  `dispatch_generation = attempt` when an epoch is in force.
- `DispatchAttempt.routed` attaches the harness the policy path selected. It is
  `dataclasses.replace`, so the identity, ledger floor, epoch, and generation
  survive selection. A policy-path dispatch therefore records the same
  provenance as a direct one; the route is the only difference between them.
- `DispatchAttempt.recorded_for` is the single reconstruction of an attempt from
  persisted state. It reads every field from the slice's own dispatch boundary
  (intent, instant, resolved agent type, model, attempt, generation, ledger
  floor) plus the epoch its caller passes, and a slice that records no intent
  has no attempt, so it fails closed instead of inventing one. The persisted
  boundary records the resolved agent type rather than the qualified harness
  identifier, so both routing dimensions of a reconstruction read that value.

`dispatch_generation` has exactly one meaning: the per-slice dispatch attempt
counter under the current controller epoch. It is minted with the intent,
persisted on the slice in the same single-writer state mutation that records the
intent, echoed on every `tl.dispatch_*` boundary, and is the value any later
reconstruction reports. It is not a controller generation (that is
`controller_epoch`) and not a publication counter. A spawn observation that
carries a generation is adopted only when it names the persisted one, so a
confirmation belonging to an earlier attempt of the same intent is refused as
`dispatch_generation_mismatch`; a publication that carries one must carry the
generation of the dispatch that owns it, which is why the same field serves
both correlations. A scheduled retry created nothing, so it clears the
generation with the rest of that attempt's identity and the re-drive mints the
next one. The runtime's own ledger rows do not carry either field -- `file_pr`
writes no `generation`, and no Rust writer emits `dispatch_generation` -- so
both checks apply only to rows that carry one, and a row that carries none is
never refused on a dimension it does not claim.

`tl_loop/tests/test_dispatch_provenance.py` holds these properties: the two
spawn paths must record and persist the same identity, a re-drive persists and
emits its own generation, a reconstruction must carry every field, and every
field must be propagated by the one reconstruction. The last two are structural
guards, so adding a field without propagating it fails rather than silently
reaching a boundary event as its default.

A sub-TL start is the one site that is a mint and not a reconstruction: it
stamps a fresh identity for a child controller, so it carries neither this run's
epoch nor a generation. The child controller mints its own epoch, and the
generation persisted on a sub-TL slice is the one the parent's publication
binder compares the child's own PR event against, not this run's attempt
number. Carrying this run's epoch there would attribute the parent's generation
to a child that never dispatched under it.

### Dispatch failure classification and retry

A rejected spawn request is classified by its stable machine code and by
nothing else. The code is the `code` field of the correlated durable
`agent.spawn_failed` ledger event, which Rust writes from the typed
`EffectError` it already returned. The `dispatch_error` prose is written for an
operator reading a parked run and is never inspected. An unreadable ledger, a
missing event, and an empty or absent code all resolve to "no code", which is
terminal.

`tl_loop.loop.dispatch_classification` is the single source of that decision,
in three classes. **The retryable invariant: a code is retryable only when its
refusal proves that no side effect of the dispatch happened** — either the
decision never ran, or the attempt lost a creation race. Anything else is
terminal or ambiguous.

| Class | Codes | Why |
|---|---|---|
| retryable | `worktree.branch_exists` | a creation race: the branch now exists and this attempt created nothing, so a re-drive attaches to it |
| retryable | `worktree.lifecycle_lock_timeout` | the shared lifecycle lock was busy, so the create/attach decision never ran and nothing was created |
| ambiguous | `dispatch.transport_timeout` | the server-side spawn timeout wraps the **whole** spawn, so a worktree, identity, and tmux window may already exist; the outcome is unproven |
| terminal | `worktree.branch_ownership_conflict` | the birth branch belongs to another worktree owner; no retry can change that |
| terminal | `worktree.pr_context_unavailable` | the forge could not supply the resume context; the operator must fix it first |
| terminal | every other code, and no code | fail closed: an unclassified refusal is never retried by default |

Adding a code to `RETRYABLE_CODES` asserts the no-side-effect invariant. It is
never a prose pattern and never a time-based inference.

An **ambiguous** refusal is not a failure at all. The slice holds at
`dispatch_unconfirmed` with its intent, agent-identity slots, and ledger floor
**intact** — that intent is the only correlation the reconciler has against the
correlated `agent.spawned` event and the runtime's owner listing — and it opens
no gate, because this is a wait state rather than a decision. It is resolved by
matching evidence or verified owner reconciliation, exactly like an accepted
request with delayed evidence, and it is **never** re-driven: a second spawn
could put a second actor on the same deterministic branch. An operator can still
abandon it explicitly through `abandon_slice`, which is the documented
destructive path.

A retryable rejection does not leave a `dispatching` intent behind. The slice
moves to `dispatch_retry_scheduled` with `dispatch_retry_attempt`,
`dispatch_next_attempt_at`, `dispatch_error_code`, and `dispatch_error`; the
intent, agent, and invocation identities are cleared, because no leaf, branch,
worktree, or PR exists yet. That status is the only evidence of the boundary
and the schema rejects it if it carries a leaf identity, a missing code, or a
missing instant. Reconciliation leaves it alone: re-driving happens through the
ordinary dispatch path, once `dispatch_next_attempt_at` has arrived, which makes
the backoff a durable scheduled boundary rather than an in-memory sleep. A
restart inside the window resumes the same boundary and issues no intent and no
spawn.

The boundary is idempotent per dispatch attempt. `dispatch_retry_for_attempt`
records which attempt it was scheduled for, so repeated reconciliation of the
same rejected attempt neither consumes budget twice nor reschedules.

Refusal correlation is bounded. Each intent records `dispatch_ledger_floor`, the
consumed ledger position when it was issued, and the classification reads the
ledger forward from there rather than from zero. A refusal for that intent can
only be at or after that position, so a row recorded before it belongs to an
earlier attempt even if it carries the same intent id, and a run with a long
ledger does not re-read it on every refusal.

The delay is bounded exponential backoff. The knobs are `dispatch_retry_limit`
(default 3 scheduled retries), `dispatch_retry_base_delay_seconds` (default
5.0), and `dispatch_retry_max_delay_seconds` (default 60.0) on `TLLoopConfig`;
the first scheduled retry waits one base delay and each later one doubles up
to the cap. They are operator-configurable in `.exo/config.toml` as
`tl_dispatch_retry_limit`, `tl_dispatch_retry_base_delay_seconds`, and
`tl_dispatch_retry_max_delay_seconds`, validated on load (a positive limit,
positive delays, and a cap that is not below the base) and threaded into the
controller launch as `--dispatch-retry-limit`, `--dispatch-retry-base-delay`,
and `--dispatch-retry-max-delay`. `wall_clock` injects the dispatch clock, and
every backoff decision reads it rather than sleeping.

Exactly one named human gate is opened per dispatch failure, and
`tl.gate_opened` is emitted only when that gate is not already pending. Both
gates are scoped to the slice that stopped, the way
`tl-ordered-child-recovery-<child>` is scoped to its child:
`tl-dispatch-ownership-conflict-<slice>` is opened immediately for a terminal
ownership conflict, because the operator action differs: they must resolve who
owns the branch, not merely acknowledge a refusal. `tl-dispatch-failed-<slice>`
is opened when the configured attempt limit is exhausted, keeping the machine
code that proved the last attempt retryable. The scope is required, not
cosmetic: one run's `gates` list is shared by every slice, so a run-global name
lets a second exhaustion reuse the first slice's gate and emit no
`tl.gate_opened` at all. A slice id is the whole scope — gates live in the
slice-owning run's checkpoint and slice ids are unique inside it — and the code
picks the prefix, so an absent code still names the refusal gate rather than the
conflict gate. Answering one slice's gate records that slice's decision and
leaves every other gate pending; it never releases or re-dispatches anything by
itself.

A checkpoint written before per-slice naming holds one run-global
`tl-dispatch-failed` or `tl-dispatch-ownership-conflict` gate. It is migrated,
never renamed by assumption: the slice parked on `DISPATCH_FAILED` with
`park_cause=dispatch_failed` names the exhaustion the gate was opened for, and
the rename preserves the gate's pending status, advances `state_version` like a
gate answer, and emits no second `tl.gate_opened`. Exactly one such slice is
required — zero, several, or a recorded machine code that contradicts the
legacy gate's prefix fails the controller closed with the exact answer command
that retires the gate, and leaves the checkpoint untouched. A legacy gate that is
already answered is left exactly as it is: it is the durable record of a
decision, and every later exhaustion opens its own per-slice gate.

`tl.dispatch_retry_scheduled` carries `machine_code`, `retry_attempt`, and
`next_attempt_at` as their own dimensions beside the operator prose, so a
reader can classify a retry without parsing a message. `agent.spawn_failed`
rows are telemetry for the loop: the boundary is already persisted, and the
event is the evidence the classification reads.

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

## Per-slice model tier selection

The selector resolves a model *within* the already-selected harness from a
checked-in, human-authored `.exo/model-catalog.json`, loaded offline at
controller startup (`tl_loop.select.model.load_model_catalog`). The catalog
never widens `harness_policy.toml`'s allowlist or budget ceilings — it only
orders model entries inside the harness the harness selector already chose.

Each catalog entry may carry an optional `coding_score` (0-100 composite
benchmark index) and `price_per_1m_tokens`. `select_model_for_difficulty`
maps the classified `Difficulty` to an abstract tier against whatever catalog
is loaded:

- TRIVIAL/STANDARD, not escalated → lowest `price_per_1m_tokens / coding_score`
  (cost per intelligence point). Entries missing score/price sort last; ties
  resolve to catalog order.
- HARD, or escalated → highest raw `coding_score`. Escalation reuses the same
  `escalate_after_attempts` + NO-GO signal as harness escalation, surfaced via
  `HarnessChoice.reason == "escalated_after_no_go"`; there is no second counter.

Precedence at the dispatch call site mirrors `effective_model_for`'s
override-wins-over-config: an explicit `TLLoopConfig.requested_model` wins over
the difficulty-derived tier, which wins over the harness-pinned `route.model`.
A missing catalog file fails open to today's static per-role model config
(including the "unresolved" log line for a bare harness string); a
present-but-malformed catalog raises `ModelResolutionError` rather than
guessing. Catalog entries carry no vendor-specific model table in
`tl_loop/select/` — the score/cost fields are seeded offline by the operator,
the same way `harness_policy.toml` and `harness_capability.toml` are authored.

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
The controller activates only the persisted current numeric stage: its
same-order children run concurrently, and the next stage cannot activate until
every child in the current stage has completed its own integration and
post-merge recovery. Stable child ID order controls aggregate integration,
independent of dispatch or review completion order. Legacy sub-TL plans without
`order` remain one order-1 stage.

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
