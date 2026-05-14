# E2E Codex Hooks Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in Codex hooks test mode. The validator process observes tmux, generated config, hook logs, and worktree artifacts.
This test is local-only. GitHub auth is intentionally unset, and file_pr must use ExoMonad's local .exo/prs.json review flow. Do not run gh auth status or use gh pr commands.

## Do This Now

1. Create a team with the ExoMonad team creation MCP tool.
2. Spawn exactly one Codex sub-TL with the ExoMonad fork_wave MCP tool.
3. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: codex-hooks-tl
- agent_type: codex
- fork_session: false
- task:

You are a Codex TL in the Codex hooks E2E test. Do exactly these steps:

1. Create a team with the ExoMonad team creation MCP tool.
2. Spawn exactly one Codex dev leaf. Use the normal leaf-spawn tool available to TL roles. The leaf name/slug must be codex-hooks-dev. The leaf must run as a dev leaf, not as a TL.
3. Give the dev leaf this exact task:

   You are a Codex dev leaf in the Codex hooks E2E test. Do exactly these steps:
   1. Write a file named codex-hooks-dev-output.txt in your current directory containing the single line: Codex dev hook test passed
   2. Stage and commit it with message: e2e: add codex hooks dev output
   3. Push your branch.
   4. Use the ExoMonad file_pr MCP tool to file a local PR for your branch. Do not check GitHub auth; local .exo/prs.json review is expected.
   5. Use the ExoMonad notify_parent MCP tool with status='success' and message='[CODEX-HOOKS-DEV-DONE] Codex dev hook test complete.'
   6. Stop.

4. After spawning the dev leaf, stop and idle. Do not merge anything. Do not do the dev leaf's work yourself.

## Never Do These Things

- Never write or commit files yourself.
- Never create more than one sub-TL.
- Never create more than one dev leaf.
- Never use codex exec review; reviewer agents must be normal ExoMonad reviewer-role agents.
- Never run gh auth status, gh pr create, or any external GitHub PR command.
- Never call server endpoints directly.
- Never use shell commands to fake hook logs or validator artifacts.
