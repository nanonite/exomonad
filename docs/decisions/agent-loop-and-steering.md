# Agent Loop and Durable Steering

**Status:** Accepted for E6; implementation work follows the queue and adapter
plan below.

**Date:** 2026-08-12

**Issues:** #723, #752, #753, #754, #755, #756

## Context

ExoMonad needs durable TL-to-agent steering that survives transport failure,
process restart, and runtime-specific input behavior. The prime-agent study
shows a useful model: steering and follow-up are separate queues, messages are
consumed as atomic batches, steering has priority after the current tool batch,
and follow-up is considered only when the agent would otherwise stop.

The current ExoMonad transports do not expose one shared way to append a
message to the next model context:

- Claude Code owns its response loop and reads its native Teams inbox.
- Codex exposes synchronous command hooks around tools and stopping, but no
  common next-context queue API.
- OpenCode exposes a TypeScript plugin around tools and session stop events,
  but the current plugin has no durable prompt/continue acceptance API.
- UDS and tmux provide delivery paths for custom or non-Claude runtimes, not a
  common model-loop authority.

The detailed transport observations and ledger measurements are recorded in
[Agent steering transport evidence](agent-steering-transport-evidence.md).
The cross-runtime assessment is in
[Agent loop ownership by harness](agent-loop-ownership-by-harness.md), and
the proposed queue schema and state transitions are in
[Durable agent steering queue design](durable-agent-steering-queue.md).

## Decision

### 1. ExoMonad owns workflow and queue authority, not the active model loop

`tl_loop` remains the sole workflow coordinator. It owns WorkPlan dispatch,
Chainlink accounting, review/CI/merge gates, retry and escalation state, and
the immutable-ledger projection.

Claude Code, Codex, and OpenCode retain ownership of their active response
loops, including model requests, current tool-call batches, context assembly,
turn boundaries, and runtime stop/idle behavior. ExoMonad may express a
steering or cancellation intent and may observe runtime evidence, but it may
not treat transport input as proof that the runtime accepted or acted on that
intent.

This is the only loop boundary that satisfies the hard harness-agnosticism
constraint without replacing the supported runtimes with new model clients.

### 2. Use a durable per-agent guidance queue

Guidance is persisted in `.exo/inbox.db` as atomic batches. The queue has two
classes:

- `steering`: eligible at a safe post-turn boundary, before follow-up or
  continuation;
- `follow_up`: eligible only when the runtime reports that it would otherwise
  stop and no steering batch is pending.

Each batch has a stable `batch_id`, one or more ordered `item_id` values, a
per-agent `queue_seq`, target runtime identity, source and invocation
correlation, and an explicit state. The durable states are `pending`, `leased`,
`submitted`, `accepted`, `abandoned`, and `cancelled`.

One batch is the scheduling and acknowledgement unit. A batch containing
several messages is not split across turns, and a later batch cannot pass a
leased or submitted head for the same agent. There is at most one active lease
per agent across both queue classes.

The durable queue replaces process-local queue state as the authority. The
existing `AgentInbox` may remain temporarily as a transport worker cache, but
it must be reconstructible from durable pending or expired rows and its
in-memory deduplication window cannot determine durable identity.

### 3. Consume only at an adapter-reported safe boundary

The queue does not infer a turn boundary from pane liveness, input-buffer
absence, or a successful write. A runtime adapter must report a boundary before
claiming work:

1. At `turn_finished`, claim the oldest available steering batch.
2. If steering is empty and the runtime is at `would_stop`, claim the oldest
   available follow-up batch.
3. Do not claim while the current tool batch is executing or after a hard stop
   that declines more work.
4. Submit the entire batch, wait for positive acceptance evidence, and only
   then release the per-agent claim for the next batch.

If a runtime cannot expose a safe boundary or positive acceptance, the adapter
retains the batch and surfaces pending/unknown state. It must not send an
unbounded stream of tmux input to simulate loop ownership.

### 4. Use leases and at-least-once delivery

Claiming is a serialized SQLite compare-and-set transaction. The lease records
the consumer, invocation, generation, and expiry. A lost consumer leaves the
batch durable; startup recovery returns expired `leased` or `submitted` work
to `pending` with bounded backoff.

Transport submissions are at-least-once. If a runtime accepted a batch but its
acknowledgement was lost, retry may produce a duplicate. Stable IDs and
idempotent acknowledgement make that duplicate observable. Exactly-once
delivery is not promised because the supported runtimes do not share a native
idempotency contract.

### 5. Separate transport, runtime acceptance, and workflow authority

The following facts remain distinct:

| Fact | Evidence | Authority |
|---|---|---|
| Durable enqueue | Committed SQLite batch and ledger observation | Queue projection |
| Transport attempt | `message.delivery` with method, attempt, and outcome | Delivery diagnostics |
| Runtime/mailbox acceptance | Idempotent acknowledgement tied to batch and invocation | Queue consumption projection |
| Workflow result | Review, CI, PR, merge, or explicit human-gate event | Immutable ledger and `tl_loop` FSM |

A Teams file timestamp, UDS HTTP 2xx, tmux injection success, or hook command
success is transport evidence unless the adapter supplies the stronger
runtime-acceptance correlation. No guidance event can approve a review, merge a
PR, or alter the controller FSM directly.

Existing `message.consumed` observations must identify their acknowledgement
kind and confidence. A legacy inbox read can be recorded as
`ack_kind="inbox_read"`; a target-runtime boundary is
`ack_kind="runtime_accepted"`. A transport success alone never emits the
stronger event.

## Adapter matrix

| Runtime | Current offer path | Acceptance boundary | Decision |
|---|---|---|---|
| Claude Code | Native Teams inbox, with ExoMonad fallback only for owned recipients | Native mailbox read/verification plus session correlation; this is mailbox evidence, not automatic context proof | Keep Claude loop native; serialize durable offers through a Teams adapter |
| Codex | ExoMonad FIFO and runtime-specific tmux path; shell hooks for policy and observation | Positive TUI/runtime signal or future hook correlation carrying batch identity | Keep Codex loop native; do not equate tmux write with acceptance |
| OpenCode | ExoMonad FIFO/tmux path and the existing TypeScript tool/lifecycle plugin | Plugin/session event tied to batch, invocation, and runtime identity | Keep OpenCode loop native; plugin hook success is not prompt acceptance |
| Custom UDS runtime | HTTP `/notify` over per-agent Unix socket | Runtime-specific response and correlation contract | Permit an adapter only when it documents positive acceptance semantics |

The adapter envelope should carry `batch_id`, `item_id`, `queue_class`, and
invocation generation where the runtime can preserve them. If it cannot echo
those identities, the adapter records evidence as inferred or unknown instead
of upgrading it to exact acceptance.

## Queue contract

The implementation should provide the following operations, with all state
changes and identity checks durable:

```text
enqueue_batch(target, class, items, identity) -> batch_id
claim_next(target, boundary, consumer) -> batch | none
record_transport_attempt(batch_id, attempt, result)
acknowledge_runtime(batch_id, evidence) -> accepted | already_accepted | rejected
release_for_retry(batch_id, reason, next_attempt_at)
cancel_batch(batch_id, reason)
recover_expired_leases(now)
```

`enqueue_batch` commits the batch and all items before transport begins.
`claim_next` gives steering precedence and holds one lease per agent.
`acknowledge_runtime` validates exact target and batch identity, matching
invocation/generation, recognized adapter evidence, and a non-terminal state.
Repeated acknowledgements are no-ops; late acknowledgements become duplicate
evidence and cannot acknowledge another batch.

Deduplication is identity-based rather than a 30-second process-local body
hash. Producers should supply a stable idempotency key for structured events;
free-form messages receive a UUID and remain distinct even when their bodies
match. Queue sequence is retained across retry, cancellation, and
abandonment, so ordering remains explainable during replay.

## Ledger and observability contract

Every guidance event includes, when available, `batch_id`, `item_id`,
`agent_id`, `queue_class`, `queue_seq`, `run_id`, `session_id`,
`invocation_id`, `generation`, `runtime`, `harness`, `role`, `attempt`, and
`consumer`. Bodies remain local-sensitive and are not copied into aggregate
ledger projections.

The current event vocabulary is extended by payload fields rather than
creating a second message authority:

- `inbox.state_changed` records enqueue, claim, lease expiry, acknowledgement,
  cancellation, and abandonment transitions;
- `message.delivery` records each transport attempt;
- `message.consumed` records positive mailbox/runtime acceptance and its
  `ack_kind`/confidence;
- `agent.guidance.delivery` records operational delivery dimensions; and
- `agent_inbox.messages_abandoned` records explicit terminal abandonment.

The event registry, expected-event rules, fixtures, and replay projection must
be updated with this contract. A durable enqueue expects eventual acceptance
or explicit abandonment. A transport success without either remains pending
or unknown and cannot count as successful consumption.

## Rejected alternatives

### Make Claude Code the universal loop owner

This would turn Teams and Claude's InboxPoller into the de facto protocol.
Codex and OpenCode would still require tmux compatibility paths, leaving the
system Claude-specific and weakening the cross-runtime invariant.

### Make tmux the universal loop owner

Tmux serializes keystrokes, not model context, tool completion, turn boundaries,
or runtime acceptance. The #752 ledger evidence includes failures,
abandonments, and duplicate ambiguity after transport attempts. Tmux is an
adapter, never the response-loop authority.

### Treat SQLite `read_at` as runtime consumption

An inbox read proves that a durable row was observed or marked read. It does
not prove that the target harness appended the message to the next context.
Conflating these facts would produce false consumption rates and unsafe retry
decisions.

### Keep the process-local FIFO as the source of truth

The current FIFO has useful serialization and retry behavior, but restart loses
queued messages and its dedup window. It cannot satisfy durable recovery or
cross-process replay without a SQLite-backed authority.

### Replace all supported runtimes with an ExoMonad model client

That would create a common loop only by discarding runtime-owned context,
tools, hooks, and lifecycle semantics. It is a different product and control
plane, outside E6.

### Let guidance alter review or merge state

Steering is delivery intent. It cannot authorize a PR review verdict, CI
interpretation, merge, or human gate. Those decisions remain typed ledger
events projected by `tl_loop`.

## Consequences

Positive consequences:

- durable queue state and replay survive controller or transport restart;
- steering/follow-up ordering is explicit and batch atomic;
- duplicates, ambiguous acknowledgements, lease recovery, and abandonment are
  visible rather than silently collapsed;
- Claude, Codex, and OpenCode keep their native response-loop ownership; and
- workflow authority remains in the immutable ledger and `tl_loop` FSM.

Costs and limits:

- positive runtime acceptance adapters are still runtime-specific;
- at-least-once delivery permits duplicates when acknowledgements are lost;
- schema, replay, and observability migrations are required before the durable
  queue replaces the in-memory authority; and
- a runtime without a safe boundary may remain pending until a human gate or a
  new adapter capability is available.

## Implementation gates

The follow-on implementation must demonstrate:

1. atomic enqueue and contiguous batch ordering;
2. concurrent claim exclusion per agent;
3. steering priority over follow-up;
4. lease expiry and restart recovery;
5. idempotent enqueue and acknowledgement;
6. explicit retry, cancellation, and abandonment;
7. adapter evidence that distinguishes transport from runtime acceptance;
8. replay parity between SQLite state and the immutable ledger; and
9. no merge or review authority in any queue or delivery acknowledgement.

This ADR is the E6 decision boundary. The queue design and harness assessment
remain the detailed implementation inputs; they do not authorize ExoMonad to
take over a runtime's active model loop.
