# E2E Status

Last updated: 2026-05-14

This document tracks the high-signal E2E coverage needed before continuing the Codex and runtime-role test work. Use `just` targets as the test entrypoint unless a row explicitly says it is a planning-only release gate.

## Current Tests

| Test | Target | Scope | Last observed result | Status | Notes / next action |
| --- | --- | --- | --- | --- | --- |
| Codex hooks | `just e2e-codex-hooks` | Codex root -> Codex TL -> Codex dev leaf -> local PR -> Codex reviewer, plus hook config and `gh` command denial | Passed with formal validator result: `Failures: 0`. Reviewer MCP scope was fixed and verified by `just test-wasm-integration`; reviewer role exposes `approve_pr`, `request_changes`, `post_review_comment`, and `notify_parent`. | Green | Hook review notice is deferred under Chainlink #191. Keep this as the main local Codex orchestration regression. |
| Codex hooks static check | `just check-e2e-codex-hooks` | Syntax checks for the Codex hooks run and validator scripts | Passed after removing the unavailable team-creation instruction. | Green | Keep this as the cheap preflight for edits to `tests/e2e/codex-hooks/`. |
| Codex messaging | `just e2e-codex-messaging` | Codex root -> Codex TL -> Codex dev leaf, with TL `send_message` to dev and TL/dev `notify_parent` back up through tmux routing | Passed with formal validator result: `Failures: 0`. Static preflight `just check-e2e-codex-messaging` also passed. | Green | Added under Chainlink #147. Uses `/tmp` temp repo/session only, isolated `CODEX_HOME`, local `.exo/logs` assertions, and no `gh`/external PR state. |
| Chainlink role tool scope | `just test-wasm-integration` | Static WASM assertions for Chainlink MCP tool exposure by role: TL, dev, worker, root, reviewer, testrunner | Passed: `30 passed` before sqlite hook additions; later full run passed `32 passed`. | Green | Added under Chainlink #172. This pins the role contract before Codex Chainlink MCP E2E work. |
| Chainlink sqlite block | `just e2e-chainlink-sqlite-block` | PreToolUse denies direct `.chainlink/issues.db` access for Claude-shaped, Codex-shaped, and OpenCode-shaped hook invocations | Passed. Runtime probes denied all three payload shapes and the fake `sqlite3` marker was absent. Static preflight `just check-e2e-chainlink-sqlite-block` also passed. | Green | Added under Chainlink #174. Uses `/tmp` temp repo/server only and validates hook trace logs for all three runtimes. |
| Chainlink Codex flow | `just e2e-chainlink-codex` | Codex root -> Codex TL -> Codex worker. TL uses `chainlink_issue_create`, `chainlink_session_status`, and coordinator-side `chainlink_issue_close`; worker uses `chainlink_session_start`, `chainlink_session_work`, `chainlink_issue_comment`, `chainlink_session_end`, and `notify_parent`. | Static preflight `just check-e2e-chainlink-codex` passed after the Chainlink timer/role-scope refactor. `just test-wasm-integration` also passed with `32 passed` and covers the updated role contract. | Green | Uses `/tmp`, isolated `CODEX_HOME`, local Chainlink DB, and no `gh` or external PR state. Chainlink agent/sync/lock tools are intentionally out of the role workflow. |
| Chainlink timer role scope | `just check-e2e-chainlink-timer-role-scope` | Static role-scope assertions for TL-only timer tools, coordinator close semantics, dev subissue close, worker telemetry-only tools, and no lock/agent/sync role exposure. | Passed after the Chainlink timer/role-scope refactor. Final preflight also paired this with `just check-e2e-chainlink-codex`, `bash -n tests/e2e/chainlink/run.sh`, and `bash -n tests/e2e/chainlink-close/run.sh`. | Green | Added under Chainlink #196. Keep this cheap preflight paired with `just test-wasm-integration` for Chainlink MCP surface changes. |
| Runtime-role matrix | Planning-only release gate for now | Important TL/dev/reviewer role combinations across Claude Code, Codex, and OpenCode | Not run. | Next | Keep decomposable: Codex-only subset for regular work; full matrix only before production release. |
| Tangled VM PR integration | `just e2e-tangled-vm-pr` | Real Tangled VM PR path with external Git remote, knot event stream, spindle event stream, reviewer approval, `approved_at_sha`, and merge-ready delivery | Static preflight `just check-e2e-tangled-vm-pr` passed. Runtime not run in normal local loop; requires VM env. | Ready for VM run | Separate from local E2E. Requires `TANGLED_VM_GIT_REMOTE`, `TANGLED_VM_KNOT_WS_URL`, `TANGLED_VM_SPINDLE_WS_URL`, and `TANGLED_VM_OWNER_DID`. |

## Codex Hooks Feedback

- The current Codex hooks E2E is functionally validating the core local orchestration path; the latest rerun is waiting for a formal validator result.
- The `3 hooks need review before they can run` warning is Codex's project-hook trust gate, not evidence that TL spawning is wrong.
- The hook denial path is working: the validator's Codex-shaped `gh auth status` payload received a deny response with `permissionDecision = deny`.
- Reviewer MCP scope is now explicit: reviewer-role `tools/list` includes `approve_pr`, `request_changes`, `post_review_comment`, and `notify_parent`; `just test-wasm-integration` covers this.
- The previous "team creation MCP tool" prompt text was stale and has been removed from the committed E2E prompt.
- The test should remain local-only: unset GitHub auth, use local `.exo/prs.json`, and keep any Tangled/GitHub PR integration in a separate E2E.
