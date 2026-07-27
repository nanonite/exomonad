# Review-loop Chainlink audit

Snapshot date: 2026-07-27

Source: `/home/goya/storage/RustDebuggerRepo`, queried with:

```text
chainlink issue list --json --status all --label review-stuck
```

## Inventory

| Measure | Count |
|---|---:|
| `review-stuck` issues | 123 |
| Open | 106 |
| Closed | 17 |
| Watcher-generated title/description format | 122 |
| Manually labeled/non-watcher issue | 1 (`#51`) |
| Unique `(PR, head SHA, classification)` keys | 66 |
| Redundant rows beyond unique keys | 57 |
| Distinct PRs represented | 57 |
| Exact duplicate groups found | 29 |

The machine-readable duplicate map is [review-loop-audit-2026-07-27.json](review-loop-audit-2026-07-27.json). It chooses the oldest-created watcher issue as a proposed canonical issue and records the later rows as duplicates. This is a proposal only.

## Safety status

No Chainlink issue was closed, deleted, relabeled, commented on, or otherwise mutated during this audit. No cleanup should occur until a human approves the canonical mapping and confirms that the watcher fix is deployed and no longer creates new issues.

The manual issue #51 is excluded from duplicate cleanup because it does not use the watcher-generated schema. The PR/head/classification key is the only duplicate criterion; same-PR issues with different heads or classifications must remain separate.

## Recommended next action

Have a human review the JSON mapping. If approved, reconcile only the listed `duplicate_issue_ids`, preserve each `canonical_issue_id`, and record the mapping in Chainlink comments. Keep all distinct keys and the manual issue untouched.
