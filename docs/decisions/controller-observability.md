# ADR: Controller decisions belong in the ledger

Date: 2026-08-12
Status: Proposed
Builds on: [tl-as-loop.md](tl-as-loop.md), [watcher-as-sensor.md](watcher-as-sensor.md), [operator-control-plane.md](operator-control-plane.md)

## Context

FailureAtlas measures ExoMonad from the append-only ledger at
`.exo/ledger/segments/*.jsonl`, imported into `.exo/analysis/atlas.db` by
`exomonad logs import` and reduced by `logs measure` / `logs export`.

The agent and PR layer is well covered. An audit on 2026-08-12 found:

- **40 of 41 declared event types are emitted** in non-test source. Only
  `agent.stop_check` has no producer.
- **The 11 denominator rules in `expected-events.v1.json` cover the merge
  path**, including `merge_request_requires_approved_current_head`,
  `merge_request_requires_passing_ci_current_head`, and
  `guidance_enqueue_requires_acceptance_or_abandonment`.

The controller layer is invisible. `tl_loop/CLAUDE.md:99` states the invariant
plainly — *"The loop never writes ledger segments."* It is a ledger **reader**.
Every controller decision lives only in `.exo/tl-loop/<run_id>/run.json`, and
FailureAtlas imports the ledger, not run state.

| Not observable today | Lives only in |
|---|---|
| Gate answered — decision, gate name, when | `run.json` `gates[]` |
| Slice parked — which of the seven causes | `run.json` slice `park_cause` |
| FSM phase transitions | `run.json` `fsm` |
| Merge decision and the evidence behind it | `run.json` per-head review state |
| Budget charge, reservation, reconciliation | `run.json` `budgets.ledger` |
| RLM judgments — `decompose`, `adjudicate_review`, `compose_repair`, `interpret_operator_intent` | `RlmCallStore`; not in the registry at all |
| Plan proposal via `/control` | nowhere durable |

`control_gate.rs:76` shells out to `python3 -m tl_loop gate` and emits nothing.
So a human approving a gate — the point where human authority enters a run —
leaves no ledger trace. Gate latency, park-cause distribution, and judgment
retry rates are exactly the orchestration-quality signals FailureAtlas exists
to produce, and none of them are currently measurable.

## Decision

### 1. A closed set of declared `tl.*` event types

Add to `docs/observability/event-registry.json`, all with payload class
`aggregate_dimensions` and producer `tl_loop`:

| Event | Emitted when | Key dimensions |
|---|---|---|
| `tl.phase_changed` | Durable FSM transition | `from_phase`, `to_phase`, `run_id` |
| `tl.slice_status_changed` | Slice status transition | `slice_id`, `from_status`, `to_status` |
| `tl.slice_parked` | Slice parks | `slice_id`, `park_cause`, `attempts` |
| `tl.gate_opened` | A named gate becomes pending | `gate_name`, `run_id` |
| `tl.gate_answered` | Operator approves or rejects | `gate_name`, `decision`, `source` (`cli` \| `control`) |
| `tl.merge_decided` | Controller decides to merge or not | `slice_id`, `pr_number`, `decision`, `head_sha_hash` |
| `tl.judgment` | Any RLM judgment completes | `judgment` name, `attempt`, `outcome`, `tokens`, `replayed` |
| `tl.plan_proposed` | A `/control` plan proposal is accepted or rejected | `run_id`, `accepted`, `rejection_reason` |

Bodies stay local. These carry identities and bounded dimensions only —
no utterances, no diffs, no repair prose, no plan documents.

### 2. Rust emits; Python decides

`tl_loop` does not gain a ledger writer. It calls a new effect through the
existing `EffectClient` (`tl_loop/client/effects.py`), and the Rust handler
appends via the existing `LedgerWriter` (`services/immutable_ledger.rs:138`).

This preserves both invariants already in force: Rust owns I/O, Python owns
decisions, and there remains exactly one ledger writer. It is the same shape as
every other effect the controller calls.

Emission is **best-effort and never load-bearing**. A failed emit is logged and
the controller continues; observability must not be able to stall a run or
alter a merge decision.

### 3. Not generic `log.emit_event`

`handlers/log.rs:128` already provides generic emission, and reusing it looks
attractive. It is the wrong instrument here.

The registry's `dynamic_event_rule` confines dynamic types to the `custom.`
namespace with `default_payload_class: local_sensitive`. A controller event
emitted that way would be local-sensitive and therefore **excluded from L4
aggregate export** — which is precisely where park-cause distribution and gate
latency need to be visible. Generic emission would produce rows that exist but
cannot be measured.

Declared types with `aggregate_dimensions` are required for the signal to reach
the artifact that gets shared.

### 4. Denominator rules for the controller

Add to `expected-events.v1.json`, in the same style as the existing eleven:

- `park_requires_gate_or_terminal` — a `tl.slice_parked` expects a
  `tl.gate_opened` or a terminal run outcome. A park that silently disappears
  is a defect.
- `gate_opened_requires_answer` — a `tl.gate_opened` expects a
  `tl.gate_answered`, with no delay bound. This is the measurement that makes
  human-gate latency a number rather than an anecdote.
- `merge_decision_requires_pr_outcome` — a `tl.merge_decided` with
  `decision=merge` expects `pr.merged` or `pr.merge_failed`, correlated on
  `pr_number`.
- `judgment_failure_requires_retry_or_park` — a failed `tl.judgment` expects a
  further attempt or a park.

### 5. Refresh the coverage audit

`docs/observability/tl-loop-event-coverage.md` is dated 2026-08-11 and predates
M9, M11, and M12. It cites `worktree_event_watcher.rs` line numbers for
`merge_ready` semantics that E4 removed, and describes an architecture that no
longer exists. Re-audit against the current watcher-as-sensor boundary and
extend it to the controller and guidance-queue surfaces.

It also self-reports two open items worth resolving in the same pass:
`[DEV FAILED]` has no raw watcher observation mapping to it, and
`[RATE LIMITED]` is accepted by the guest with no live producer. Either wire a
producer or remove the variant — a declared signal with no emitter is the same
class of defect as `agent.stop_check`.

## Rejected alternatives

**Let `tl_loop` write ledger segments directly.** Breaks the single-writer
invariant and the Rust-owns-I/O boundary. The reason the controller is a reader
is the same reason the watcher is a sensor.

**Import `run.json` as a FailureAtlas source.** Run state is mutable,
last-write-wins, and has no sequence numbers. The ledger's ordering, replay,
and supersession semantics are what make measurement sound; a mutable snapshot
provides none of them.

**Emit everything, including bodies.** Utterances, diffs, and repair prose are
local-sensitive. The L4 boundary exists to keep them out of shareable
artifacts, and widening it for convenience would undo the privacy contract.

**Do nothing and read `run.json` manually when curious.** That is the current
state. It makes orchestration quality an anecdote rather than a measurement,
and it cannot answer questions across runs.

## Consequences

Positive:

- Human-gate latency, park-cause distribution, judgment retry rates, and merge
  decision outcomes become measurable across runs.
- The denominator rules make a *missing* controller event detectable, rather
  than indistinguishable from a quiet run.
- Controller and agent events share one envelope, one ledger, and one import
  path — no second observability surface.

Costs and limits:

- Emission volume rises with slice count; `tl.slice_status_changed` is the
  chattiest and may warrant a recorder rule if it dominates.
- Best-effort emission means a dropped controller event is possible under
  ledger write failure; `sink.health` already records that condition and
  should be consulted before trusting a controller denominator.
- The registry gains eight types, which is a real increase in contract surface
  that must stay in sync with the emitters. The 40/41 audit method in this ADR
  is the check that keeps it honest.
