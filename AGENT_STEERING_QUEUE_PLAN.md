# Implementation plan — Durable agent steering queue

Follow-on to E6 (#723). The ADR
[`docs/decisions/agent-loop-and-steering.md`](docs/decisions/agent-loop-and-steering.md)
is Accepted but explicitly research-only: *"implementation work follows the
queue and adapter plan below."* Nothing is built yet.

## Context

E6 answered the question and stopped. Current state:

- `#752–#756` produced research and four decision documents, no code.
- Runtime loops remain Claude/Codex/OpenCode-owned — and per the ADR, **that
  is the decision**, not a gap. ExoMonad owns workflow and queue authority, not
  the active model loop.
- Roles remain `dev` + `worker` (`.exo/roles/devswarm/AllRoles.hs:92`). No new
  role is required; this is a non-goal.
- No durable steering queue, leases, acknowledgements, or adapters exist.

What *does* exist, and matters:

| Component | Lines | State |
|---|---|---|
| `.exo/inbox.db` | 20KB | Only `messages` + `agent_inbox_meta`. No batches, classes, leases, or states |
| `services/inbox_store.rs` | 955 | `InboxStore`; `drain_unread` marks read **and emits `message.consumed` before any runtime accepted anything** |
| `services/agent_inbox.rs` | 596 | In-memory `VecDeque`, 32-message cap, 30s body-hash dedup, lost on restart |
| `services/delivery.rs` | 2010 | Transport: Teams, UDS, tmux |
| `services/inbox_watcher.rs` | 178 | Poke/backoff |

So this is an **evolution of a real store**, not a greenfield build. The
existing `messages` table stays as the human/worker compatibility surface; the
guidance queue becomes the scheduler's source of truth alongside it.

## Target

Two new tables in `.exo/inbox.db`:

- **`guidance_batches`** — `batch_id` (UUID), `agent_id`, `queue_class`
  (`steering` | `follow_up`), per-agent monotonic `queue_seq`, `state`,
  `available_at`, `lease_owner`, `lease_expires_at`, `attempt_count`,
  `accepted_at`, `terminal_at`/`terminal_reason`, plus `run_id`, `session_id`,
  `invocation_id`, `generation`, `runtime`/`harness`/`role`, and an optional
  `source_message_id`. Unique `(agent_id, queue_seq)`.
- **`guidance_items`** — `(batch_id, position)` primary key, contiguous from
  zero, with `item_id`, `from_agent`, `content`, `summary`,
  `injection_options`.

State machine, all transitions compare-and-set inside SQLite transactions:

```
pending ──lease acquired──► leased ──transport submitted──► submitted
   ▲                                                            │
   └──────── lease expired / explicit retry ────────────────────┘
                                                                │
                                            runtime accepts ────► accepted

pending|leased|submitted ──explicit cancel──► cancelled
pending|leased|submitted ──retry budget exhausted──► abandoned
```

Seven operations:

```
enqueue_batch(target, class, items, identity) -> batch_id
claim_next(target, boundary, consumer)        -> batch | none
record_transport_attempt(batch_id, attempt, result)
acknowledge_runtime(batch_id, evidence)       -> accepted | already_accepted | rejected
release_for_retry(batch_id, reason, next_attempt_at)
cancel_batch(batch_id, reason)
recover_expired_leases(now)
```

Claim ordering: at `turn_finished` take the oldest steering batch; if steering
is empty **and** the runtime reports `would_stop`, take the oldest follow-up.
One active lease per agent across both classes. Never claim mid-tool-batch or
after a hard stop.

## The honest limitation

The ADR is careful about this and the plan must stay careful too: **the
strongest available evidence on every current runtime may still fall short of
proving the model accepted the message into its next context.**

- Claude Code: a Teams mailbox read is *mailbox* evidence, not context proof.
- Codex: no native hook carries batch identity today.
- OpenCode: plugin hook success is not prompt acceptance.

So E10 may legitimately land with some runtimes at `inferred`/`unknown`
confidence. That is a correct outcome, not a failure. The rule is that the
adapter must **never upgrade transport success to `runtime_accepted`** — an
unproven batch stays pending or unknown and becomes visible, rather than being
silently marked consumed. Success for this initiative is *ambiguity made
observable*, not a fabricated 100% delivery rate.

## Phases

Derived directly from the design doc's migration plan
(`durable-agent-steering-queue.md:303-316`).

### P1 — Schema and transactional queue API
Add both tables and all seven operations **without changing legacy
`drain_unread` behavior**. Pure addition; nothing routes through it yet.
Concurrency is the hard part: serialized CAS claiming, one lease per agent,
bounded lease deadlines, `recover_expired_leases` on startup.

### P2 — Route producers through `enqueue_batch`
Steering/follow-up producers commit a batch before transport begins. Retain the
`messages` row as a compatibility pointer via `source_message_id`. Replace the
30-second body-hash dedup with identity-based idempotency keys — free-form
messages get a UUID and stay distinct even when bodies match.

### P3 — Separate transport evidence from consumption
`InboxStore::drain_unread` currently emits `message.consumed` before any
runtime accepted anything. Split acknowledgement into `ack_kind="inbox_read"`
(legacy, weak) and `ack_kind="runtime_accepted"` (adapter-proven), each with a
confidence. A transport success alone must never emit the strong event. This is
a correctness fix, not just plumbing — today's consumption metrics are
overstated.

### P4 — Runtime adapters, strongest evidence first
Claude → Codex → OpenCode. Each adapter reports a boundary phase
(`turn_finished` / `would_stop`) and supplies acceptance evidence carrying
`batch_id`, `item_id`, `queue_class`, and invocation generation where the
runtime can echo them. Keep legacy delivery in shadow mode until queue and
ledger projections agree.

### P5 — Observability contract
Extend the existing vocabulary by payload fields — do **not** create a second
message authority. `inbox.state_changed`, `message.delivery`,
`message.consumed` (+ `ack_kind`/confidence), `agent.guidance.delivery`,
`agent_inbox.messages_abandoned`. Update the event registry,
`expected-events.v1.json`, fixtures, and replay projections. Bodies stay local;
ledger carries identities and bounded dimensions only.

### P6 — Retire the in-memory FIFO as authority
Rebuild `AgentInbox` from durable pending/expired rows on restart, then remove
it as an authority **only after** restart, lease, duplicate, retry, and
abandonment gates pass for all supported runtimes.

## Verification

The ADR's nine implementation gates and the design doc's ten-item test matrix
are the acceptance bar. Consolidated:

1. Atomic enqueue, contiguous batch ordering.
2. Concurrent claims for one agent never lease two batches.
3. Independent agents progress concurrently.
4. Steering precedence over follow-up; one-batch atomicity (an adapter cannot
   accept item 0 and leave item 1 pending).
5. Crash/restart recovery of `leased` and `submitted` rows.
6. Idempotent enqueue and acknowledgement; late and duplicate acks handled.
7. Retry sequence preservation, explicit cancellation and abandonment.
8. Queue caps, retention, and authorization enforced.
9. **No `message.consumed` from transport success alone.**
10. Ledger replay produces the same pending/accepted/terminal projection as
    SQLite state.
11. No queue or delivery acknowledgement carries merge or review authority.

```bash
just rust-test        # fast loop
just test             # full workspace including integration targets
just validate-observability-contracts   # P5
```

## Chainlink

**Milestone 27 — M11: Durable agent steering queue.** Created 2026-08-12.

| Phase | Epic | Tasks |
|---|---|---|
| P1 Schema + queue API | **#764** | #770–775 |
| P2 Producer routing | #765 | #776–779 |
| P3 Evidence separation | #766 | #780–783 |
| P4 Runtime adapters | #767 | #784–788 |
| P5 Observability | #768 | #789–792 |
| P6 Retire in-memory authority | #769 | #793–796 |

`#764` is blocked by `#723` (E6). Since E6 is closed that gate is already
satisfied, so the frontier is `#764` / `#770`.

**Sequencing is strictly linear.** All 27 tasks form a single chain
`#770 → #771 → … → #796` that runs *through* epic boundaries, and the epics are
chained `#764 → #765 → … → #769`. Exactly one task is workable at a time.

This double chaining is deliberate: `chainlink issue ready` does **not** inherit
a parent epic's blocked state, so without cross-epic task blockers the first
task of every blocked epic would surface as workable.

```bash
chainlink issue ready    # expect exactly one task
chainlink issue blocked  # full chain
```

## Non-goals

- Owning any runtime's active model loop. Explicitly rejected in the ADR.
- New agent roles. `dev` + `worker` stay as they are.
- Exactly-once delivery. At-least-once with observable duplicates; the three
  harnesses share no native idempotency contract.
- Letting guidance alter review, CI, merge, or gate state. Ever.
