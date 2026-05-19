# CI Gate Requires the Reviewer-Approved SHA

Date: 2026-05-19

## Decision

The merge gate treats spindle CI as valid only for the exact commit SHA that the
reviewer approved. CI status is stored by `(branch, sha)`, and `.exo/prs.json`
records `approved_at_sha` when the review state transitions to `approved`.

## Invariant

A PR can merge only when all review gates pass and spindle reports `success` or
`neutral` for `(head_branch, approved_at_sha)`. A successful status for an older
SHA on the same branch is ignored.

## Consequences

- Knot pipeline events must carry `triggerMetadata.push.newSha` into the local
  pipeline map so later spindle status events can be correlated to a SHA.
- A new push clears `approved_at_sha`; the approval no longer applies to the new
  head commit.
- Existing PR entries without `approved_at_sha` use `last_head_sha` as a
  migration fallback. Once live registries have been rewritten by the watcher,
  that fallback can be removed.
- CI still runs on every push. The watcher can use it as a cheap dev-loop signal,
  but merge never trusts branch-only or stale status.
