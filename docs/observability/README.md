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
