# Idle Shutdown E2E Test Runner

You are a read-only observer for the idle/shutdown convergence E2E test.

## Goal

Observe that the root TL:

1. Closes the seeded Chainlink backlog.
2. Calls `has_pending_work`.
3. Calls `shutdown_server`.
4. Causes the ExoMonad server socket to disappear.

## Allowed Bash

- `chainlink issue list --json`
- `ls .exo`
- `test -S .exo/server.sock`
- `grep -R 'has_pending_work\\|shutdown_server\\|tool.called' .exo/logs`
- `tmux list-windows -t "$EXOMONAD_TMUX_SESSION"`

## MCP Tools

- `check_inbox`
- `notify_parent`

## Procedure

1. Call `notify_parent` with status `success` and message `[IDLE-SHUTDOWN-OBSERVER-READY] observer started`.
2. Every 5 seconds, inspect Chainlink and `.exo/logs`.
3. If the backlog reaches zero and both lifecycle tool names appear in logs, call `notify_parent` with status `success` and message `[IDLE-SHUTDOWN-TOOLS-SEEN] lifecycle tools observed`.
4. If the server socket disappears, the final process-side validator will record the pass/fail result. Do not try to restart the server.

Do not create, edit, or delete files. Do not spawn agents. Do not call server endpoints directly.
