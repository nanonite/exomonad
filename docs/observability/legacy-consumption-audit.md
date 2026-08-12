# Legacy consumption audit

This audit records the production paths that emit `message.consumed` and the
fact each path actually proves. It is the E9.1 baseline for separating durable
inbox reads from runtime acceptance.

## Current emitters

| Emitter | Production caller | State mutation | Evidence actually available | Current claim |
|---|---|---|---|---|
| `InboxStore::drain_unread` in `rust/exomonad-core/src/services/inbox_store.rs` | `rust/exomonad-core/src/handlers/inbox.rs::InboxEffects::check` | Selects unread `messages`, sets `read_at`, updates `agent_inbox_meta.last_check_inbox_at`, then commits | The caller read and returned the legacy SQLite rows. No runtime boundary, transport acknowledgement, invocation, generation, batch, or item evidence is supplied | Emits `message.consumed` with `outcome="consumed"`; this currently overstates runtime consumption |
| `InboxStore::drain_unread` compatibility/test callers | Tests and compatibility code that directly drain the legacy store | Same as above | Same durable-read evidence only | Same legacy event shape; not a runtime acceptance signal |
| `InboxStore::peek_unnotified` | No `message.consumed` emission | Sets `notified_at` only | Notification/peek observation | Correctly emits no consumption event |
| `InboxStore::write_message` / `enqueue_batch_with_compatibility` | Delivery producers | Inserts the compatibility row and/or durable batch | Durable persistence and transport preparation only | Emits `message.delivery`, not `message.consumed` |
| `InboxStore::acknowledge_runtime` in `rust/exomonad-core/src/services/guidance_queue.rs` | Runtime guidance adapter boundary | Atomically changes the exact batch to `accepted` and clears its lease | Exact `batch_id`, normalized target, queue class, complete item ID list, consumer, invocation, generation, and `AcceptanceConfidence::Exact` | Emits `message.consumed` with `batch_id`, `ack_kind`, `confidence`, and item count; this is the authoritative positive-acceptance path |

The only production caller of `drain_unread` is the `inbox.check` effect
handler. `drain_unread` is also used in tests and compatibility assertions; it
must remain available as a human/worker inbox surface while its event is
classified as a weak legacy acknowledgement.

## What the legacy event does not prove

Calling `inbox.check` proves that ExoMonad selected the rows and marked them
read in SQLite. It does not prove that the target runtime:

- reached a safe turn boundary;
- received the message through Teams, UDS, tmux, or another transport;
- accepted the exact batch or item set;
- associated the message with the current invocation or generation; or
- used the message in its next model context.

Therefore a legacy drain must not satisfy a runtime-acceptance denominator or
authorize review, CI, merge, retry suppression, or workflow progress. A
transport success is also insufficient: it remains delivery evidence until an
adapter provides positive acceptance evidence.

## Required event distinction

The legacy path should be represented as:

```json
{
  "event_type": "message.consumed",
  "ack_kind": "inbox_read",
  "confidence": "unknown"
}
```

The guidance queue acceptance path should be represented as:

```json
{
  "event_type": "message.consumed",
  "ack_kind": "runtime_accepted",
  "confidence": "exact",
  "batch_id": "...",
  "item_ids": ["..."]
}
```

The payload examples are semantic contracts; local message bodies remain in
the local inbox/queue stores and are not copied into shared observability
events.

## Follow-on work

- E9.2 (#781) adds `ack_kind` and `confidence` to every acknowledgement.
- E9.3 (#782) ensures transport success cannot emit `runtime_accepted`.
- E9.4 (#783) keeps batches without acceptance evidence visible as
  `pending`/`unknown` rather than silently consumed. The durable batch
  projection reports `acceptance_confidence=unknown` until exact acceptance;
  transport and rejected-ack observations carry the same explicit unknown
  classification.
