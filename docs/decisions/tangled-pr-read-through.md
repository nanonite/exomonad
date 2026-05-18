# Use Tangled appview for local PR read-through

**Status:** Implemented

## Context

ExoMonad stores local pull request state in `.exo/prs.json`. That file is the
canonical local cache for merge gates, reviewer assignment, and branch lookups.
When a process starts with an empty or stale cache, the previous local fallback
could only report "not found" even if the PR exists in Tangled.

Tangled records PRs in appview state derived from knot records and spindle CI
events. The appview is therefore the right read side for GitHub-equivalent
lookups such as "get pull request by number" and "get pull request for branch".

## Decision

Keep `.exo/prs.json` canonical. Add a read-through fallback that only runs after
the local cache misses:

- `file_pr.local_pr_get` first checks `.exo/prs.json`, then calls Tangled
  appview.
- `file_pr.local_pr_get_for_branch` follows the same order.
- Tangled reads are disabled unless both `tangled_appview_url` and
  `tangled_owner_did` are configured.

The initial appview contract is an XRPC-shaped HTTP read API:

- `GET /xrpc/sh.tangled.repo.getPull?repo=<owner_did>&pull=<number>`
- `GET /xrpc/sh.tangled.repo.getPullForBranch?repo=<owner_did>&branch=<branch>`

Responses are mapped into the existing `LocalPrResponse` wire shape so Haskell
WASM tools do not need a second PR state model.

## Consequences

The local cache remains the only writer-owned PR registry. Tangled appview is a
read fallback, not a bidirectional sync source.

If the appview is unavailable or returns an invalid/not-found response, ExoMonad
keeps the previous behavior and returns a not-found `LocalPrResponse`. This keeps
merge and stop-hook flows tolerant of incomplete Tangled configuration.
