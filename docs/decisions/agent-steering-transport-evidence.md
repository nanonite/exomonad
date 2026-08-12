# Agent steering transport evidence

**Status:** Evidence record for Chainlink #752

**Date:** 2026-08-12

## Scope

ExoMonad does not own the Claude Code, Codex, or OpenCode response loop. A
steering message therefore travels through one of three runtime-facing paths:

1. Claude Code's Teams inbox;
2. an HTTP request over a per-agent Unix socket; or
3. tmux input injection, normally behind ExoMonad's per-agent FIFO.

The durable SQLite inbox and the immutable ledger sit beside those transports.
They are not a fourth runtime transport: the SQLite inbox is the recovery
record, while the ledger is the evidence and workflow-authority boundary.
Successful steering delivery never authorizes a merge or changes review state.

## Current routing

`services/delivery.rs::deliver_to_agent` records the message in the durable
inbox first. It then uses this order:

| Recipient/path | Delivery behavior | Evidence of success |
|---|---|---|
| Claude with a resolved Teams registration | Teams inbox write, up to three attempts with 100 ms fixed backoff | The Teams write returns a timestamp; a background verifier checks the native inbox read state for up to 30 seconds |
| Claude Teams write failure | For ExoMonad-owned recipients, enqueue the durable message for tmux fallback; Tier-2 native recipients have no tmux fallback | `message.delivery` and `agent.guidance.delivery` rows identify the failed Teams attempt and any fallback |
| Agent with `notify.sock` | One HTTP `/notify` request over UDS with a five-second timeout | A successful HTTP response; timeout or non-2xx falls through to tmux |
| Codex/OpenCode or failed UDS | Resolve current routing/pane, verify liveness, enqueue the per-agent FIFO, and inject one message at a time | The FIFO consumer records each attempt; successful injection completes the queue item |

The FIFO has a 32-message hard cap, a single active consumer, a 30-second
deduplication window, and up to eight injection attempts with capped
exponential backoff. An item that exhausts those attempts emits
`agent_inbox.messages_abandoned`. The durable SQLite row remains the recovery
record and can be surfaced by the inbox poke path.

`services/inbox_watcher.rs` is a separate Claude Teams compatibility consumer
for synthetic members. It polls the Teams file every 500 ms, snapshots the
initial message count, and injects only the appended suffix. It logs an
`inbox.message` observation before calling tmux injection. Its cursor advances
even when injection fails, so a failed watcher injection can be lost unless
the source Teams message is observed again or another delivery path supplies
it.

`services/state_mirror.rs` mirrors mutable inbox and memory operations into the
ledger on a fail-open basis. Runtime state remains authoritative when the
mirror cannot open or append; sink-health telemetry records the failure. A
missing mirror row is therefore an observability gap, not evidence that the
state mutation did not happen.

## Failure modes by transport

### Teams inbox

- A missing or stale `TeamRegistry` entry prevents the write and routes the
  message to the next path when the recipient has an ExoMonad-owned pane.
- A successful file write is only producer-side acceptance. If the native
  InboxPoller is dormant, slow, or changes its on-disk protocol, the verifier
  can time out.
- When the verifier times out for an ExoMonad-owned Claude recipient, tmux
  fallback may deliver the same message after the Teams write eventually
  becomes visible. This is a possible duplicate, not a safe proof of loss.
- Tier-2 Claude recipients have no ExoMonad pane fallback. An unread Teams
  message remains dependent on the native poller.
- The Teams writer retries transient write errors three times, but the
  verifier does not make the write transactional with runtime consumption.

### HTTP over UDS

- The socket is attempted only when `.exo/agents/<agent>/notify.sock` exists.
- Connect, request, response, and body-read failures are bounded by one
  five-second request timeout. There is no UDS retry loop in this path.
- A failed or timed-out request falls through to tmux. If the agent accepted
  the request but the response was lost, the fallback can duplicate the
  message; if the request never arrived, the fallback is the recovery path.
- A missing socket is not itself a delivery failure: routing continues to the
  tmux FIFO.

### tmux and the ExoMonad FIFO

- Stale or unresolvable `routing.json`, missing panes, and failed pane-liveness
  checks prevent enqueueing and return the durable-inbox result to the caller.
- The FIFO serializes injection, but it cannot prove that the external
  harness accepted or acted on the submitted text. This is the root cause
  identified by the cross-runtime inbox ADR; a tmux lock only serializes
  writers.
- A failed injection is retried eight times. After the eighth failure the
  in-memory queue item is abandoned and emits an explicit abandonment event.
- Deduplication suppresses repeated structured notifications within 30 seconds
  and emits `agent_inbox.duplicates_dropped`. That prevents some duplicates,
  but it is not a durable idempotency key across process restarts.
- Queue-cap rejection and process restart can leave the SQLite durable row
  unread while the transient FIFO item is gone. The inbox/poke path is the
  recovery mechanism, not a proof that the external harness consumed it.

## Ledger evidence

The measured evidence below comes from the canonical ledger segment present in
the workspace at:

```text
rust/exomonad-core/.exo/ledger/segments/segment-000000000000.jsonl
```
The segment had SHA-256
`b6bc40ebaabd561b48354f5ae5034c185f4b0306520f9ce10ef4f5dd6bd5da83`, 3,309
rows with `run_seq` 1 through 3,309, and event times from
`2026-08-10T04:47:48.263Z` through `2026-08-12T13:25:14.618Z`.

The following counts were obtained by grouping the immutable rows by `type`
and the transport fields inside `data`:

| Ledger evidence | Count | Rate interpretation |
|---|---:|---|
| `agent.guidance.delivery`, `channel=exact_tmux`, `outcome=failed` | 554 | 554/554 observed exact-pane attempts failed (100%) in this evidence set |
| `agent.guidance.delivery`, `channel=tmux_injection`, `outcome=success` | 320 | 320 of 1,280 tmux-injection outcomes succeeded (25%) |
| `agent.guidance.delivery`, `channel=tmux_injection`, `outcome=failed` | 960 | 960 of 1,280 tmux-injection outcomes failed (75%) |
| `message.delivery`, `method=agent_inbox_tmux`, `outcome=success` | 308 | 308 of 1,232 FIFO attempt rows succeeded (25%) |
| `message.delivery`, `method=agent_inbox_tmux`, `outcome=failed` | 924 | 924 of 1,232 FIFO attempt rows failed (75%) |
| `agent_inbox.duplicates_dropped` | 154 | Structured duplicate suppressions observed; not message loss |
| `agent_inbox.messages_abandoned` | 77 | FIFO items exhausted all eight attempts |
| `message.consumed` | 12 | Durable inbox messages marked consumed |

These are ledger-observed attempt and terminal-event rates, not a production
unique-message success rate. The segment was produced by repeated integration
and failure-path runs: `run_id` and `session_id` are null, and the transient
`message_id` values are reused between test fixtures. Consequently, joining
attempts by `message_id` would falsely merge independent messages. The
ledger proves the failure modes and their emitted telemetry, but this fixture
cannot prove a population-level end-to-end drop rate. A production rate needs
non-reused message identity plus run/session/invocation correlation.

The evidence also shows why delivery must remain separate from workflow
authority: `agent.guidance.delivery` rows carry `delivery_vs_authoritative`
`delivery_only`, while review, CI, and merge decisions remain ledger-projected
workflow events.

## Consequences for the loop-ownership decision

The current transports can report producer acceptance, fallback, retry,
abandonment, deduplication, and durable consumption, but none can inject a
message directly into the next model context for all three harnesses. Teams
delegates that ownership to Claude Code; UDS and tmux delegate it to the target
runtime. Improving transport retries reduces loss, but does not remove the
fundamental acknowledgement and duplicate ambiguity. That question is left to
the parent E6 tasks that study queue ownership and per-harness loop ownership.

## Reproducible ledger query

From the repository root, the aggregate counts above can be regenerated with:

```bash
ledger=rust/exomonad-core/.exo/ledger/segments/segment-000000000000.jsonl
jq -r 'select(.type == "agent.guidance.delivery")
  | [.data.channel, .data.outcome] | @tsv' "$ledger" | sort | uniq -c
jq -r 'select(.type == "message.delivery")
  | [.data.method, .data.outcome] | @tsv' "$ledger" | sort | uniq -c
for kind in agent_inbox.duplicates_dropped agent_inbox.messages_abandoned message.consumed; do
  jq -c "select(.type == \"$kind\")" "$ledger" | wc -l
done
```
