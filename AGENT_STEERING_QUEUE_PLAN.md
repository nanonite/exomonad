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

Derived from the design doc's migration plan
(`durable-agent-steering-queue.md:303-316`).

**Status as of 2026-08-12: P1–P4 are complete.** The sections below record what
landed, so this document is not read as describing unbuilt work.

### P1 — Schema and transactional queue API ✅ DONE (E7 / #764)
`rust/exomonad-core/src/services/guidance_queue.rs` implements all seven
operations and all six states, exported from `services/mod.rs`.

### P2 — Route producers through `enqueue_batch` ✅ DONE (E8 / #765)
`delivery.rs` and `guidance_shadow.rs` now call `enqueue_batch`. Identity-based
idempotency landed in `agent_inbox.rs` (`dedup_key.idempotency_key`), replacing
the 30-second body-hash window.

### P3 — Separate transport evidence from consumption ✅ DONE (E9 / #766)
`ack_kind` distinguishes `inbox_read` from `runtime_accepted`;
`runtime_accepted` is emitted only from the acknowledgement path
(`guidance_queue.rs:661`) and is covered by a test. The overstated-consumption
bug is fixed.

### P4 — Runtime adapters ✅ DONE (E10 / #767)
`guidance_adapters.rs` provides the `GuidanceRuntimeAdapter` trait with
`turn_finished`/`would_stop` boundaries; Claude Teams, Codex, and OpenCode
adapters landed. `guidance_shadow.rs` provides the shadow comparison harness
(`compare_batch`, `in_sync`, `diff_count`).

---

### P5 — Observability contract ⬅ CURRENT (E11 / #768, tasks #789-792)

**Narrower than originally scoped.** The five event *names* are already in
`docs/observability/event-registry.json`: `inbox.state_changed`,
`message.delivery`, `message.consumed`, `agent.guidance.delivery`,
`agent_inbox.messages_abandoned`. Do not re-add them.

The actual gaps:

- **Payload identity fields.** The registry has essentially no declaration of
  `batch_id`, `item_id`, `queue_class`, `queue_seq`, `consumer`, etc. Extend
  payloads, do not mint new event identities.
- **Denominator rules.** `expected-events.v1.json` has *zero* guidance-related
  rules. A durable enqueue must expect eventual acceptance or explicit
  abandonment; a transport success with neither stays pending/unknown.
- **Replay projection.** Ledger replay must reconstruct
  pending/accepted/terminal identically to SQLite state.
- **Body-leakage check.** Bodies stay in L1/L2; aggregate projections carry
  identities and bounded dimensions only.

### P6 — Retire the in-memory FIFO as authority (E12 / #769, tasks #793-796)

**Scope needs assessment before removal — two distinct things share the name.**

- `services/agent_inbox.rs` still holds a `VecDeque`. This is the candidate for
  demotion. Whether it is still an *authority* or already reduced to a
  transport cache by P2 is the first question E12.1 must answer.
- `services/continuation/{mod,adapters,renderer}.rs` reference
  `AgentInboxSummary`, which is a **read-side summary type for the continuation
  brief**, not the delivery FIFO. It is not in scope for removal.

Rebuild from durable pending/expired rows on restart, then remove the authority
**only after** restart, lease, duplicate, retry, and abandonment gates pass for
all three runtimes.

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

| Phase | Epic | Tasks | State |
|---|---|---|---|
| P1 Schema + queue API | #764 | #770–775 | ✅ closed |
| P2 Producer routing | #765 | #776–779 | ✅ closed |
| P3 Evidence separation | #766 | #780–783 | ✅ closed |
| P4 Runtime adapters | #767 | #784–788 | ✅ closed |
| P5 Observability | **#768** | #789–792 | ⬅ frontier |
| P6 Retire in-memory authority | #769 | #793–796 | open |

Frontier is `#768` / `#789`. M12 (operator control plane, `#797–799`,
tasks `#800–815`) is chained behind `#769`.

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
