# Task-blocked human-gate transport acceptance

This transport acceptance harness starts a disposable repository, local
Forgejo-shaped API, real ExoMonad server, real WASM tool surface, and real
Unix-socket MCP transport. It creates a scoped leaf, records typed
agent.task_blocked and tl.slice_parked events through that transport, restarts
the server, and resumes the same owner only after the durable human issue and
event records are reused.

The evidence proves durable event persistence, stale-invocation rejection,
same-owner branch/worktree preservation, and PR publication after resume. The
blocked and parked events are injected fixture inputs; this harness does not
claim to exercise the TL controller's parking decision or the CI watcher's
base/head attribution. Those paths require dedicated controller/watcher
acceptance coverage. Each run owns its temporary Git remote, database, tmux
session, server, worktree, and mock API.
