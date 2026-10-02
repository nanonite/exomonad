---
paths:
  - "**/*.rs"
  - "**/*.sh"
---

# tmux Environment Rules

## A pane's environment is fixed at spawn

tmux snapshots a pane's environment when it spawns the pane's process, and never
re-reads it. The snapshot layers the server's global environment, then the session
environment, then the window environment.

Consequences that decide where a value has to be written:

- `set-environment -t <session>` after a window exists reaches only windows created
  *afterwards*. It rewrites the session and window environments, which `show-environment`
  then reports, so a session-environment read agrees with `init` even when the pane
  still runs on the value it was spawned with.
- A new session starts from an **empty** session environment and falls back to the
  server's global environment, which the server captured from whichever process
  started it. With a tmux server already running — another workspace, a parallel
  scenario, a developer's own session — that captured value is not the caller's.
- `new-session -e NAME=value` (tmux 3.2+) merges into the session environment
  *before* tmux spawns the first window, so the first window is born with it and every
  window created later inherits it from the session. It leaves the global environment
  untouched. `TmuxIpc::new_session`'s `environment` argument is this mechanism.
- `new-window -e`, `split-window -e`, and `respawn-pane -e` are the same mechanism for
  windows created later.

## Rules

- A value the process tmux spawns in a pane must resolve has to travel into the command
  that creates that pane. Never write it with a later `set-environment` and assume the
  first window sees it.
- Do not mutate the server's global environment (`set-environment -g`) to fix a
  session's environment. Every other session on that server falls back to it.
- `-e` puts values on the tmux client's command line. Never pass a credential that way;
  use `set-environment` for the windows created later, which do read it.
- To prove a pane resolved a value, read the spawned process: `#{pane_pid}` plus
  `/proc/<pid>/environ`. `show-environment` cannot distinguish a pane that inherited a
  value from one that was told about it afterwards.
- `TmuxIpc::new_session` requires tmux 3.2 or newer on the paths that pass a
  non-empty `environment`; it names the requirement when the tmux build rejects `-e`.