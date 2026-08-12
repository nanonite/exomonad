# ExoMonad observability contracts

These files freeze the Phase 0 contracts used by the append-only observability plan:

- `event-registry.json` defines the versioned envelope, event namespaces, payload classes,
  emitter sources, and compatibility rules.
- `expected-events.v1.json` defines deterministic denominator rules for required workflow
  transitions.
- `fixtures/phase0-contract-fixtures.json` provides non-sensitive multi-harness coverage
  for identity, delivery, review, gaps, state reconstruction, privacy, and provenance.
- `scripts/validate_observability_contracts.py` validates all three artifacts with only the
  Python standard library.

Run the contract gate from the repository root:

```bash
just validate-observability-contracts
```

The gate must pass before an emitter, importer, exporter, detector, or architecture
comparison is considered measurement-ready. Changes to event names, required envelope
fields, payload classes, or expected-event rules require a versioned contract update and
fixture coverage in the same change.

The registry permits sensitive evidence in local L1, L2, and L3 layers. The L4 compile
step is the share boundary: it selects allowlisted dimensions and must not emit raw
payloads, transcripts, reasoning, paths, secrets, or stable source identifiers.

## Retired harness aggregation

Effective 2026-08-10, historical rows from the retired harness remain compilable but
aggregate under `provider`, `runtime`, and `harness` as `other`. This preserves the
interpretation of Failure Atlas artifacts produced before the allowlist change; historical
ledger segments and atlas databases are unchanged.


## Runtime MVP-B paths

New sessions write required structured telemetry automatically. The authoritative
boundary is locked in .exo/session.json; swarm continuity remains .exo/run_id.
The canonical append-only ledger is .exo/ledger/segments/*.jsonl, while the
per-agent .exo/events and .exo/logs JSONL views remain compatibility inputs.

.exo/sink-health.json is the durable fallback for accepted/rejected event counts,
write failures, the last successful sequence, and complete/partial/unknown status.
Its counters are tagged with the active session ID; health from another session is
unknown and cannot classify the current session.
It is local evidence and is imported into L2 as sink.health. Mutable session,
memory, and inbox changes emit session.state_changed, memory.state_changed, and
inbox.state_changed events into L1. Human-readable --verbose tracing does not
control these structured events.

## Runtime MVP-C paths

Corrections are append-only event.superseded events. L2 exposes the
rebuildable resolved_events view for readers that should exclude superseded
observations while retaining every correction in the raw evidence.

Closed ledger segments rotate by size or age. Retention is whole-segment only:

    exomonad logs drop-segments --dry-run
    exomonad logs drop-segments --older-than-seconds 2592000

Dry runs return segment paths, byte counts, and SHA-256 fingerprints. A
non-dry-run records ledger.segment.dropped in the surviving segment before
dropping the expired file. The current segment is never eligible. Local
crypto-shredding remains an optional deployment-level policy; the default
retention operation drops complete segments.

Durable delivery rows include message.delivery, message.consumed,
agent_inbox.duplicates_dropped, and agent_inbox.messages_abandoned.
expected-events.v1.json is embedded in the core and reconciled by session;
missing required outcomes remain denominator gaps and produce partial or
unknown completeness rather than zero-valued success.

## Runtime MVP-D export

After importing a session into L2, compile the only shareable artifact with:

    exomonad logs export --mode aggregate

The command writes analysis.json, manifest.json, and privacy-report.json under
.exo/analysis/export by default. The Python compiler reads an explicit
allowlisted view of L2, computes aggregate metrics and pointer-only exemplars,
then runs a denylist scanner as a backstop. It never reads local_payload_json
for output and has no internal-events export mode.

The manifest carries database/source hashes plus query, code, method, detector,
and experiment revisions. Every metric carries the same provenance object.
Fixed input and revisions produce byte-stable analysis.json output.

## Runtime MVP-E measurement

Run the local measurement pipeline with an explicit preregistration:

    exomonad logs measure --preregistration docs/observability/preregistration.example.json

It writes signals.json, incidents.json, adjudication.json, and
measurement.json under .exo/analysis/measurement. Mechanical detectors are
high-recall candidates; incidents are clusters; adjudication records judge
model, prompt revision, label schema, seed, and Wilson intervals. Judge names are
metadata only: provide independent per-judge labels with --labels labels.json.
Without complete per-judge labels, precision remains pending and cannot be
published; single-judge precision is marked provisional.

The current semantic detector set includes `missing_review`, `missing_ci`,
`stale_head_approval`, `review_timeout`, `review_loop_stall`, and
`merge_without_gates`. These are local mechanical candidates: they require
adjudication and never authorize a review, CI, or merge decision.

The measurement gate requires session-level units, an explicit baseline and
treatment, assignment method, fixed primary outcome/denominator, missingness
rule, stopping rule, and all declared confound controls. Provider/runtime/
harness group-bys are permanently descriptive. Add --require-ready when a
caller must fail unless a preregistered controlled contrast is valid.

## Compatibility smoke matrix

The Phase 0 fixture gate covers a mixed session containing Claude, Codex,
and a custom Python loop, plus resume/retry delivery, review/merge,
partial-sink/gap, state reconstruction/correction, and privacy-boundary cases.
These fixtures exercise the shared normalized event graph rather than
transcript-specific adapters. Run the full local handoff checks with:

    just validate-observability-contracts
    just test-observability-export
    just test-observability-measurement
