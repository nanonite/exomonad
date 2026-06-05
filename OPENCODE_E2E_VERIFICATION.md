# OpenCode E2E Verification

Date: 2026-05-27
Chainlink: #171

## Scope

This pass verifies OpenCode compatibility with ExoMonad role/tool contracts and the tmux/mailbox message design before testing mixed runtime harnesses. Claude Code may be used as an observer/delegator in existing harnesses, but Claude Code as a role under test is deferred to #421.

## Results

| Check | Status | Evidence | Notes |
| --- | --- | --- | --- |
| OpenCode harness shell syntax | Passed | `bash -n tests/e2e/opencode-tl/run.sh tests/e2e/opencode-worker/run.sh tests/e2e/hook-rewrite/run.sh` | Static only; does not launch agents. |
| OpenCode Rust unit coverage | Passed | `cargo test -p exomonad-core opencode` | 9 tests passed: OpenCode serialization, command construction, model config, TUI signal detection. |
| WASM `spawn_leaf` OpenCode agent_type | Passed | `cargo test -p exomonad-core wasm_spawn_leaf_passes_agent_type` | Confirms `agent_type = opencode` reaches the spawn_leaf effect. |
| WASM `spawn_worker` OpenCode agent_type | Passed | `cargo test -p exomonad-core wasm_spawn_worker_passes_agent_type` | Confirms `agent_type = opencode` reaches the spawn_worker effect. |
| WASM `notify_parent` roundtrip | Passed | `cargo test -p exomonad-core wasm_notify_parent_roundtrip` | Confirms MCP/WASM notify_parent tool roundtrip. |
| Worker role notify_parent visibility | Passed | `cargo test -p exomonad-core wasm_worker_tools_include_notify_parent` | Worker exposes notify_parent and excludes coordinator tools. |
| Reviewer role tool visibility | Passed | `cargo test -p exomonad-core wasm_reviewer_tools_include_review_commands` | Reviewer exposes review commands and excludes notify_parent/spawn/file_pr/merge_pr. |
| Live tool visibility matrix | Passed | `cargo test -p exomonad-core mcp_tool_visibility_matrix_matches_live_wasm_tools` | Live WASM tool set matches documented role matrix. |
| Delivery routing tests | Passed | `cargo test -p exomonad-core delivery` | 21 tests passed, including OpenCode/non-Claude inline tmux submit, Teams inbox distinction, routing targets, and tmux fallback. |
| Live `opencode-tl` harness launch without TTY | Failed | `timeout 420s ./tests/e2e/opencode-tl/run.sh` | Fails at `tmux attach`: `open terminal failed: not a terminal`. Harness is interactive by design. |
| Live `opencode-tl` harness launch with PTY default TERM | Failed | `timeout 240s ./tests/e2e/opencode-tl/run.sh` in PTY | Fails at `tmux attach`: `terminal does not support clear`. |
| Live `opencode-tl` harness launch with PTY + TERM | Failed | `env TERM=xterm-256color timeout 240s ./tests/e2e/opencode-tl/run.sh` | OpenCode wrote the expected repo-root file, but the harness failed: testrunner polled the wrong path and no OpenCode `send_message` call appeared in the MCP log. |
| Live `opencode-worker` harness with PTY + TERM | Failed | `env TERM=xterm-256color timeout 360s ./tests/e2e/opencode-worker/run.sh` | Session started, but root TL landed idle with `No conversation found to continue`; `fork_wave` never ran, no OpenCode worker window appeared, and no `oc-worker-output.txt` was created. |

## Live `opencode-tl` Findings

Working evidence:

- Tmux session `e2e-opencode-tl` was created.
- Server, Watcher, and `test-runner` windows were created.
- OpenCode TL window started and then exited.
- OpenCode created `/home/goya/.cache/exomonad-e2e/oct.Lb30tGYV/repo/opencode-tl-test.txt`.
- File content was exactly `OpenCode TL test passed`.

Problems found:

- Claude Code testrunner stopped at the workspace trust prompt until manually accepted.
- After acceptance, the testrunner used `git rev-parse --show-toplevel` from the companion worktree, so it polled `/home/goya/.cache/exomonad-e2e/oct.Lb30tGYV/repo/.exo/companions/test-runner/opencode-tl-test.txt` instead of the actual repo-root output file.
- No `OC-TL-DONE` entry was found in `~/.claude/teams`.
- The OpenCode root MCP log only showed `tools/list`; it did not show a `send_message` tool call, so the Teams inbox path was not exercised by OpenCode in this run.
- The Claude testrunner reported failure via `notify_parent`, and that `notify_parent` tool call succeeded.

## Deferred

- Claude Code as TL/dev-leaf/worker/reviewer is tracked separately in #421 because Claude credits are limited this week.
- Mixed runtime matrix testing should happen after the OpenCode-only harness observer and message assertions are reliable.

## Live `opencode-worker` Findings

Working evidence:

- Tmux session `e2e-opencode-worker` was created.
- Server, TL, Watcher, and `test-runner` windows were created.
- Both Claude Code trust prompts could be manually accepted.

Problems found:

- The root TL window displayed `No conversation found to continue` and then sat idle at the Claude Code prompt.
- The root TL did not execute the `e2e-test.md` instructions, so `fork_wave` was never called.
- No OpenCode worker window appeared.
- No `oc-worker-output.txt` was created.
- The testrunner's Phase 1 checked global `~/.claude/teams` and treated stale team directories as success, so team detection is not scoped to the active test session.

## Follow-Up Chainlink Issues

- #421: Verify Claude-only ExoMonad session flow when credits are available.
- #422: Fix OpenCode TL E2E observer path and message assertion.
- #423: Avoid Claude Code workspace trust prompts in E2E harnesses.
- #424: Fix OpenCode worker E2E root TL task delivery.
- #425: Scope E2E team detection to the active test session.
