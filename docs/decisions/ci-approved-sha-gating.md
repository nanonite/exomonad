# CI Gate Requires the Reviewer-Approved SHA

Date: 2026-05-19

## Decision

The merge gate treats Forgejo CI as valid only for the exact commit SHA that the
reviewer approved. The watcher reads PR head state and reviews from Forgejo, then
polls Forgejo commit statuses for the approved head SHA.

## Invariant

A PR can merge only when all review gates pass and Forgejo reports `success` or
`neutral` for the approved head SHA. A successful status for an older SHA on the
same branch is ignored.

## Consequences

- A new push invalidates the prior review approval because Forgejo reviews are
  tied to the previous head commit.
- CI still runs on every push. The watcher can use it as a cheap dev-loop signal,
  but merge never trusts branch-only or stale status.
