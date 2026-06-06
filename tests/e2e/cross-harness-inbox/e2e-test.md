# E2E Cross-Harness Inbox Test

This fixture validates the SQLite-backed cross-harness inbox path without relying on live model CLIs. It starts `exomonad serve`, seeds a Gemini-shaped agent identity and tmux routing pane, sends messages through live MCP tool calls, and verifies piggyback unread mail, explicit `check_inbox`, `list_agents` unread metadata, and watcher timeout poke delivery.
