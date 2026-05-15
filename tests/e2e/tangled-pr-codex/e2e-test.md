# E2E Tangled PR Codex Test Mode - Root TL Protocol

This is an automated E2E test. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

You are the root TL in Tangled PR Codex test mode. The validator process observes tmux, generated Codex configs, local Tangled git refs, local PR/review state, spindle CI events, and `.exo/logs`.
This test is local-only. GitHub auth is intentionally unset, and file_pr must use ExoMonad's local `.exo/prs.json` review flow. Do not run gh auth status or use gh pr commands.
The success condition is not plain PR filing or reviewer approval. The dev leaf must remain alive until ExoMonad observes both reviewer approval and successful Tangled Spindle CI, records `[MERGE READY]`, and delivers the merge-ready release message back to the dev leaf.

## Do This Now

1. Spawn exactly one Codex sub-TL with the ExoMonad fork_wave MCP tool.
2. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: tangled-pr-codex-tl
- agent_type: codex
- fork_session: false
- task:

You are a Codex TL in the Tangled PR Codex E2E test. Do exactly these steps:

1. Spawn exactly one Codex worker with the ExoMonad spawn_worker MCP tool:
   - name: tangled-pr-codex-worker
   - agent_type: codex
   - task:

     You are a Codex worker in the Tangled PR Codex E2E test. Do exactly these steps:

     1. Use the ExoMonad notify_parent MCP tool with status='success' and message='[TANGLED-PR-CODEX-WORKER-DONE] Codex worker tmux delivery complete.'
     2. Stop.

2. Spawn exactly one Codex dev leaf. Use the normal leaf-spawn tool available to TL roles. The leaf name/slug must be tangled-pr-codex-dev. The leaf must run as a dev leaf, not as a TL.
3. Give the dev leaf this exact task:

   You are a Codex dev leaf in the Tangled PR Codex E2E test. Do exactly these steps:
   1. Write a file named tangled-pr-codex-dev-output.txt in your current directory containing the single line: Tangled PR Codex dev output
   2. Stage and commit it with message: e2e: add tangled pr codex dev output
   3. Use the ExoMonad file_pr MCP tool to file a local PR for your branch. Do not check GitHub auth; local `.exo/prs.json` review is expected.
   4. Use the ExoMonad notify_parent MCP tool with status='success' and message='[TANGLED-PR-CODEX-DEV-DONE] Codex dev filed Tangled PR.'
   5. Stay alive and wait for watcher-delivered review, CI, and merge-ready messages. Do not stop until merge-ready is delivered. If reviewer changes are requested, address them in the same worktree and stay alive. If a `[STUCK: ...]` message arrives, wait for TL clarification.

4. After spawning the worker and dev leaf, stop and idle. Do not merge anything. Do not do the worker or dev leaf work yourself.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use shell commands to fake validator artifacts, local PR state, review state, CI state, or logs.
4. Spawn exactly one Codex TL, exactly one Codex worker, and exactly one Codex dev leaf.
5. Do not use `codex exec review`; reviewer agents must be normal ExoMonad reviewer-role agents.
6. Do not merge the PR.
7. The dev leaf must not stop immediately after filing the PR.
8. Do not treat reviewer approval alone as completion; the watcher must deliver merge-ready after CI and review are both satisfied.
