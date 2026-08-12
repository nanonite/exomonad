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

## Target review-gate event contract

The TL loop consumes these canonical source events for PR review convergence.
Each row is an immutable ledger envelope ordered by global `run_seq`; the
event's `head_sha` is the identity binding for review and CI evidence. The
bridge preserves the producer's value and never reconstructs a missing head
from another event.

| Event | Meaning | Required review-gate context |
| --- | --- | --- |
| `pr.filed` | A PR is created or first observed for a slice | PR number, branch/base, and the filed `head_sha` |
| `pr.updated` / `pr.head_changed` | Known PR metadata changes; a changed `head_sha` starts a new review/CI state | PR number and the new `head_sha`; `pr.head_changed` names the semantic transition, while `pr.updated` is the current wire event |
| `pr.review` | Reviewer evidence or a review-state transition is observed | PR number, `head_sha`, review kind/state, findings or notification |
| `ci.status_changed` | CI status changes for a PR head | PR number, `head_sha`, and status (`pending`, `success`, `failure`, `neutral`, or `unknown`) |
| `pr.merged` | The PR merge succeeds | PR number and the merged `head_sha` |
| `pr.merge_failed` | A merge attempt fails | PR number, attempted `head_sha`, and failure details |

`merge_ready` is derived state, not a source event. The TL may derive merge
eligibility only when binding reviewer evidence has been adjudicated GO, CI is
`success` or `neutral` for that same `head_sha`, and that head still equals
the live PR head. A legacy `MergeReady` notification may remain as a compatibility
signal during migration, but it is not an authority and is not required for
the bridge contract. Review timeout, missing CI, stale evidence, and a head
mismatch never imply merge readiness.

The event set above is the review-gate contract, not an exhaustive list of
ledger events. Agent lifecycle, inbox, hook, and watcher-observation events
remain valid ledger rows but do not authorize a review or merge decision.

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
