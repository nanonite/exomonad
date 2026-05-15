# Tangled PR Codex E2E Validator Notes

This test validates the Codex root/TL/worker/dev/reviewer path against a local Tangled remote and real Spindle CI ingestion. The active validator is `validate.sh`; this file records the expected checks for humans and future validators.

## Run Commands

```bash
just install-all-dev
just check-e2e-tangled-pr-codex
just e2e-tangled-pr-codex
```

`just e2e-tangled-pr-codex` launches `exomonad init --verbose --reviewer codex` in an isolated fixture repo. It requires `codex`, `tmux`, `docker`, `sqlite3`, `curl`, the local Tangled knot container, built WASM plugins, and the Spindle binary. `just install-all-dev` builds the Spindle binary and installs it to the ExoMonad dev install path.

## Expected Coverage

- Root Codex starts from `e2e-test.md`, spawns exactly one Codex TL, then idles.
- The Codex TL spawns exactly one Codex worker and exactly one Codex dev leaf.
- The worker reports `[TANGLED-PR-CODEX-WORKER-DONE]` through normal `notify_parent` tmux delivery.
- The dev leaf creates `tangled-pr-codex-dev-output.txt`, commits it, files a local PR through `.exo/prs.json`, reports `[TANGLED-PR-CODEX-DEV-DONE]`, and stays alive.
- `file_pr` pushes the dev branch to the local Tangled remote.
- The validator injects a Tangled pipeline event for the dev branch, starts Spindle, and waits for the workflow log to end successfully.
- ExoMonad maps the Spindle event back to the PR branch and records successful CI ingestion in `.exo/logs`.
- Filing the local PR spawns a normal Codex reviewer-role agent; the test must not use `codex exec review`.
- Reviewer approval is recorded in `.exo/reviews/pr_1.json` and delivered through normal reviewer `notify_parent` routing.
- The watcher records `[MERGE READY]` only after reviewer approval and CI success are both present.
- The watcher sends a merge-ready release message back to the original live dev leaf.
- The watcher must not log `No plugin found for agent` for the dev leaf before merge-ready release.

## Pass Criteria

The validator writes `Failures: 0` to its result file. A passing run proves the full local chain:

```text
Codex root -> Codex TL -> Codex worker
Codex TL -> Codex dev leaf -> local PR
local PR -> Tangled branch push -> knot event -> Spindle CI
Spindle success + Codex reviewer approval -> merge-ready notification
merge-ready notification -> release delivered to original dev leaf
```

Do not mutate the fixture manually while validating. The root, TL, worker, dev leaf, reviewer, Tangled push, Spindle event ingestion, and merge-ready release must be produced through normal ExoMonad runtime paths.
