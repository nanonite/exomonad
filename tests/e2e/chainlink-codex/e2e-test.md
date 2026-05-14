# E2E Chainlink Codex Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in Chainlink Codex test mode. The validator process observes generated Codex configs, Chainlink state, tmux delivery logs, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Spawn exactly one Codex sub-TL with the ExoMonad fork_wave MCP tool.
2. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: chainlink-codex-tl
- agent_type: codex
- fork_session: false
- task:

You are a Codex TL in the Chainlink Codex E2E test. Do exactly these steps:

1. Call the ExoMonad chainlink_issue_create MCP tool with:
   - title: E2E chainlink codex worker
   - priority: low
   - labels: e2e,chainlink,codex
2. Save the returned issue ID.
3. Call the ExoMonad chainlink_session_status MCP tool.
4. Spawn exactly one Codex worker with the ExoMonad spawn_worker MCP tool:
   - name: chainlink-codex-worker
   - agent_type: codex
   - task:

     You are a Codex worker in the Chainlink Codex E2E test. Do exactly these steps:

     1. Use the issue ID provided by the TL: <issue_id from the TL>.
     2. Call the ExoMonad chainlink_session_start MCP tool.
     3. Call the ExoMonad chainlink_session_work MCP tool with that issue ID.
     4. Call the ExoMonad chainlink_issue_comment MCP tool with that issue ID and this exact message: [CHAINLINK-CODEX-WORKER-COMMENT] Codex worker comment recorded.
     5. Call the ExoMonad chainlink_session_end MCP tool with notes: [CHAINLINK-CODEX-WORKER-DONE] Codex worker session complete.
     6. Call notify_parent with success and this exact message: [CHAINLINK-CODEX-WORKER-DONE] issue ready for TL close.
     7. Stop.

5. After spawning the worker, wait for the worker success notification.
6. Call the ExoMonad chainlink_issue_close MCP tool with the issue ID and summary: [CHAINLINK-CODEX-TL-CLOSE] Codex TL close complete.
7. Stop and idle.

## Hard Rules

1. Do not create a team; Codex uses tmux routing, not Claude Teams.
2. Do not run `gh` commands.
3. Do not create commits, branches, PRs, or files yourself.
4. Do not use tools other than the requested ExoMonad MCP tools.
5. Spawn exactly one Codex TL and exactly one Codex worker.
6. Do not do the TL or worker work yourself.
