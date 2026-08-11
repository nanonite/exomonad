# TL Loop Event Bridge

## Decision

The Python TL loop tails the existing immutable ledger at
`.exo/ledger/segments/` by global `run_seq`. `tl_loop/events/reader.py` reads
segments in lexical order, applies the Rust ledger's supersession and sequence
semantics, and projects mapped rows through the typed M2.4 envelope.
`tl_loop/events/bridge.py` adds lifecycle logging around that projection and
the bounded in-process queue. It does not create a durable log or write ledger
segments.

The server's existing watcher and poller event paths remain responsible for
emitting the canonical ledger rows. Review and CI rows carry the producer's
known `head_sha`, so the bridge preserves the reviewed head without querying a
second service or reconstructing it from another event.

## Evidence

- `EventLog::append` already writes the canonical ledger through
  `LedgerWriter`, assigns global `run_seq`, fsyncs the row, and maintains the
  compatibility JSONL view.
- `LedgerWriter::read_events` enumerates immutable segments lexically;
  `read_resolved_events` applies supersession; and `sequence_status` defines
  `unknown`, `partial`, and `complete` sequence states.
- `worktree_event_watcher` and the hibernated `github_poller` already use
  `HasEventLog` and emit PR, review, CI, and agent events.

## Rejected alternatives

### New UDS event stream

Rejected. The UDS is a request/response boundary for MCP calls, and a push
stream would add a new I/O surface when events are already durable and
sequence-numbered on disk. A future pull endpoint is only a fallback if
read-only ledger tailing proves insufficient.

### Inbox or inbox database tailing

Rejected. Inbox data is worker/human-facing routing and poke text, not the
structured PR number, reviewed head, CI status, and review state required by
the TL loop. The inbox remains a delivery mechanism, not an event source.
