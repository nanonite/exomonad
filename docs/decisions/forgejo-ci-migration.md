# Forgejo CI Migration

**Date:** 2026-05-21

**Chainlink:** #343 (parent) + #344-#351 (subissues)

## Context

The project moved CI and pull-request orchestration to Forgejo and Forgejo Actions. Forgejo provides a GitHub-compatible REST surface, so ExoMonad can use one forge as the source of truth for PR metadata, reviews, branch heads, and commit statuses.

## Current State

- `.exo/config.toml` stores Forgejo URL, author token, reviewer token, and webhook configuration.
- `exomonad new` provisions Forgejo repositories and action workflow files.
- `exomonad init` injects `GH_HOST` and `GH_TOKEN` for spawned agents.
- `exomonad serve` receives Forgejo CI webhooks and the watcher also polls Forgejo commit statuses for PR head SHAs.
- Reviewer tools submit Forgejo pull-request reviews through `forgejo_reviewer_token`, which must belong to a different Forgejo user than the PR author token.

## CI Status Flow

```
git push -> Forgejo -> Forgejo Actions runs workflow
         -> webhook and commit status APIs expose result
         -> watcher observes PR review + head status
         -> MergeReady fires only after approval and mergeable CI status
```

## Verification

1. `just build` compiles the workspace.
2. `docker compose -f forgejo/docker-compose.yml up -d` starts local Forgejo.
3. `exomonad new` creates a Forgejo repository and registers the webhook.
4. A pushed branch triggers Forgejo Actions.
5. `/ci` webhooks and commit status polling update watcher state.
6. `merge_pr` passes the merge gate only when the approved head SHA has a success or neutral status.
