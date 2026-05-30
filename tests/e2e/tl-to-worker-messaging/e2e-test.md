# E2E TL-to-Worker Messaging Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in TL-to-worker messaging test mode. The validator process observes tmux, generated runtime configs, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Spawn exactly one Codex sub-TL with the ExoMonad fork_wave MCP tool.
2. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: tl-to-worker-messaging-tl
- agent_type: codex
- fork_session: false
- task:

You are a Codex TL in the TL-to-worker messaging E2E test. Do exactly these steps:

1. Spawn exactly one OpenCode worker pane with the ExoMonad spawn_worker MCP tool.
   - name: tl-to-worker-oc-worker
   - agent_type: opencode
   - task:

     You are an OpenCode worker in the TL-to-worker messaging E2E test. Do exactly these steps:
     1. Wait for a later tmux-injected message containing [TL2WORKER-INJECTED].
     2. When you see it, call the ExoMonad notify_parent MCP tool with status='success' and message='[TL2WORKER-WORKER-ACK] OpenCode worker received the TL tmux pane message.'
     3. Do not inspect files, run shell commands, search the repository, or ask for permission.
     4. Stay alive and idle after notifying. Do not close your pane yourself.

2. After spawn_worker returns, use the ExoMonad send_tmux_message MCP tool with:
   - recipient: tl-to-worker-oc-worker-opencode
   - content: [TL2WORKER-INJECTED] Codex TL to OpenCode worker pane message delivery test. You are now seeing the injected message. Immediately call the ExoMonad notify_parent MCP tool with status='success' and message='[TL2WORKER-WORKER-ACK] OpenCode worker received the TL tmux pane message.' Do not inspect files, run shell commands, search the repository, or ask for permission.
   - summary: TL-to-worker pane messaging E2E send_tmux_message test
3. Use the ExoMonad notify_parent MCP tool with status='success' and message='[TL2WORKER-TL-DONE] Codex TL sent the worker pane message.'
4. Stop.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one Codex TL and then stop.
5. Do not spawn a placeholder TL or tell the TL to wait for more instructions.
6. Do not do the TL or worker work yourself.
