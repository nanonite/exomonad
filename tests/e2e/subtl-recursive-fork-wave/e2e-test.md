# E2E Recursive Fork Wave Test Mode

This is an automated E2E test for recursive sub-TL fork/worker routing. Execute the steps below immediately on your first turn. Do not research, browse files, or do unrelated work.

Runtime under test: __RUNTIME__
Fork-session mode: __FORK_SESSION__

You are the root TL in recursive fork_wave test mode. The validator process observes generated runtime configs, tmux panes, `.exo/logs`, and TL phase transition logs.

## Do This Now

1. Spawn exactly one __RUNTIME__ sub-TL with the ExoMonad `fork_wave` MCP tool.
2. Stop and idle after the sub-TL is spawned.

## fork_wave Spec

Spawn one agent:

- name: recursive-subtl
- agent_type: __RUNTIME__
- fork_session: __FORK_SESSION__
- task:

You are a __RUNTIME__ sub-TL in the recursive fork_wave E2E test. Do exactly these steps:

1. Spawn exactly one __RUNTIME__ worker pane. Use the normal worker-spawn tool available to TL roles. The worker name/slug must be recursive-worker.
2. Give the worker this exact task:

   You are a __RUNTIME__ worker in the recursive fork_wave E2E test. Do exactly these steps:
   1. Use the ExoMonad notify_parent MCP tool with status='success' and message='[RECURSIVE-WORKER-DONE] Worker notified recursive sub-TL.'
   2. Stop.

3. Wait until you receive the worker notification containing [RECURSIVE-WORKER-DONE].
4. Use the ExoMonad notify_parent MCP tool with status='success' and message='[RECURSIVE-SUBTL-DONE] Recursive sub-TL received worker notification and notified root.'
5. Stop.

## Hard Rules

1. Do not run `gh` commands.
2. Do not create commits, branches, PRs, or files yourself.
3. Do not use tools other than the requested ExoMonad MCP tools.
4. Spawn exactly one sub-TL and exactly one worker.
5. Do not do the sub-TL or worker work yourself.
