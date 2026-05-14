# E2E Codex Messaging Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in Codex messaging test mode. The validator process observes tmux, generated Codex configs, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Spawn exactly one Codex sub-TL with the ExoMonad fork_wave MCP tool.
2. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: codex-messaging-tl
- agent_type: codex
- fork_session: false
- task:

You are a Codex TL in the Codex messaging E2E test. Do exactly these steps:

1. Spawn exactly one Codex dev leaf. Use the normal leaf-spawn tool available to TL roles. The leaf name/slug must be codex-messaging-dev. The leaf must run as a dev leaf, not as a TL.
2. Give the dev leaf this exact task:

   You are a Codex dev leaf in the Codex messaging E2E test. Do exactly these steps:
   1. Wait briefly for a message from your parent TL containing [CODEX-MSG-TL-TO-DEV].
   2. Use the ExoMonad notify_parent MCP tool with status='success' and message='[CODEX-MSG-DEV-NOTIFY] Codex dev messaging notification complete.'
   3. Stop.

3. After the dev leaf is spawned, use the ExoMonad send_message MCP tool with:
   - recipient: codex-messaging-dev-codex
   - content: [CODEX-MSG-TL-TO-DEV] Codex TL to dev tmux message delivery test.
   - summary: Codex messaging E2E send_message test
4. Use the ExoMonad notify_parent MCP tool with status='success' and message='[CODEX-MSG-TL-NOTIFY] Codex TL messaging notification complete.'
5. Stop.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one Codex TL and then stop.
5. Do not spawn a placeholder TL or tell the TL to wait for more instructions.
6. Do not do the TL or dev leaf work yourself.
