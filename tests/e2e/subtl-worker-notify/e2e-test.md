# E2E Sub-TL Worker Notify Test Mode

This is an automated E2E test for the pane-pinning notify_parent path. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in sub-TL worker notify test mode. The validator process observes tmux panes and `.exo/logs`.

## Do This Now

1. Spawn exactly one Codex sub-TL with the ExoMonad `fork_wave` MCP tool.
2. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: subtl-worker-notify-tl
- agent_type: codex
- fork_session: false
- task:

You are a Codex sub-TL in the sub-TL worker notify E2E test. Do exactly these steps:

1. Spawn exactly one Codex worker pane. Use the normal worker-spawn tool available to TL roles. The worker name/slug must be subtl-worker-notify-worker.
2. Give the worker this exact task:

   You are a Codex worker in the sub-TL worker notify E2E test. Do exactly these steps:
   1. Use the ExoMonad notify_parent MCP tool with status='success' and message='[SUBTL-WORKER-NOTIFY] Worker notify_parent reached the sub-TL.'
   2. Stop.

3. After spawning the worker, do not switch tmux panes yourself. Leave the worker pane active if the runtime made it active.
4. Stop and idle. Do not call notify_parent yourself.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one Codex sub-TL and exactly one Codex worker.
5. Do not do the sub-TL or worker work yourself.
