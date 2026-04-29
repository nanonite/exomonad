# Post-Mortem: Chainlink Issue Dispatch via OpenCode Workers

## Summary

22 chainlink issues dispatched across 7 batches using `exomonad_spawn_worker`. All issues were implemented and closed successfully. The site runs on port 8765 via `just dev`.

## Issues with Agent Spawning

### 1. `notify_parent` never fires

`spawn_worker` appends a "Completion Protocol" to each task instructing the worker to call `notify_parent` with status + detailed results. Not a single worker did this across all 22 issues. Workers completed their work (wrote files, ran `chainlink issue close`) but the parent received zero notifications.

**Workaround:** Poll manually: check `ps aux | grep 'opencode run'`, inspect created files, verify git status and issue list.

### 2. `fork_wave` fails with "Failed to capture OpenCode ACP port"

`fork_wave` (which creates isolated worktrees + branches per worker) failed immediately. Root cause: no ACP server was running for exomonad to connect to. `spawn_worker` was used as a fallback — it spawns headless `opencode run` processes directly.

### 3. Workers skip `chainlink issue close` inconsistently

~30% of workers completed their implementation but did not run `chainlink issue close`. The issue had to be closed manually. The workers that DID close successfully suggest it's a race or timing issue rather than a systematic failure.

### 4. No file isolation on `spawn_worker`

`spawn_worker` runs all workers on the parent's branch with no worktree isolation. Parallel workers modifying the same files would conflict. Batches had to be planned carefully to avoid overlapping file writes.

## Recommendations

- Fix `notify_parent` delivery from opencode workers to exomonad
- Get `fork_wave` working (needs opencode ACP server running) for proper branch isolation and PR workflow
- Make `chainlink issue close` more robust in the worker completion step
- Consider adding a `--wait` mode to `spawn_worker` that blocks until `notify_parent` is received
