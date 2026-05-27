# Claude-only E2E Smoke

`run.sh` starts a temporary ExoMonad project with a Claude Code root TL on Haiku and validates only role-safe root behavior: server startup, root SessionStart registration, TeamCreate, and Teams metadata registration. It intentionally does not ask the TL to write files or spawn child agents.

Use `KEEP_E2E_WORKDIR=1` to retain the temp repository for debugging.
