# Exomonad Plans

## 1. Worktree Cleanup After Merge

**Status:** Handled automatically.

The `merge_pr` MCP tool (in `MergePR.hs:285-296`) already runs `agent.cleanup` after merging: closes agent tab, removes worktree, unregisters from routing. No additional TL prompt instruction needed.

**Orphan cleanup (not yet exposed):** The `agent.cleanup_merged` effect exists in proto/effects/agent.proto and the Rust handler but has no MCP tool wrapper. Stale worktrees from crashed/failed agents accumulate. Options:
- Expose `cleanup_merged` as a TL MCP tool
- Cron-style server-side sweep on startup
- `git worktree prune` in the prompt as fallback

---

## 2. Worker `notify_parent` Availability

**Status:** Fixed (2026-04-30).

OpenCode workers spawned via `spawn_worker` previously inherited the caller's `opencode.json` with `--role root`, which lacks `notify_parent`. Fixed in `spawn.rs` — workers now get their own `opencode.json` with `--role worker --name <agent>` in their agent config dir.

---

## 3. Autonomous TL Prompt

**Status:** Written (`root-tl-autonomous.md`).

Looping variant that processes all open chainlink issues without pausing for user input between waves. Dispatch → converge → check chainlink → repeat until zero open issues.

---

## 4. fork_wave agent_type Fallback

**Status:** Verified working.

All three spawn paths (spawn_subtree, spawn_worker, spawn_leaf_subtree) use `unwrap_or(default_type)` where `default_type = self.service.default_spawn_agent_type()` resolved from `EXOMONAD_SPAWN_AGENT_TYPE` env var. Calling `fork_wave` without `agent_type` correctly defaults to the server's configured spawn type.

---

## 5. Chainlink.Pure Module Placement

**Status:** Fixed (2026-04-30).

Moved from top-level `ExoMonad.Chainlink.Pure` to `ExoMonad.Guest.Tools.Chainlink.Pure` — nested under its parent tool module, following the `Guest.Tool.Suspend.Types` precedent. Removed the `wasm-guest-pure` library; folded into `wasm-guest-internal`.

---

## 6. Pending / TODO

- [ ] Expose `cleanup_merged` as TL MCP tool for orphaned worktree cleanup
- [ ] E2E test for OpenCode worker `notify_parent` delivery
- [ ] E2E test for autonomous TL loop (chainlink issue → worker → notify_parent → merge_pr → cleanup)
- [ ] Worker chainlink protocol injection into opencode worker prompt (currently only in workerProfileText for Gemini)
- [ ] TL observability: `chainlink_session_status` integration with autonomous dispatch loop
- [ ] Rate-limiting / max parallel workers in autonomous mode
- [ ] Session persistence across server restarts for in-flight waves
