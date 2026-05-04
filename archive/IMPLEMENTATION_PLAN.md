# Chainlink MCP Integration + OpenCode Reliability

## Goal

Make the OpenCode TL + worker workflow reliable end-to-end by:
1. Orienting agents at session start with step-by-step chainlink + exomonad workflows
2. Wrapping chainlink CLI as MCP tools with bundled logic (atomic sequences, structured output)
3. Enforcing worker completion via stop hooks and an atomic close-and-notify primitive
4. Giving the TL a single supervision tool (`chainlink_worker_status`) to detect stuck/runaway workers
5. Deprecating the broken OpenCode ACP `fork_wave` path in favor of tmux `spawn_worker`

## Architecture Notes

- `spawn_worker` runs on **parent's branch with no git isolation** (SharedDir). Workers must not commit/push/branch.
- `chainlink agent` is identity-only — does not log activity. Supervision signal comes from lock hold time + token usage + `git diff --stat`.
- `chainlink` DB is hard-coded to `{project_dir}/.chainlink/` — MCP chainlink tools target `spawn_worker` workers only. `spawn_leaf` worktree workers need a `--db` flag added to chainlink CLI (filed separately).
- MCP tools bundle non-obvious sequences. CLI still available for simple reads.

## Execution Order

Tracks A and E are independent. Track B must precede C and D.

```
Track A ──────────────────────────────────────────────────────► done
Track B: #61 → #62 → #63,#64 → #65 ──────────────────────────► done
                                    └──► Track C: #66,#67 → #68
                                    └──► Track D: #69 → #70,#71
Track E ──────────────────────────────────────────────────────► done
```

## Track A — Session-Start Orientation (milestone #7)

**Issues:** #57 #58 #59 #60

| # | Issue | File |
|---|-------|------|
| 57 | Create `chainlink-tl.md` | `.exo/roles/devswarm/context/chainlink-tl.md` (new) |
| 58 | Create `chainlink-worker.md` | `.exo/roles/devswarm/context/chainlink-worker.md` (new) |
| 59 | Inject worker orientation into `workerProfileText` | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs` |
| 60 | Inject TL orientation into OpenCode `initial_prompt` | `rust/exomonad-core/src/services/opencode_acp.rs` |

**TL orientation** (chainlink-tl.md) covers: scaffold → spawn → supervise (`chainlink_worker_status`) → handle blocks → merge. Lists all ExoMonad MCP tools and chainlink TL commands.

**Worker orientation** (chainlink-worker.md) covers: `agent init` + `session start` + `issue_claim` → `issue_show` → do work + comment → `chainlink_issue_close` (MCP only). Includes stuck-escalation and scope-creep paths.

## Track B — Chainlink MCP Tools (milestone #8)

**Issues:** #61 #62 #63 #64 #65 — blocks Tracks C and D

| # | Issue | File |
|---|-------|------|
| 61 | Investigate existing RunProcess/ExecCommand effect | `haskell/wasm-guest/src/ExoMonad/Guest/Effects/` |
| 62 | Add `ChainlinkExec` effect + Rust handler (if needed) | `Effects/Chainlink.hs`, `handlers/chainlink.rs`, proto |
| 63 | Implement TL chainlink MCP tools | `Tools/Chainlink.hs` (new) |
| 64 | Implement worker chainlink MCP tools | `Tools/Chainlink.hs` |
| 65 | Wire tools into `TLRole.hs` and `WorkerRole.hs` | `.exo/roles/devswarm/` |

**TL tools:** `chainlink_issue_create/list/show/update/block/relate/cascade`, `chainlink_milestone`, `chainlink_sync`

**Worker tools (bundled logic):**
- `chainlink_issue_claim` → `locks claim` + `session work` (atomic)
- `chainlink_issue_close` → `locks release` + `issue close` + `session end` + `NotifyParent` (atomic, 4 steps)
- `chainlink_issue_comment`, `chainlink_timer`, `chainlink_locks`

All tools use `--json` output. Failures surface as MCP errors.

## Track C — Worker Completion Reliability (milestone #9)

**Issues:** #66 #67 #68 — blocked by #64 (B4)

| # | Issue | File |
|---|-------|------|
| 66 | Verify `chainlink_issue_close` 4-step atomic sequence | `Tools/Chainlink.hs` |
| 67 | Add worker stop hook blocking exit on active locks | `.exo/roles/devswarm/WorkerRole.hs` |
| 68 | Integration test: close fires `notify_parent`, stop hook blocks exit | `tests/e2e/opencode-worker/` |

**Root cause of 0% notify_parent rate (post-mortem):** workers exit after primary task completes, treating completion protocol as optional text. Fix: make `chainlink_issue_close` the only way to close — it atomically notifies parent. Stop hook enforces no exit with open locks.

## Track D — Worker Supervision (milestone #10)

**Issues:** #69 #70 #71 — blocked by #65 (B5)

| # | Issue | File |
|---|-------|------|
| 69 | Implement `chainlink_worker_status` aggregation tool | `Tools/Chainlink.hs` |
| 70 | Expose `chainlink_worker_status` in `TLRole.hs` | `.exo/roles/devswarm/TLRole.hs` |
| 71 | Unit tests for aggregation logic | test suite |

**`chainlink_worker_status` aggregates:**
1. `chainlink issue list --json --status open` — locked issues
2. `chainlink locks list --json` — lock hold timestamps
3. `chainlink usage list --json` — token consumption per agent
4. `git diff --stat` — uncommitted files on shared branch

**Returns per-worker:** `{agent_id, issue_id, issue_title, lock_held_minutes, input_tokens, output_tokens, estimated_cost_usd, uncommitted_files[]}`.

**Runaway signals:** `lock_held_minutes > 20` + no `notify_parent` = stuck. `uncommitted_files` outside spec = scope creep. `input_tokens > 2x expected` = looping.

**Escalation (TL):** ping via `send_message` → kill tmux pane + re-decompose after 5 min no reply.

## Track E — OpenCode ACP Cleanup (milestone #11)

**Issues:** #72 #73

| # | Issue | File |
|---|-------|------|
| 72 | Add ACP deprecation warning for OpenCode in `spawnAcpCore` | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs` |
| 73 | Unit test: warning logged for OpenCode, not for Claude/Gemini | test suite |

Warning text: `[WARN] OpenCode ACP fork_wave is unstable: OpenCode does not yet support team calling or inbox delivery. Use spawn_worker (tmux) for OpenCode workers.`

## Out of Scope (follow-up)

- **Chainlink `--db <path>` flag** — needed for `spawn_leaf` worktree workers to access root DB. File as chainlink issue.
- **PR quality / automated reviews** — deferred until this workflow stabilizes.

## Verification

```bash
just install-all-dev                    # after each track

# Track A: inspect spawn log for orientation text
# Track B: chainlink_issue_claim → verify lock; chainlink_issue_close → verify notify_parent
# Track C: spawn worker, call close, verify parent notified; exit with open lock → stop hook fires
# Track D: spawn 2 workers, call chainlink_worker_status, verify JSON
# Track E: fork_wave opencode → verify warning in log

just e2e-opencode-worker                # full E2E
```
