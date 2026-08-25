# Publication ownership succession across session recreation

## Decision

`--recreate` preserves the ledger-owned publication registry and agent
identity. When a same-owner invocation is started again, the controller
appends an `invocation_succession` record to each matching publication. The
original `invocation_id` is never rewritten.

Adoption is allowed only when the agent identity, head branch, base branch,
slice ID, provenance, PR number, and head SHA all match. The invocation ID is
the sole field that may differ. A succession is append-only and idempotent by
`from_invocation_id`/`to_invocation_id`.

The watcher accepts a current invocation only when it is reachable from the
publication invocation through the succession chain. An unprovable mismatch
is surfaced through the effect response and parks the slice with
`publication_ownership_unresolved` behind a stable human gate. The controller
never edits `published-heads.json` directly; registry reads and writes stay
behind the Rust effect boundary.

## Handoff provenance resolution

The publication registry records the invocation that originally filed a head,
while dispatch_invocation_id identifies the invocation currently being
reconciled. Those values can differ after a same-owner succession. The
existing precedence in _publication_from_watcher is intentional: the
current dispatch identity wins, followed by an already-bound publication, and
then the host publication record for a first recovery. This preserves the
current-owner handoff without rewriting the registry's original evidence.

The reconciliation backfill has one provenance route: it reads
PublicationBinding.invocation_id after _publication_from_watcher has folded
the host observation into the binding. It does not read the raw watcher
record a second time. The raw record is still used by the separate
tl.handoff_reconciled diagnostic payload when a handoff cannot be built; that
read explains a missing-evidence report and does not resolve handoff state.

## Rationale

Restarting a process does not create a new publication owner. Recording an
explicit succession preserves the evidence needed to distinguish a legitimate
session restart from a branch, slice, or provenance mismatch, while allowing
repeated `--recreate` runs to converge without duplicate reviewers, PRs, or
budget charges.
