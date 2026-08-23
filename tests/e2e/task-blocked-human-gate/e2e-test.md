# Task-blocked human-gate E2E

This acceptance harness starts a disposable repository, local Forgejo-shaped
API, real ExoMonad server, real WASM tool surface, and real Unix-socket MCP
transport. It seeds a scoped leaf whose base CI is independently unstable,
records a typed `agent.task_blocked` handoff, restarts the server, and resumes
the same owner only after the durable human issue and parked event are reused.

The evidence proves the ordered lifecycle `spawned → parked → spawned →
in_review`, preserves the branch/worktree, attributes difficulty to the base
failure, and publishes a PR after resume. It also rejects stale invocation
resumption and checks for duplicate blocked events. Each run owns its temporary
Git remote, database, tmux session, server, worktree, and mock API.
