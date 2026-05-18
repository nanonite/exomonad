# E2E testrunner companion audit

Chainlink #287 audited harnesses that use LLM testrunner companions after the
reviewer-convergence harness produced a fabricated failure verdict. The failure
mode was polling an overwritten artifact (`.exo/reviews/pr_1.json`) and inferring
that an intermediate `changes_requested` review never happened.

`tests/e2e/reviewer-convergence-loop/validate.sh` is the objective oracle for
that harness because it reads append-only server log evidence:

- `Fanning out pr_review event to reviewer agent`
- `[EventDispatch] Calling handle_event for agent 'review-pr-...`
- `[EventDispatch] handle_event returned`

## Decisions

| Harness | Decision | Rationale |
| --- | --- | --- |
| `reviewer-convergence-loop/` | Strip LLM testrunner | The process validator already owns objective verdicts from append-only logs and `.exo/prs.json`; the LLM testrunner fabricated failure modes from overwritten review state. |
| `messaging/` | Keep for now | The companion is the message recipient under test; replacing it needs a dedicated non-LLM inbox observer so the test still covers live delivery to a companion. |
| `hook-rewrite/` | Keep for now | The companion validates OpenCode hook rewriting from the fixture output. It is not polling overwritten review state, but should be migrated to a process validator under the broader harness redesign. |
| `codex-messaging/` | No LLM testrunner | Uses a process validator companion (`codex-messaging-validator`). |
| `codex-hooks/` | No LLM testrunner | Uses a process validator companion (`codex-hooks-validator`); `testrunner.md` is role context for spawned agents, not a companion. |
| `opencode-tl/` | Keep for now | The companion is the direct `send_message` target used to validate ACP-to-Teams delivery. |
| `opencode-worker/` | Keep for now | The companion observes worker completion and message routing. It should be converted with the other legacy Claude testrunners, but does not share the overwritten-review artifact failure mode. |
| `chainlink/` | Keep for now | The companion validates the MCP tool result through Chainlink CLI and CHANGELOG state. |
| `chainlink-close/` | Keep for now | The companion validates issue close behavior and generated CHANGELOG state. |
| `chainlink-codex/` | No LLM testrunner | Uses a process validator companion (`chainlink-codex-validator`). |
| `chainlink-timer-role-scope/` | No LLM testrunner | Uses a shell validator recipe, not an LLM companion. |
| `tangled-pr-codex/` | No LLM testrunner | Uses shell/Python validation and `testrunner.md` as validator notes only. |

## Follow-up

The remaining legacy Claude testrunners are still candidates for the broader
harness redesign in #281. They were not removed here because each currently acts
as an addressed companion or fixture-level observer, while the immediate bug was
specific to reviewer-convergence's redundant, non-history-preserving verdict
agent.
