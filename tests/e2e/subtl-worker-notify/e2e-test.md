# E2E Worker Notify Test Mode

This is an automated E2E test for the pane-pinning notify_parent path. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in worker notify test mode. The validator process observes tmux panes and `.exo/logs`.

## Do This Now

1. Spawn exactly one Codex worker pane with the ExoMonad spawn_worker MCP tool.
2. Stop and idle after the worker is spawned; do not switch panes yourself.

## spawn_worker Spec

Spawn one worker:

- name: subtl-worker-notify-worker
- agent_type: codex
- task:

You are a Codex worker in the worker notify E2E test. Do exactly these steps:

1. Use the ExoMonad notify_parent MCP tool with status='success' and message='[SUBTL-WORKER-NOTIFY] Worker notify_parent reached the root pane.'
2. Stop.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one Codex worker.
5. Do not do the worker work yourself.
