# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- Fix: update root TL prompt sanity check for headless OpenCode ACP workers (#45)
- Fix: remove --model from opencode serve, pass to opencode run --attach instead (#44)
- Bug: --worker-model not forwarded from exomonad init to exomonad serve (#49)
- Bug: fork_wave still spawns Gemini despite --worker=opencode (binary may predate fix) (#48)
- Fix: update root TL prompt sanity check for headless OpenCode ACP workers (#45)
- fork_wave/spawn_subtree handler ignores config.spawn_agent_type (hardcodes Claude fallback) (#34)
- spawn_worker handler ignores config.spawn_agent_type (hardcodes Gemini fallback) (#33)
- Fix --worker flag ignored — spawn_worker falls back to hardcoded Gemini (#36)

### Added
- Add OpenCode unit tests, models subcommand, and E2E test targets (#74)
- Unit tests: build_agent_command for OpenCode agent type (#51)
- Unit tests: OpenCode config parsing (tl_model, worker_model, opencode_as_tl) (#50)
- Add exomonad models discovery subcommand (#46)
- E2E verify: --worker=opencode spawns OpenCode workers (#35)
- Add default_spawn_agent_type() getter to AgentControlService (#32)
- inject agent.md into sub-agent initial prompt (#31)
- update tl.md with {{spawn_agent_type}} placeholder (#29)
- interpolate role context template at spawn time (#28)
- inject EXOMONAD_SPAWN_AGENT_TYPE in common_spawn_env (#27)
- add --opencode and --claude-code init flags (#26)
- add spawn_agent_type to config.rs (#25)
- Auto-create local bare repo when no git remote is configured (#23)
- Spawn OpenCode in headless ACP server mode (opencode acp --port 0) (#20)
- Wire ACP into send_message/notify_parent routing for OpenCode agents (#12)
- Deliver initial task to OpenCode via ACP connect_and_prompt() (#11)
- Capture ACP port from opencode acp stdout and register in AcpRegistry (#10)
- Spawn OpenCode in headless ACP server mode (opencode acp --port 0) (#9)
- Propagate routing chain to OpenCode children (propagate_team_to_child equivalent) (#5)
- Server-side auto-register OpenCode agents in AgentStore at spawn time (#3)
- Propagate routing chain to OpenCode children (propagate_team_to_child equivalent) (#17)
- Server-side auto-register OpenCode agents in AgentStore at spawn time (#15)
- **FixesPushed event**: Poller fires `fixes_pushed` event when leaf addresses Copilot review and pushes fixes. Copilot does NOT re-review — this is the actionable signal for the TL to merge.
- **Dual timeout**: 15 minutes for initial Copilot review, 5 minutes after leaf addresses changes (since Copilot won't re-review).
- **Event handler dispatch**: Third dispatch category alongside tools and hooks. GitHub poller calls WASM `handle_event` for PR review events (reviews, approvals, timeouts, fixes pushed) and sibling merge events.
- **CI status change events**: Route CI status transitions through WASM event handlers.
- **Sibling merge notification**: Event when a sibling PR merges (rebase may be needed).
- **Pragma corruption guard**: PreToolUse hook blocks edits that corrupt Haskell `#-}` LANGUAGE pragma closings.
- **Bidirectional messaging**: `send_message` tool for arbitrary agent-to-agent messaging (routes via Teams inbox, ACP, UDS, or Zellij).
- **ACP messaging**: Structured JSON-RPC messaging via Agent Client Protocol for Gemini agents.
- **HTTP-over-UDS delivery**: `notify_parent` → POST to `.exo/agents/{name}/notify.sock` for custom binary agents.
- **Coordination mutexes**: In-memory `MutexRegistry` with FIFO wait queues and TTL auto-expiry for parallel agents.
- **KV store**: Persistent key-value store via `.exo/kv/` for cross-agent state.
- **Claude session registry**: Track Claude session UUIDs for `--fork-session` context inheritance.
- **`exomonad reload`**: Clear WASM plugin cache (hot reload on next call).
- **`exomonad shutdown`**: Graceful server shutdown.

### Changed
- Investigate existing RunProcess/ExecCommand effect in wasm-guest Effects/ (#61)
- Create .exo/roles/devswarm/context/chainlink-worker.md (#58)
- Create .exo/roles/devswarm/context/chainlink-tl.md (#57)
- spawn_worker ignores agent_type: hardcodes AgentType::Gemini in Rust (#24)
- Replace JSON-RPC HTTP client in opencode_acp.rs with opencode run --attach (#22)
- Fix opencode_acp.rs: change 'opencode acp' to 'opencode serve' (#21)
- Remove SpawnOpencodeC effect and spawnOpencodeCore from Haskell (#6)
- Make tmux STDIN injection primary delivery path for OpenCode agents (#4)
- Fix fork-session CLI flags for OpenCode: --resume/--fork-session → --session/--fork (#2)
- Strip invalid --dangerously-skip-permissions flag from OpenCode command builder (#1)
- Remove spawn_opencode Rust implementation from spawn.rs (#19)
- Remove spawn_opencode Rust implementation from spawn.rs (#8)
- Remove spawn_opencode handler in handlers/agent.rs (#7)
- Remove SpawnOpencodeC effect and spawnOpencodeCore from Haskell (#18)
- Make tmux STDIN injection primary delivery path for OpenCode agents (#16)
- Fix fork-session CLI flags for OpenCode: --resume/--fork-session → --session/--fork (#14)
- Strip invalid --dangerously-skip-permissions flag from OpenCode command builder (#13)
- **Teams inbox delivery** replaces Zellij stdin injection as primary delivery mechanism. `notify_parent` → Teams inbox → native `<teammate-message>` in parent conversation.
- **`notify_parent` is a message bus**, not a completion signal. Used for status updates, failure escalation, and structured results.
- **Event-driven PR review flow**: Stop hook and prompts updated for event handler architecture. Leaf agents no longer need to manually call `notify_parent` after Copilot review — the event system handles it.

## [0.1.0] - 2026-02-24

### Added
- Agent orchestration: `spawn_subtree` (Claude), `spawn_leaf_subtree` (Gemini), `spawn_workers` (Gemini panes)
- PR workflow: `file_pr`, `merge_pr`, `notify_parent`
- Haskell WASM effect system with typed effects and Rust host handlers
- Hot reload for WASM tools in serve mode
- Zellij plugin for agent status display and interactive popup UI
- Event logging (JSONL) and GitHub poller for CI/review status
- Role system: TL, Dev, Worker roles with permission cascades
- `exomonad init` for session bootstrap (server tab + TL tab)
