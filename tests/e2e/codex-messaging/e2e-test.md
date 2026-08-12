# E2E Codex Messaging Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in Codex messaging test mode. The validator process observes tmux, generated Codex configs, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Spawn exactly one Codex dev leaf with the ExoMonad spawn_codex MCP tool.
2. After the dev leaf is spawned, use the ExoMonad send_tmux_message MCP tool with recipient codex-messaging-dev-codex and the message marker below.
3. Stop and idle after the direct message is sent.

## spawn_codex Spec

Spawn one Codex dev leaf:

- branch_name: codex-messaging-dev
- task:

You are a Codex dev leaf in the Codex messaging E2E test. Do exactly these steps:

1. Wait briefly for a direct message from the root containing [CODEX-MSG-ROOT-TO-DEV].
2. Use the ExoMonad notify_parent MCP tool with status='success' and message='[CODEX-MSG-DEV-NOTIFY] Codex dev messaging notification complete.'
3. Stop.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one Codex dev leaf and then stop.
5. Do not spawn a placeholder leaf or do the leaf work yourself.
6. Do not do the TL or dev leaf work yourself.
