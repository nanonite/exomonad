# E2E Chainlink Issue Create Test Mode

This test validates the `chainlink_issue_create` MCP tool. The TL agent receives its task via `initial_prompt` and calls the tool, which shells out to `chainlink create` via the `process.run` effect. The TL then writes the result to a file and notifies the testrunner.

The testrunner companion (Claude haiku) validates:
1. `chainlink-e2e-result.txt` was created in the repo root
2. The file contains a valid numeric issue ID
3. `chainlink issue show <id>` confirms the issue exists with the correct title

This file is loaded by the testrunner companion only (via `.claude/rules/`).
