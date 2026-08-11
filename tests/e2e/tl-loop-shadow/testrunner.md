# M3.3 live shadow trajectory capture

The process companion runs the shadow loop directly; this document is the
operator-facing test plan and capture contract.

## Required trajectory

1. The root TL creates a team and uses exactly two direct `spawn_leaf` calls
   to spawn `shadow-slice-a` and `shadow-slice-b`.
2. Each leaf makes one small, independent change, commits it, pushes its
   branch, and files a PR.
3. The root TL runs the normal review loop for both PRs, including reviewer
   approval, and merges both PRs through the MCP tool.
4. The process companion tails the same immutable ledger segments while the
   live TL is running. It owns no agent identity and has no write-capable tool
   client.

## Assertions

- `artifacts/intended.jsonl` contains the shadow loop's durable intended
  actions.
- `artifacts/actual.jsonl` contains server-observed `tool.called` actions.
- The generated report is present even when the real trajectory exposes a
  divergence for M3.4 to triage.
- `metadata.json` reports zero shadow mutation-attributable calls.
- No ledger segment, Atlas database, or scratch repository outside the test
  work directory is modified.

The captured pair is retained as the replay input for M5.5. The committed
fixture under `fixtures/` is a stable shape check; each live run writes its
fresh capture only below the temporary E2E work directory.
