# E2E testrunner companion audit

Chainlink #287 audited harnesses that use LLM testrunner companions after a
reviewer convergence harness produced a fabricated failure verdict. The failure
mode was polling superseded local review artifacts and inferring state from files
that no longer represent the review source of truth.

## Decisions

| Harness | Decision | Rationale |
| --- | --- | --- |
| `messaging/` | Keep for now | The companion is the message recipient under test; replacing it needs a dedicated non-LLM inbox observer so the test still covers live delivery to a companion. |
| `hook-rewrite/` | Keep for now | The companion validates OpenCode hook rewriting from the fixture output. It is not polling overwritten review state, but should be migrated to a process validator under the broader harness redesign. |
| `codex-messaging/` | No LLM testrunner | Uses a process validator companion (`codex-messaging-validator`). |
| `opencode-tl/` | Keep for now | The companion is the direct `send_message` target used to validate ACP-to-Teams delivery. |
| `opencode-worker/` | Keep for now | The companion observes worker completion and message routing. It should be converted with the other legacy Claude testrunners. |
| `chainlink/` | Keep for now | The companion validates the MCP tool result through Chainlink CLI and CHANGELOG state. |
| `chainlink-close/` | Keep for now | The companion validates issue close behavior and generated CHANGELOG state. |
| `chainlink-codex/` | No LLM testrunner | Uses a process validator companion (`chainlink-codex-validator`). |
| `chainlink-timer-role-scope/` | No LLM testrunner | Uses a shell validator recipe, not an LLM companion. |

## Follow-up

The remaining legacy Claude testrunners are still candidates for the broader
harness redesign in #281. They were not removed here because each currently acts
as an addressed companion or fixture-level observer.
