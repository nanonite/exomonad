# ADR: TL shadow parity gate

Status: Proposed, awaiting human operator sign-off
Date: 2026-08-11
Scope: M3.4 Shadow parity acceptance gate (#681)

## Context

M3.3 captured a live two-slice TL trajectory beside the read-only shadow loop
in commit `12aedfdb`. The committed replay pair is
`tests/e2e/tl-loop-shadow/fixtures/intended.jsonl` and
`tests/e2e/tl-loop-shadow/fixtures/actual.jsonl`; its report is
`tests/e2e/tl-loop-shadow/fixtures/tl-loop-shadow-report-replay.md`.

The replay report contains zero MATCH rows, three DIVERGENT rows, one EXTRA
row, and zero MISSING rows. The report now gives a named classification and
rationale for every non-MATCH row. The M2.7 coverage audit marks the
`agent.spawned` and `pr.merged` event paths as covered.

## Triage result

All four non-MATCH rows are accepted-intentional-differences:

1. The child fan-out DIVERGENT row pairs one shadow `dispatch` with the live
   `fork_wave`. The live call carries both children in one transport action;
   the shadow loop records one deterministic intent per child. The second
   shadow dispatch is consequently the EXTRA row. Both children remain
   represented, so this is action-granularity variance rather than lost state.
2. The `shadow-slice-a` merge DIVERGENT row compares shadow `merge` with live
   `merge_pr`. The target and normalized PR number agree; the difference is
   the intentional distinction between a non-executing judgment and its
   transport tool.
3. The `shadow-slice-b` merge DIVERGENT row has the same accepted rationale
   as `shadow-slice-a`: target and normalized PR number agree, while action
   names represent different abstraction layers.

No shadow bug or event-coverage gap was found in this replay. The external
provider spend-limit failure during a later verification attempt is recorded
as an environment limitation and is not used as parity evidence; the original
M3.3 run passed before this gate was opened.

## Decision

The M3 read-only trajectory is parity-acceptable for the captured replay after
the explicit classifications above. There are zero untriaged rows.

M5 is **NO-GO pending human sign-off**. No M5 task may begin until the human
operator accepts these three intentional differences and the ADR is updated
with the sign-off below.

## Human sign-off

- Operator: pending
- Decision: pending approval or rejection
- Date: pending
- Notes: The operator must explicitly confirm whether the accepted
  action-granularity and abstraction-vocabulary differences are sufficient for
  the M5 go/no-go decision.
