# E2E Chainlink Codex Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in Chainlink Codex test mode. The validator process observes generated Codex configs, Chainlink state, tmux delivery logs, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Call the ExoMonad chainlink_issue_create MCP tool with:
   - title: E2E chainlink codex worker
   - priority: low
   - labels: e2e,chainlink,codex
2. Save the returned issue ID.
3. Spawn exactly one Codex dev leaf with the ExoMonad spawn_codex MCP tool.
4. Stop and idle after the dev leaf is spawned.

## spawn_codex Spec

Spawn one Codex dev leaf:

- branch_name: chainlink-codex-dev
- task:

You are a Codex dev leaf in the Chainlink Codex E2E test. Do exactly these steps. The root must include the returned issue ID in this task:

1. Call the ExoMonad chainlink_session_status MCP tool.
2. Call the ExoMonad chainlink_session_start MCP tool.
3. Call the ExoMonad chainlink_session_work MCP tool with the issue ID supplied in this task.
4. Call the ExoMonad chainlink_issue_comment MCP tool with that issue ID and this exact message: [CHAINLINK-CODEX-WORKER-COMMENT] Codex worker comment recorded.
5. Call the ExoMonad chainlink_session_end MCP tool with notes: [CHAINLINK-CODEX-WORKER-DONE] Codex worker session complete.
6. Call notify_parent with success and this exact message: [CHAINLINK-CODEX-WORKER-DONE] issue ready for root close.
7. Stop.

5. After the dev leaf success notification, call the ExoMonad chainlink_issue_close MCP tool with the issue ID and summary: [CHAINLINK-CODEX-TL-CLOSE] Codex root close complete.
6. Stop and idle.

## Hard Rules

1. Do not create a team; Codex uses tmux routing, not Claude Teams.
2. Do not run `gh` commands.
3. Do not create commits, branches, PRs, or files yourself.
4. Do not use tools other than the requested ExoMonad MCP tools.
5. Spawn exactly one Codex dev leaf.
6. Do not do the dev leaf work yourself.
