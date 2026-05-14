# E2E Status

Last updated: 2026-05-14

This document tracks the high-signal E2E coverage needed before continuing the Codex and runtime-role test work. Use `just` targets as the test entrypoint unless a row explicitly says it is a planning-only release gate.

## Current Tests

| Test | Target | Scope | Last observed result | Status | Notes / next action |
| --- | --- | --- | --- | --- | --- |
| Codex hooks | `just e2e-codex-hooks` | Codex root -> Codex TL -> Codex dev leaf -> local PR -> Codex reviewer, plus hook config and `gh` command denial | The flow exercised successfully: root spawned TL, TL spawned dev, dev committed/pushed/filed local PR #1, reviewer completed with success, and the `gh auth status` probe was denied by the hook policy. The run did not produce a formal validator result file. | Needs cleanup before counting as green | Remove stale prompt assumptions from the running temp session, then make Codex hook trust non-interactive so `3 hooks need review before they can run` does not appear. Also check the tmux delivery warning caused by the TL pane disappearing before reviewer/dev `notify_parent` injection. |
| Codex hooks static check | `just check-e2e-codex-hooks` | Syntax checks for the Codex hooks run and validator scripts | Passed after removing the unavailable team-creation instruction. | Green | Keep this as the cheap preflight for edits to `tests/e2e/codex-hooks/`. |
| Codex messaging | Not added yet | Codex Teams inbox / tmux message delivery path | Not run. | Planned | Next E2E candidate after Codex hooks cleanup. Should use a `just` target and `/tmp` temp repo/session only. |
| Runtime-role matrix | Planning-only release gate for now | Important TL/dev/reviewer role combinations across Claude Code, Codex, and OpenCode | Not run. | Planned | Keep decomposable: Codex-only subset for regular work; full matrix only before production release. |
| Tangled VM PR integration | Not added yet | Real Tangled VM / PR integration path | Not run. | Planned separately | Keep separate from local E2E so local tests use `.exo/prs.json` and do not require `gh` or external PR state. |

## Codex Hooks Feedback

- The current Codex hooks E2E is functionally validating the core local orchestration path, but it is not yet a clean unattended pass.
- The `3 hooks need review before they can run` warning is Codex's project-hook trust gate, not evidence that TL spawning is wrong.
- The hook denial path is working: the validator's Codex-shaped `gh auth status` payload received a deny response with `permissionDecision = deny`.
- The previous "team creation MCP tool" prompt text was stale and has been removed from the committed E2E prompt.
- The test should remain local-only: unset GitHub auth, use local `.exo/prs.json`, and keep any Tangled/GitHub PR integration in a separate E2E.
