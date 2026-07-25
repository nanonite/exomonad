# E2E Status

Last updated: 2026-05-30

This document tracks the high-signal E2E coverage needed before continuing the Codex and runtime-role test work. Use `just` targets as the test entrypoint unless a row explicitly says it is a planning-only release gate.

## Current Tests

| Test | Target | Scope | Last observed result | Status | Notes / next action |
| --- | --- | --- | --- | --- | --- |
| Codex messaging | `just e2e-codex-messaging` | Codex root -> Codex TL -> Codex dev leaf, with TL `send_message` to dev and TL/dev `notify_parent` back up through tmux routing | Passed with formal validator result: `Failures: 0`. Static preflight `just check-e2e-codex-messaging` also passed. | Green | Added under Chainlink #147. Uses `/tmp` temp repo/session only, isolated `CODEX_HOME`, local `.exo/logs` assertions, and no `gh`/external PR state. |
| Chainlink role tool scope | `just test-wasm-integration` | Static WASM assertions for Chainlink MCP tool exposure by role: TL, dev, worker, root, reviewer, testrunner | Passed: `30 passed` before sqlite hook additions; later full run passed `32 passed`. | Green | Added under Chainlink #172. This pins the role contract before Codex Chainlink MCP E2E work. |
| Chainlink sqlite block | `just e2e-chainlink-sqlite-block` | PreToolUse denies direct `.chainlink/issues.db` access for Claude-shaped, Codex-shaped, and OpenCode-shaped hook invocations | Passed. Runtime probes denied all three payload shapes and the fake `sqlite3` marker was absent. Static preflight `just check-e2e-chainlink-sqlite-block` also passed. | Green | Added under Chainlink #174. Uses `/tmp` temp repo/server only and validates hook trace logs for all three runtimes. |
| Chainlink Codex flow | `just e2e-chainlink-codex` | Codex root -> Codex TL -> Codex worker. TL uses `chainlink_issue_create`, `chainlink_session_status`, and coordinator-side `chainlink_issue_close`; worker uses `chainlink_session_start`, `chainlink_session_work`, `chainlink_issue_comment`, `chainlink_session_end`, and `notify_parent`. | Static preflight `just check-e2e-chainlink-codex` passed after the Chainlink timer/role-scope refactor. `just test-wasm-integration` also passed with `32 passed` and covers the updated role contract. | Green | Uses `/tmp`, isolated `CODEX_HOME`, local Chainlink DB, and no `gh` or external PR state. Chainlink agent/sync/lock tools are intentionally out of the role workflow. |
| Chainlink timer role scope | `just check-e2e-chainlink-timer-role-scope` | Static role-scope assertions for TL-only timer tools, coordinator close semantics, dev subissue close, worker telemetry-only tools, and no lock/agent/sync role exposure. | Passed after the Chainlink timer/role-scope refactor. Final preflight also paired this with `just check-e2e-chainlink-codex`, `bash -n tests/e2e/chainlink/run.sh`, and `bash -n tests/e2e/chainlink-close/run.sh`. | Green | Added under Chainlink #196. Keep this cheap preflight paired with `just test-wasm-integration` for Chainlink MCP surface changes. |
| Claude-only bounded smoke | `just e2e-claude-only` / `just check-e2e-claude-only` | Claude Code root TL on Haiku with explicit role-safe `initial_prompt`; validates server startup, root SessionStart registration, TeamCreate, and Teams metadata registration without spawning children | Passed on 2026-05-27. Harness used `port = 0`, pretrusted the temp workspace, observed root Claude session registration, TeamCreate, new Teams directory, and `Registered team: exomonad-smoke-test`. | Green | This is intentionally bounded to root TL startup/Teams registration. Full Claude TL/worker/dev-leaf/reviewer matrix remains tracked by #421 to avoid unbounded token use. |
| Runtime-role matrix | `docs/architecture/runtime-role-e2e-matrix.md` | Release-gate matrix for TL/dev/reviewer/worker coverage across Claude Code, Codex, and OpenCode | Defined under Chainlink #171. | Planned | Regular development gates cover static contracts and local Codex/OpenCode paths; release-only gates cover Claude credit-burning and Forgejo reviewer provenance flows. |

## Codex Hooks Feedback

- The current Codex messaging E2E is functionally validating the core local orchestration path; the latest rerun is waiting for a formal validator result.
- The `3 hooks need review before they can run` warning is Codex's project-hook trust gate, not evidence that TL spawning is wrong.
- The hook denial path is working: the validator's Codex-shaped `gh auth status` payload received a deny response with `permissionDecision = deny`.
- Reviewer MCP scope is now explicit: reviewer-role `tools/list` includes `approve_pr`, `request_changes`, `post_review_comment`, and `notify_parent`; `just test-wasm-integration` covers this.
- The previous "team creation MCP tool" prompt text was stale and has been removed from the committed E2E prompt.
- The test should remain local-only: unset GitHub auth and keep Forgejo-backed PR integration in a separate E2E.
