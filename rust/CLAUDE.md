# ExoMonad Rust Workspace

Rust host for ExoMonad's resumable Python TL controller and its agent tools.

The `tl_loop` Python program owns planning, dispatch, checkpoint recovery, review and CI gates, and merge decisions. It makes bounded model calls for structured judgments. Rust supplies the server, WASM tool runtime, effects, hooks, tmux and worktree operations. A human can observe and steer a run in tmux or through controller gates; the TL is not a persistent Claude Code conversation.

## Architecture

Agent tool definitions and routing live in Haskell WASM. The Python controller decides *when* to call them; Rust hosts the plugin and performs I/O through effect handlers.

```
Python tl_loop controller ── HTTP over .exo/server.sock ──┐
Agent MCP clients / hooks ── exomonad mcp-stdio / hook ───┤
                                                     Rust exomonad serve
                                                            ↓
                                             Haskell WASM tool routing
                                                            ↓
                                       Rust effect handlers perform I/O
                                                            ↓
                                 Result returns to caller; Python records
                                 run transitions in its durable event ledger
```

### Key Components

| Component | Purpose |
|-----------|---------|
| **exomonad** | Rust binary: init, server, MCP proxy, hook forwarding, and cleanup |
| **exomonad-core** | WASM runtime, effect framework, handlers, services, and protocol types |
| **exomonad-proto** | Proto-generated types (prost) for FFI + effects |
| **wasm-guest** | Haskell WASM plugin (pure logic, no I/O) |
| **tl_loop** | Python controller (outside this Rust workspace) for durable orchestration |

### Deployment

**Local controller and server:**

```
exomonad init
├── Server window: exomonad serve (WASM from .exo/wasm/)
├── TL window: python3 ~/.exo/tl_loop.pyz run (durable controller)
└── Watcher/dashboard window
    └── dispatched sub-TLs and leaves: tmux processes and git worktrees
```

`exomonad init` packages the Python controller, starts the Rust server, waits for its socket, then launches the controller. The controller reads a structured plan and persists its run state and event ledger. It calls role-scoped server tools over the Unix socket to provision sub-TLs, spawn leaves and workers, observe PRs, and perform approved transitions. Worktree leaves have a branch and PR; inline workers use a tmux pane in their parent's worktree. Harnesses are selected by policy and configuration, so neither role is tied to one model vendor. Recursive sub-TL branches and PR bases are tracked by the plan and publication state; do not infer ownership from a branch name alone.

## Documentation Tree

```
rust/CLAUDE.md  ← YOU ARE HERE (router)
├── exomonad/CLAUDE.md  ← Server, CLI, MCP proxy, and hook handler (BINARY)
│   • Binary: exomonad
│   • hook subcommand: handles CC hooks via WASM
│
├── exomonad-core/  ← Unified library (publishable)
│   • Framework: EffectHandler trait, EffectRegistry, RuntimeBuilder, Runtime
│   • PluginManager (single host fn: yield_effect)
│   • MCP types (ToolDefinition, tools module)
│   • Protocol types (hook, mcp, service)
│   • Handlers: GitHandler, GitHubHandler, LogHandler, AgentHandler,
│     FsHandler, FilePRHandler, CopilotHandler
│   • Services: GitService, GitHubService, AgentControlService, TmuxIpc, etc.
│   • External service clients and telemetry
│   • tmux IPC (via `std::process::Command`, buffer pattern for input injection)
│
├── exomonad-proto/  ← Proto-generated types (prost)
│   • FFI boundary types and effect request/response messages
├── claude-teams-bridge/  ← Claude Teams compatibility bridge
└── exomonad-test-support/  ← Shared Rust test scaffolding
```

## Workspace Members

| Crate | Type | Purpose |
|-------|------|---------|
| [exomonad](exomonad/CLAUDE.md) | Binary (`exomonad`) | Init, server, MCP proxy, hooks, and cleanup |
| exomonad-core | Library | Framework, handlers, services, protocol types, UI protocol |
| exomonad-proto | Library | Proto-generated types (prost) for FFI + effects |
| claude-teams-bridge | Library | Claude Teams compatibility bridge |
| exomonad-test-support | Library (dev-only) | Shared test scaffolding |

### Feature Flags (exomonad-core)

| Feature | Default | Description |
|---------|---------|-------------|
| `runtime` | Yes | Full runtime: WASM hosting, effect handlers, services |

## Quick Reference

### Building

All `cargo` commands run from the repo root (workspace `Cargo.toml` lives there):

```bash
cargo build --release                    # Build all crates
cargo build -p exomonad                  # Build exomonad binary
cargo test --workspace                   # Run all tests

# Build WASM plugin (requires nix develop .#wasm)
nix develop .#wasm -c wasm32-wasi-cabal build --project-file=cabal.project.wasm wasm-guest
```

### Harness selection and effort policy

`exomonad init` launches the Python controller as root TL. The controller
selects TL, worker, and reviewer harness/model entries from the human-authored
`.exo/harness_policy.toml` allowlist and capability map, within its budget.
`exomonad init --worker` and `--reviewer` configure the corresponding spawned
agent defaults; `--worker-model`, `--worker-effort-level`,
`--reviewer-model`, and `--reviewer-effort-level` refine those defaults.
Supported agent harnesses include Claude, OpenCode, Codex, and Shoal where the
selected role supports them. Codex receives the resolved reasoning effort
after model capability validation; OpenCode uses a supported model variant.

Coding spawns stay on the configured worker harness. An explicit
cross-harness coding request requires human approval through
EXOMONAD_ALLOW_HARNESS_SWITCH=1; otherwise the host emits structured
agent.stuck guidance. resume_pr always reuses the persisted owner harness,
worktree, branch, and PR.


### Running
```bash
# Server (normally started by exomonad init)
exomonad serve

# Agent-facing stdio MCP proxy to the running server
exomonad mcp-stdio --role tl --name root

# Handle Claude Code hook
echo '{"hook_event_name":"PreToolUse",...}' | exomonad hook pre-tool-use
```

**Note:** WASM is loaded from `.exo/wasm/` at runtime. To update WASM, run `just wasm-all` or `exomonad recompile --role devswarm`.

### Environment Variables
| Variable | Used By | Purpose |
|----------|---------|---------|
| `FORGEJO_TOKEN` | services | Forgejo API access |
| `RUST_LOG` | all | Tracing log level |
| `EXOMONAD_AGENT_ID` | agent spawn | Agent identity for spawned agents (read at spawn time) |
| `EXOMONAD_SESSION_ID` | agent spawn | Parent's birth-branch, used for routing `notify_parent` |
| `EXOMONAD_ROLE` | agent spawn | Agent's role name (tl, dev, worker) |
| `EXOMONAD_TMUX_SESSION` | tmux_events, agent_control | tmux session name for IPC. Set globally via `tmux set-environment` during `exomonad init`; inherited by all windows/panes |
| `EXOMONAD_SWARM_RUN_ID` | agent spawn, logging | Swarm run ID (OTel resource attribute, propagated to children) |
| `EXOMONAD_PARENT_AGENT` | agent spawn, logging | Parent agent's birth branch (OTel resource attribute) |

### Agent Identity

In `mcp-stdio` mode, the agent's identity is passed via `--role {role} --name {name}`. The Python controller uses the corresponding role/name paths on the Unix-socket HTTP API. Role determines the WASM tool set. Each agent gets a `PluginManager` with an `EffectContext` (agent name and birth branch) resolved by the server. Effect handlers receive that context.

Roles are defined in Haskell WASM (`AllRoles.hs`). Adding a role is a Haskell-only change — Rust uses a lazy cache that creates a `PluginManager` per role on first request.

At spawn time, managed agents receive per-agent MCP configuration with their role and name. The server resolves the persisted identity before dispatching tools.

## MCP Tools

All tools are defined in Haskell WASM and executed via host functions.

| Tool | Role | Description |
|------|------|-------------|
| `spawn_leaf` / `spawn_worker` | coordinator roles | Start a managed worktree leaf or inline worker |
| `resume_pr` | coordinator roles | Resume the persisted owner of an existing open PR |
| `file_pr` | publishing agents | Create or update a PR for the owned branch |
| `watcher_pr_state` | coordinator roles | Observe PR head, review, and CI evidence |
| `merge_pr` | coordinator roles | Merge an eligible child PR through the host |
| `notify_parent` / `send_message` | managed agents | Route status and guidance through the server |

## Effect System

All WASM↔Rust communication flows through a single `yield_effect` host function. The Haskell guest sends protobuf-encoded `EffectEnvelope` messages, and the `EffectRegistry` dispatches to the appropriate handler by namespace prefix.

```
Haskell: runEffect @GitGetBranch request
    ↓ protobuf encode → EffectEnvelope { effect_type: "git.get_branch", payload: ... }
    ↓ yield_effect host function
    ↓ EffectRegistry::dispatch("git.get_branch", payload)
    ↓ GitHandler::handle(...)
    ↓ EffectResponse { payload | error }
    ↓ protobuf decode
Haskell: Either EffectError GetBranchResponse
```

### Error Handling Helpers

Handlers use shared ergonomic helpers from `effects/error.rs`:

- **`ResultExt::effect_err(namespace)`** — Converts any `Result<T, E: Display>` to `Result<T, EffectError>` with `EffectError::custom("{namespace}_error", e.to_string())`. Replaces verbose `.map_err(|e| EffectError::custom(...))` closures.
- **`spawn_blocking_effect(namespace, closure)`** — Runs a closure in `tokio::task::spawn_blocking` and maps both the `JoinError` and inner error to `EffectError`.

Proto field helpers in `handlers/mod.rs`: `non_empty(String) → Option<String>`, `working_dir_or_default(String) → String`, `working_dir_path_or_default(&str) → PathBuf`.

### Built-in Handlers

| Namespace | Handler | Effects |
|-----------|---------|---------|
| `git.*` | GitHandler | get_branch, get_status, get_recent_commits, get_worktree, has_unpushed_commits, get_remote_url, get_repo_info |
| `github.*` | GitHubHandler | list_issues, get_issue, create_pr, list_prs, get_pr_for_branch, get_pr_review_comments |
| `log.*` | LogHandler | info, error, emit_event |
| `agent.*` | AgentHandler | spawn_leaf_subtree, spawn_worker, resume_pr, watcher_pr_state, disposal |
| `fs.*` | FsHandler | read_file, write_file |
| `file_pr.*` | FilePRHandler | file_pr |
| `copilot.*` | CopilotHandler | wait_for_copilot_review |
| `kv.*` | KvHandler | get, set |
| `session.*` | SessionHandler | register_claude_id, register_team, deregister_team |
| `tasks.*` | TasksHandler | list_tasks, get_task, update_task (shared task list with team auto-resolution) |
| `events.*` | EventHandler | wait_for_event (internal), notify_event, notify_parent, send_message |
| `merge_pr.*` | MergePRHandler | merge_pr with PR/head and review/CI evidence checks |
| `process.*` | ProcessHandler | run (execute command with args, env, working dir, timeout) |
| `coordination.*` | CoordinationHandler | acquire_mutex, release_mutex (in-memory mutex for parallel agents) |

**tmux Integration (CLI-based):**
- All tmux communication uses `std::process::Command::new("tmux")` — simple subprocess calls
- Window management: `new-window`, `kill-window`, `list-windows` with `-F` format strings for deterministic parsing
- Pane management: `split-window`, `kill-pane` for ephemeral workers
- Input injection: buffer pattern (`load-buffer` + `paste-buffer` + 150ms debounce + `send-keys Enter`), session-qualified targets (`{session}:{target}`), per-target `Mutex` serialization
- Stable addressing: `%N` pane IDs, `@N` window IDs via `-P -F "#{pane_id}"`

## Configuration

`exomonad init` writes an MCP proxy configuration for managed agents. For a manual Claude Code client, register the proxy in `.mcp.json`:
```json
{
  "mcpServers": {
    "exomonad": {
      "command": "exomonad",
      "args": ["mcp-stdio", "--role", "tl", "--name", "root"]
    }
  }
}
```

`exomonad new` creates `.exo/config.toml`; `exomonad init` uses that project configuration and starts the server and controller.

## Testing

All commands run from repo root:

```bash
cargo test --workspace                  # All tests
cargo test -p exomonad                  # Binary tests only
cargo test -p exomonad-core             # All library tests (framework + handlers + services)
cargo test -p exomonad-proto            # Wire format compatibility tests
```

Tests that require a directory outside any git repository must use
`exomonad-test-support`'s outside-repository tempdir helper, because
`tempfile::tempdir()` honors `TMPDIR`.
The dev shell pins nix-created `TMPDIR` values back to a system temp directory,
while helpers still validate this precondition when tests run directly.

### Diagnosing Rust test failures

`just rust-test` runs the workspace library tests with `--no-fail-fast`, so a
failure reports every failing test name before the command exits. Nextest writes
the live JUnit report to `target/nextest/default/junit.xml`; when the recipe
fails, it preserves a uniquely named copy under
`target/nextest/failures/junit-*.xml` and prints that path. Read the preserved
report before starting another run, because the live report is overwritten by
the next invocation. Do not add retries, ignore a failing test, or weaken test
semantics without identifying the failure mechanism first.

`just rust-test` is the fast library-only loop. `just rust-test-all` runs the
full workspace, including all native `tests/*.rs` targets, with the same
non-fail-fast output and JUnit preservation. The aggregate `just test` gate runs
the full target set after `wasm-all`, so the WASM integration target has its
plugin available. New Rust test targets must be reachable from `just test`.

The `wasm_integration` target is assigned to a one-permit nextest group because
its tests share WASM runtime state and use `serial_test`; the group makes that
serialization work across nextest's process-per-test execution model.

The Teams bridge lock publishes complete metadata through a temporary file and
an atomic hard-link claim. Lock acquisition reclaims only temporary artifacts
whose recorded owner PID is dead; artifacts belonging to live PIDs are retained.
Successful claims are logged at debug level, while stale-lock breaks are logged
at warn level with the recorded owner PID.

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| Python controller | Durable run transitions, budgets, and recovery live in `tl_loop` |
| WASM tool routing | Haskell defines role-scoped tools and yields effects; Rust executes their I/O |
| Single `yield_effect` host fn | One entry point, all effects dispatched by namespace via EffectRegistry |
| Protobuf binary encoding | Type-safe FFI boundary, generated types on both sides |
| `runtime` feature flag | Plugin consumers get lightweight types without heavy deps |
| High-level effects | `SpawnAgent` not `CreateWorktree + OpenWindow` |
| Local tmux orchestration | Managed agents use git worktrees and tmux windows or panes; `try-exomonad` also offers a Docker wrapper |
| CLI-based tmux IPC | `std::process::Command` calls to `tmux` binary |
| Extism runtime | Mature WASM runtime with host function support |
| File-based devswarm WASM | Single WASM for all roles, loaded from disk, hot reload in serve mode |
| Project-scoped worktree lifecycle lock | `.exo/worktree-lifecycle.lock` (flock) makes worktree create/attach and residue cleanup mutually exclusive and serializes sink verification and its write, so no ownership decision is taken from a view another writer has already invalidated |

## Related Documentation

- [Root CLAUDE.md](../CLAUDE.md) - Project overview and documentation tree
- [Haskell wasm-guest](../haskell/wasm-guest/) - Haskell WASM plugin source
- [Haskell WASM guest](../haskell/wasm-guest/CLAUDE.md) - MCP tool definitions
