# TL shadow divergence report: `replay-fixture`

This committed pair is the deterministic replay shape for M5.5. A live run
writes its report below the temporary E2E work directory.

## Counts

| bucket | count |
|---|---:|
| MATCH | 0 |
| DIVERGENT | 4 |
| EXTRA | 0 |
| MISSING | 0 |

The fixture deliberately preserves both sides, including both direct leaf
calls, so replay tests cannot hide an action by dropping noisy or unmatched rows.

## Triage

Every non-MATCH row is listed below. In this report format, `EXTRA` means an
unmatched shadow action; an actual-only action would be `MISSING`.

### Child dispatch — DIVERGENT

Classification: accepted-intentional-difference. The shadow loop receives one
`ChildSpawned` event per child and records one deterministic `dispatch` intent
for each child. The fixture actual stream records the equivalent work as one
`spawn_leaf` tool call per slice, so both child rows are paired as DIVERGENT.
No child is lost and the event-coverage audit marks `agent.spawned` as covered;
this is a difference in action vocabulary, not a shadow state or coverage bug.
Owner: M3 shadow
maintainer. Status: accepted for the read-only M3 gate.

### Merge `shadow-slice-a` — DIVERGENT

Classification: accepted-intentional-difference. The shadow loop records its
non-executing judgment as `merge`, while the fixture actual stream records the
transport tool `merge_pr`. Both rows target `shadow-slice-a` and carry the same normalized PR
number, so the state transition and selected PR agree even though the
programmatic action vocabulary is intentionally abstracted. Owner: M3 shadow
maintainer. Status: accepted for the read-only M3 gate.

### Merge `shadow-slice-b` — DIVERGENT

Classification: accepted-intentional-difference. The shadow loop records its
non-executing judgment as `merge`, while the fixture actual stream records the
transport tool `merge_pr`. Both rows target `shadow-slice-b` and carry the same normalized PR
number, so the state transition and selected PR agree even though the
programmatic action vocabulary is intentionally abstracted. Owner: M3 shadow
maintainer. Status: accepted for the read-only M3 gate.

## Gate status

All four non-MATCH rows are triaged above. No event-coverage gap or shadow bug
was found in this replay. Human operator sign-off remains pending; M5 is a
NO-GO until the ADR records that sign-off.
