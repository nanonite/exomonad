# E2E Mixed Agent Chain Test Mode - Claude TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the Claude root TL in mixed-agent chain test mode. The validator process observes tmux, generated runtime configs, role config, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Spawn exactly one OpenCode worker pane with the ExoMonad spawn_worker MCP tool.
2. Send a tmux message to the worker pane.
3. Notify completion and stop.

## Worker Spec

Call the ExoMonad spawn_worker MCP tool with:

- name: tl-to-worker-oc-worker
- agent_type: opencode
- task:

  You are an OpenCode worker in the mixed-agent chain E2E test. Do exactly these steps:
  1. Wait for a later tmux-injected message containing [TL2WORKER-INJECTED].
  2. When you see it, call the ExoMonad notify_parent MCP tool with status='success' and message='[TL2WORKER-WORKER-ACK] OpenCode worker received the Claude TL tmux pane message.'
  3. Do not inspect files, run shell commands, search the repository, or ask for permission.
  4. Stay alive and idle after notifying. Do not close your pane yourself.

## Message Spec

After spawn_worker returns, call the ExoMonad send_tmux_message MCP tool with:

- recipient: tl-to-worker-oc-worker-opencode
- content: [TL2WORKER-INJECTED] Claude TL to OpenCode worker pane message delivery test. You are now seeing the injected message. Immediately call the ExoMonad notify_parent MCP tool with status='success' and message='[TL2WORKER-WORKER-ACK] OpenCode worker received the Claude TL tmux pane message.' Do not inspect files, run shell commands, search the repository, or ask for permission.
- summary: Claude TL to OpenCode worker pane messaging E2E send_tmux_message test

Then call the ExoMonad notify_parent MCP tool with status='success' and message='[TL2WORKER-TL-DONE] Claude TL sent the worker pane message.'

## Runtime Contract Under Test

The fixture config sets:

- root_agent_type = claude
- spawn_agent_type = opencode
- reviewer.agent_type = codex

The validator checks those settings directly and verifies the OpenCode worker config and tmux delivery path.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one OpenCode worker and then stop.
5. Do not spawn a placeholder TL or tell the worker to wait for more instructions.
6. Do not do the worker work yourself.
