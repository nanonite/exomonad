# Durable Agent Steering Queue Design

**Status:** Design proposal for #723; ratification deferred to #756.

## Goal and boundary

Provide one durable, per-agent queue for steering and follow-up guidance that
can be offered at a harness turn boundary, acknowledged only by positive
runtime evidence, and recovered after an ExoMonad process restart.

The queue owns ordering, identity, leases, retries, and evidence. It does not
own the Claude Code, Codex, or OpenCode model-response loop. A runtime adapter
must report when it is safe to offer a batch and when the runtime accepted it.
An injection into tmux, a Teams file write, or a successful UDS response is a
transport result, not by itself a runtime-consumption acknowledgement.

## Existing state and required change

The current system has two related but different stores:

1. `InboxStore` persists SQLite `messages`, `notified_at`, and `read_at`.
   `drain_unread` marks rows read and emits `message.consumed` before a
   runtime has proved that it accepted a new turn.
2. `AgentInbox` provides a per-agent in-memory `VecDeque`, one active
   consumer, a 32-message cap, a 30-second deduplication window, retries, and
   an abandonment event. A process restart loses its queue and dedup state.

The design keeps `messages` as the durable human/worker inbox compatibility
surface, but introduces a separate guidance queue with a clear lifecycle. A
single enqueue transaction may retain a source inbox message ID for
traceability, but the guidance queue is the scheduler's source of truth. The
two stores must never be independently treated as proof of consumption.

## Data model

Use the existing `.exo/inbox.db` with two new tables. Names are descriptive;
the final migration may choose equivalent names without changing the contract.

### `guidance_batches`

One row represents one atomic batch offered to one agent.

| Column | Contract |
|---|---|
| `batch_id` | UUID, immutable public identity and idempotency key |
| `agent_id` | Normalized bare `AgentName`; stable queue key |
| `queue_class` | `steering` or `follow_up` |
| `queue_seq` | Monotonic per-agent sequence allocated in the enqueue transaction |
| `state` | `pending`, `leased`, `submitted`, `accepted`, `abandoned`, or `cancelled` |
| `created_at` | UTC timestamp from the durable write |
| `available_at` | Earliest claim time, used for retry backoff |
| `lease_owner` | Invocation/consumer identity while leased or submitted |
| `lease_expires_at` | Deadline for recovery of a lost consumer |
| `attempt_count` | Number of transport submissions, incremented transactionally |
| `accepted_at` | Runtime acceptance timestamp, nullable until acknowledged |
| `terminal_at` | Abandonment or cancellation timestamp |
| `terminal_reason` | Bounded reason code, never free-form workflow authority |
| `run_id` | Controller run correlation, nullable for non-controller messages |
| `session_id` | Runtime session correlation, nullable when unavailable |
| `invocation_id` | One-shot invocation correlation |
| `generation` | Resume/retry generation when available |
| `runtime` / `harness` / `role` | Snapshot of the target identity at enqueue time |
| `source_message_id` | Optional foreign-key-like pointer to the compatibility inbox row |

The database enforces `queue_class`, `state`, non-negative `queue_seq`, and
one unique `(agent_id, queue_seq)` pair. `accepted`, `abandoned`, and
`cancelled` are terminal states. A terminal batch is never claimed again.

### `guidance_items`

Each batch has one or more ordered items:

| Column | Contract |
|---|---|
| `batch_id` | References `guidance_batches.batch_id` |
| `position` | Zero-based immutable order within the batch |
| `item_id` | UUID for per-message evidence and replay |
| `from_agent` | Sender identity |
| `content` | Local-sensitive message body |
| `summary` | Optional short transport summary |
| `injection_options` | Versioned JSON adapter hints, not runtime state |

The primary key is `(batch_id, position)`, and `position` must be contiguous
from zero. The batch is the scheduling unit: an adapter cannot acknowledge
item 0 and leave item 1 pending. A single-message notification is a batch of
one. An array from a prime-agent-style `steer` or `followUp` call remains one
atomic batch.

The content and source pointers stay local to L1/L2 storage. Ledger events
carry identities and bounded dimensions, not unrestricted message bodies.

## State machine

```text
                 lease acquired
pending ------------------------------> leased
   ^                                      |
   | lease expired / retry                | transport submitted
   |                                      v
   +---------------------------------- submitted
                                          |
                              runtime accepts batch
                                          v
                                       accepted

pending/leased/submitted -- explicit cancel --> cancelled
pending/leased/submitted -- retry budget exhausted --> abandoned
```

State transitions are compare-and-set operations in SQLite transactions:

- `pending → leased` requires the agent, queue class, `available_at`, and
  unexpired/no lease predicate to match. It records `lease_owner` and a
  bounded lease deadline.
- `leased → submitted` records the transport attempt and increments
  `attempt_count`. It does not mark the batch consumed.
- `submitted → accepted` requires an idempotent acknowledgement containing the
  exact `batch_id`, target identity, invocation, generation, and adapter
  evidence. Repeating the same acknowledgement is a no-op.
- `leased/submitted → pending` occurs only when the lease expires or the
  adapter explicitly releases the batch for retry. The next `available_at` is
  calculated from the bounded retry policy.
- Any non-terminal state may become `cancelled` only through an explicit
  cancellation operation. A batch that was already accepted remains accepted.
- A retry budget or queue retention deadline may transition a batch to
  `abandoned`; the source row and all evidence remain durable.

The queue uses at-least-once delivery. If a runtime accepts a batch and the
acknowledgement is lost before the lease is committed, a later retry may
duplicate the visible message. Stable batch and item IDs make this detectable;
exactly-once behavior requires a runtime-native idempotency contract that the
current three harnesses do not share.

## Queue selection and turn boundaries

There is at most one active lease per agent, even though steering and follow-up
are separate logical queues. This prevents a follow-up batch from passing a
steering batch while a transport acknowledgement is outstanding.

The claim transaction applies prime-agent's ordering without assuming control
of the target loop:

1. The adapter reports a target identity and a safe boundary phase.
2. If the phase is `turn_finished`, claim the oldest available steering batch.
3. If steering is empty and the phase is `would_stop`, claim the oldest
   available follow-up batch.
4. If no batch is eligible, return no work and leave all rows pending.
5. Do not claim guidance while a tool batch is executing, or after a hard
   runtime stop that explicitly declines further work.
6. Submit the whole claimed batch through the runtime adapter, then wait for
   the adapter's positive acceptance acknowledgement before claiming another.

The queue does not infer a turn boundary from a tmux pane becoming visually
empty. The adapter must supply the boundary evidence. If a runtime has no
such signal, the safe behavior is to retain the durable batch and expose a
pending/blocked condition rather than send an unbounded stream of input.

### Adapter contract

The implementation should expose the following conceptual operations:

```text
enqueue_batch(target, class, items, identity) -> batch_id
claim_next(target, boundary, consumer) -> batch | none
record_transport_attempt(batch_id, attempt, result)
acknowledge_runtime(batch_id, evidence) -> accepted | already_accepted | rejected
release_for_retry(batch_id, reason, next_attempt_at)
cancel_batch(batch_id, reason)
recover_expired_leases(now)
```

`boundary` is runtime evidence, not a caller-selected bypass. `consumer`
contains the one-shot invocation and generation. `evidence` is an adapter
specific bounded object, for example a Teams read timestamp, a Codex hook or
prompt-boundary correlation, or an OpenCode session event. It must not be
accepted solely because the transport command returned zero.

## Runtime adapter behavior

| Runtime | Offer path | Acceptance evidence | If evidence is unavailable |
|---|---|---|---|
| Claude Code | Write one batch to the native Teams inbox | Native mailbox read/verification plus session correlation; classify as mailbox acceptance, not context proof | Keep the batch submitted/leased and retry or escalate; do not silently mark consumed |
| Codex | ExoMonad FIFO and runtime-specific turn-boundary injection | Positive TUI/runtime signal or a future native hook correlation containing the batch ID | Retain and surface pending; a tmux write is only a transport attempt |
| OpenCode | ExoMonad FIFO or a future plugin/session adapter | Plugin/session event tied to the batch and invocation | Retain and surface pending; plugin hook success alone is insufficient |

The message envelope should include `batch_id`, `item_id`, `queue_class`, and
the invocation generation where the runtime can preserve them. If a runtime
cannot echo those IDs, the adapter records the best available correlation and
marks the acknowledgement confidence as `inferred` or `unknown`; it must not
upgrade that evidence to exact runtime acceptance.

## Transaction and recovery rules

### Enqueue

`enqueue_batch` runs in one SQLite transaction:

1. Validate target identity, queue class, batch size, content limits, and
   sender permissions.
2. Resolve the stable bare `AgentName` and snapshot runtime identity.
3. Allocate `queue_seq` and `batch_id`.
4. Insert the batch and all items.
5. Commit before asking any runtime or transport to act.
6. Emit the durable enqueue observation only after commit.

Failure before commit produces no visible batch. Failure after commit is
replayed from SQLite and the ledger; an API retry with the same client-supplied
idempotency key returns the existing batch instead of creating another.

### Claim

`claim_next` uses `BEGIN IMMEDIATE` (or an equivalent serialized transaction),
selects one eligible batch with steering precedence, and updates its lease in
the same transaction. A second consumer sees no eligible row for that agent.
The lease is short enough to recover a crashed consumer and long enough to
cover the adapter's bounded transport operation. Lease ownership is checked
on every state transition.

### Restart

On startup, recover leases whose deadlines have passed. For each recovered
batch, append a lease-expired observation, increment no attempt counter until
resubmission, and place it back in `pending` with bounded backoff. A restart
must never delete a pending, submitted, accepted, or terminal row.

The in-memory `AgentInbox` may remain as a transport worker cache during
migration, but it cannot be the only queue. Its items must be reconstructed
from durable `pending` or expired `submitted` batches, and its process-local
deduplication map must not decide durable identity.

### Acknowledgement

`acknowledge_runtime` is idempotent and validates:

- exact `batch_id` and target agent;
- current or prior matching invocation/generation;
- a recognized adapter evidence type;
- a batch state of `leased` or `submitted`; and
- the batch has not been cancelled or abandoned.

It records `accepted_at` and transitions the whole batch to `accepted`. A late
ack after a retry is retained as duplicate evidence and does not acknowledge a
different batch.

## Evidence contract

Existing event names remain useful, but their payloads must distinguish the
three different facts:

| Event | Meaning |
|---|---|
| `inbox.state_changed` | SQLite state mutation such as enqueue, claim, lease expiry, or terminal transition |
| `message.delivery` | One transport attempt and its result; success means the transport accepted the write |
| `message.consumed` | Positive runtime or mailbox acceptance for the exact batch/item; include `ack_kind` and evidence confidence |
| `agent.guidance.delivery` | Operational delivery dimensions for runtime, method, and outcome |
| `agent_inbox.messages_abandoned` | Explicit terminal abandonment after bounded retry/retention policy |

Every event for a guidance batch includes, when known:
`batch_id`, `item_id`, `agent_id`, `queue_class`, `queue_seq`, `run_id`,
`session_id`, `invocation_id`, `generation`, `runtime`, `harness`, `role`,
`attempt`, and `consumer`. Event payloads may include a bounded `reason` and
`evidence_kind`; message bodies remain local.

`message.consumed` must no longer mean merely “an agent called
`check_inbox`.” A legacy inbox read can remain observable with
`ack_kind="inbox_read"` and `confidence="observed"`; runtime turn acceptance
uses a distinct `ack_kind="runtime_accepted"`. This prevents the observability
contract from treating a durable read as proof that the next assistant turn
used the message.

The event registry and expected-event rules need a follow-up update in #756 so
that a durable enqueue expects delivery and eventual consumption or explicit
abandonment, while a transport success without consumption remains pending or
unknown rather than successful.

## Deduplication and ordering

Deduplication is durable and identity-based:

- Prefer a caller-provided idempotency key scoped to the source event and
  target invocation.
- Otherwise generate a UUID and treat repeated bodies as distinct messages.
- For structured workflow notifications, use a stable key such as event type,
  target, PR number, head SHA, and review round—not a short-lived body hash.
- Preserve separate steering and follow-up queues, but serialize claims over
  both classes per agent.
- Never use a 30-second process-local window as the durable duplicate rule.

Ordering is by `queue_seq` within an agent and class. A retry retains its
sequence; it does not leapfrog a failed head. A follow-up cannot pass a
pending steering batch. Cancellation or abandonment is explicit in the
sequence and evidence, not an invisible deletion.

## Security and limits

The queue validates the target against the agent resolver and role policy at
enqueue and claim time. Sender identity is recorded from the authenticated
effect context, not accepted from message text. Content size, item count,
batch size, retry count, lease duration, and retention are bounded by config.
SQL writes use parameters; message content is never interpolated into SQL or
shell commands. The queue never grants merge, review, or PR authority.

## Migration and verification plan

1. Add the schema and transactional queue API without changing legacy
   `InboxStore::drain_unread` behavior.
2. Route new steering/follow-up producers through `enqueue_batch`; retain the
   existing `messages` row as a compatibility pointer where required.
3. Add one adapter at a time, beginning with the runtime that can provide the
   strongest positive boundary evidence. Keep legacy delivery in shadow mode
   until queue and ledger projections agree.
4. Rebuild the in-memory FIFO from durable pending/expired rows on restart.
5. Update event registry, expected-event rules, observability fixtures, and
   replay projections.
6. Remove the in-memory queue as an authority only after restart, lease,
   duplicate, retry, and abandonment gates pass for all supported runtimes.

The implementation test matrix must cover:

- concurrent claims for one agent never leasing two batches;
- independent agents progressing concurrently;
- steering precedence over follow-up and one-batch atomicity;
- crash/restart recovery of `leased` and `submitted` rows;
- idempotent enqueue and acknowledgement;
- late and duplicate acknowledgements;
- retry sequence preservation and explicit abandonment;
- queue caps, retention, cancellation, and authorization;
- no `message.consumed` event from transport success alone; and
- ledger replay producing the same pending/accepted/terminal projection.

## Conclusion

The durable queue should sit between workflow producers and runtime-specific
delivery adapters. It should offer one atomic batch at a safe harness boundary,
wait for positive acknowledgement, and make ambiguity visible as pending or
unknown. This preserves prime-agent's steering/follow-up ordering while
respecting #754's finding that ExoMonad cannot own the active model loop across
Claude Code, Codex, and OpenCode.
