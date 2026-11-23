# exomonad

Unified sidecar binary: Rust host with Haskell WASM plugin.

## Architecture

**All logic is in Haskell WASM. Rust handles I/O only.**

WASM is loaded from `.exo/wasm/wasm-guest-devswarm.wasm` at runtime by `exomonad serve`. The `exomonad hook` command is a thin UDS client that forwards hook events to the running server — it does NOT load WASM itself.

```
# Hook mode (thin UDS client → server)
Claude Code → exomonad hook → UDS (.exo/server.sock) → server WASM → HookEnvelope → stdout

# MCP mode (multi-agent, devswarm WASM)
N agents → exomonad serve → UDS (.exo/server.sock) → Unified WASM (handles all roles) → effects → I/O
```

**Fail-open:** If the server is unreachable when a hook fires, `exomonad hook` prints `{"continue":true}` and exits 0. This prevents blocking the human's session.

## CLI Usage

```bash
exomonad hook pre-tool-use        # Handle Claude Code hook
exomonad mcp-stdio                # Stdio MCP server (single agent)
exomonad serve                    # UDS MCP server (multi-agent, hot reload)
exomonad recompile [--role ROLE]  # Build WASM from Haskell source
exomonad new                      # Initialize new project (.exo/config.toml, WASM, rules)
exomonad init [--session NAME]    # Initialize tmux session (Server window + TL window)
exomonad revert [--kill-session]  # Remove files/worktrees created by init
exomonad reload                   # Clear WASM plugin cache (hot reload)
exomonad shutdown                 # Gracefully shut down the running server
```

**Observability:** Structured OTel spans via axum middleware. Every agent request (`/agents/{role}/{name}/...`) gets an `agent_request` span with `agent_id`, `agent.role`, `agent.parent`, and `swarm.run_id` — no per-call-site annotation needed. `swarm.run_id` is persisted to `.exo/run_id` and set as an OTel resource attribute; child processes inherit it via `EXOMONAD_SWARM_RUN_ID` env var. Query all spans in a run: `resource.swarm.run_id = '{id}'`. Reconstruct spawn tree: `groupBy agent.parent, agent_id`.

### New Command

`exomonad new` is the project bootstrap command. It auto-creates `.exo/config.toml` (empty, all defaults) and `.gitignore` entries if missing. It also copies WASM plugins from `~/.exo/wasm/` and rules templates to `.claude/rules/`. Works in any project directory.

### Init Command

`exomonad init` creates a tmux session with:
- **Server window**: Runs `exomonad serve` (binds .exo/server.sock)
- **TL window**: Runs the configured root agent (Claude by default; `--tl codex` writes `.codex/config.toml`, installs the shared ExoMonad hook block in the Codex user config, and launches Codex with `--cd` in the project root)
- **Companion windows**: One per `[[companions]]` entry in config. Claude companions get isolated git worktrees at `.exo/companions/{name}/` with `.mcp.json`, hooks, and socket symlink. Worktrees persist across `--recreate`. Process companions (`agent_type = "process"`) run a plain command — no MCP, no worktree, no agent identity.

`exomonad init` requires `exomonad new` to have been run first to bootstrap the project configuration and WASM plugins. Use `exomonad init --reviewer-max-rounds N` for a validated, session-only reviewer cap override; it takes precedence over `.exo/review-policy.toml` without changing that file.

The TL window wraps the controller command so live output remains in the pane. A normal zero exit keeps the existing startup retention release, while a failed exit or durable controller-exit marker retains the pane and prints the failure marker plus the `controller-output.log` path.

**Server startup:** `init` waits up to 30s for the server to answer `/health` over
`.exo/server.sock`. `serve` reclaims its socket on every exit path, including a
failed one, so a server that gives up leaves no socket for `init` to mistake for a
server still starting. It deliberately leaves its pid record behind: a record that
existed only while the server was alive would disappear inside one poll interval of
a server that dies quickly, leaving `init` unable to tell that apart from one that
has not started yet. `init` stops as soon as the server is provably gone — a
recorded pid that has exited, or a socket nothing is listening on — and attaches the
Server window's own output to the failure. A server that is up but still loading its
WASM plugin is awaited, because the budget covers that. `serve` also binds the public
TCP listener at `0.0.0.0:<port>` (default 7433), so two projects on one host need
distinct ports; `port = 0` takes an ephemeral one.

When the compiled interruption acceptance has configured the harness-only
`EXOMONAD_E2E_INTERRUPT_*` variables, `init` carries them into the Server window
it starts so a replacement server still reaches the same boundary. The command
clears each name with `env -u` before assigning it, because the tmux server's
inherited environment may hold an earlier value and `std::env::var` reads the
last duplicate, and the assignment block has its own leading separator so it
cannot merge into the preceding model-route value. In production the variables
are unset and the command is unchanged.

Init also refreshes project-local WASM from `~/.exo/wasm/` if the global copy is newer (consuming projects only, not source projects with `.exo/roles/`).


Claude MCP is auto-registered during init. Other harnesses use their standard MCP registration.

Use `--recreate` to delete an existing session and create fresh (e.g., after binary updates).
The `record-plan-snapshot` stdin command persists plan bytes captured by a
controller. It holds the plan transition lock, rejects any existing transition
journal, and repairs a missing digest only for an identical snapshot.

#### Controller startup handoff

`init --continue` advances `.exo/tl-loop/root.controller-epoch`, persists the
gated target as `controller-target.pending.json` with its tmux server and pane
process identities, and writes a `controller-startup.json` fence before child
repair. The committed `controller-target.json` remains the previous lifetime
until startup release and commit. A fresh tmux server may recycle the same
numeric `@window`/`%pane` IDs; identity, not the numeric ID, decides whether the
old controller lifetime ended. Child repair and startup retries reuse a
successor created in the same controller epoch. A failed handoff stays
recoverable in the gated pane; the next init reconciles it, and an explicit
workspace shutdown may quiesce the exact pending target. Init always persists
the fence before the pending target, so a pending record whose fence is missing
or conflicts is treated as a partial handoff and cannot select a shutdown or
restart target. When the gated pane is found stopped, recovery commits the exact
target for that epoch, clears both the pending record and the startup fence, and
returns to the caller so a retry starts from a clean handoff instead of looping
on the same interrupted startup.

A completed workspace-shutdown handoff (`workspace-shutdown.json`, phase
`complete`) is scoped by the root `run_id` and its controller epoch, not by the
tmux session name. `--session <name>` may therefore adopt the same run's
completed handoff across sessions: the recorded and adopting session names are
kept in an adjacent archive evidence record while the archived handoff bytes
stay identical to the retained intent. The handoff is validated before the
controller is released on both startup paths (the interrupted-startup recovery
and the fresh `--continue` TL window): schema 1, phase `complete`, a safe
operation id, the same run, an archive whose existing bytes are identical, and
an epoch that names a controller lifetime which already ended. That epoch is
compared against the epoch being started, any pending target epoch, and a
committed target epoch whose exact pane is still live; it is deliberately not
required to equal the latest epoch marker, because a later failed
`init --continue` may have advanced the marker while the handoff still names
the earlier stopped lifetime. An incomplete, foreign, conflicting, or still-live
handoff therefore fails init before any ownership commit or release. After a
successful startup commit the handoff is renamed into
`.exo/controller-lifecycle/archive/<operation_id>.json`; if that post-release
archival still fails, the pending target, startup fence, and exact controller
pane are retained so the next `init --continue` re-adopts the same pane and
completes the archive instead of leaving an unowned controller.

`exomonad shutdown` stays server-only. `exomonad shutdown --workspace` is the
explicit control-plane operation: it fences dispatch, quiesces the root and
shared sub-TL controllers, preserves independent worker panes, writes terminal
interruption evidence, and releases server artifacts. Shutdown selects a valid
pending startup target (current epoch, tmux server, pane process, root run scope,
and startup fence) before the last committed target; a pending record from
another scope or an unavailable inspection fails closed instead of being
preferred. A bounded failure persists the intent as `blocked` with the precise
owner/absence/inspection or retirement blocker while preserving the pane and
worker evidence. Repeating shutdown is idempotent, and an unfinished shutdown
intent is retargeted to the exact pending startup when init advanced the epoch.

### Revert Command

`exomonad revert` removes workspace files created by `exomonad init` so the repository can be used without ExoMonad role enforcement or hook interception. It removes root MCP/hook configs, root agent runtime configs, companion worktrees/config files, and stale sockets when the server is not running. It leaves `.exo/config.toml`, `.exo/wasm/`, `.chainlink/`, `.exo/logs/`, and `.gitignore` intact. Use `--kill-session` to also kill the configured tmux session.

**Example `.exo/config.toml`:**
```toml
# All fields are optional — auto-detection handles the common case
default_role = "tl"
project_dir = "."
shell_command = "nix develop"  # environment wrapper for TL tab + server
wasm_dir = ".exo/wasm"        # project-local default
wasm_name = "devswarm"        # auto-detected from .exo/roles/ if exactly one exists
model = "sonnet"              # optional — --model flag for root TL agent
root_agent_type = "claude"    # legacy compatibility; the Python TL controller does not read it
spawn_agent_type = "codex"    # default worker/leaf/companion harness
tl_effort_level = "medium"    # CLI overrides config
worker_effort_level = "medium"
poll_interval = 60            # optional — GitHub poll cycle in seconds (default: 60)
[reviewer]
agent_type = "claude"
effort_level = "medium"
```

**Reviewer policy:** `--reviewer-max-rounds N` is valid for `exomonad init` when `N >= 1`. It overrides `.exo/review-policy.toml` for the new server session; omitting it preserves the policy file and built-in defaults.

**Role-specific effort:** `--tl-effort-level`, `--worker-effort-level`, and
`--reviewer-effort-level` override their matching config values. Resolution is CLI >
local config > global config > medium default. OpenCode receives effort as a
model-aware `--variant`; Shoal logs that effort is ignored.

**Accepted role-routing identity:** `config.rs::routing_configuration_fingerprint`
is the one canonical identity shared by `serve`, `init`, the controller handoff,
and the host launch check. It is `sha256:v2:` over only each role's *effective*
harness/model/effort in deterministic `tl`, `worker`, `reviewer` order. Which
source supplied a value (`project_config`, `local_config`, `environment`, `cli`,
`policy`, `default`, `harness_default`) is recorded separately by
`routing_configuration_provenance` and exported as audit evidence in the init
log, the serve startup log, and the `/health` surface — it is never hashed into
the identity. Repeating a project value as an explicit `--worker-model`/effort/
harness (or reviewer) flag is therefore the same accepted configuration, while a
genuinely different value changes the identity. `sha256:v1:` values are
historical receipts: they remain readable by the Python controller, are never
reinterpreted as v2, and can never satisfy a v2 acceptance. Before it keeps or
releases a controller, `init` reads the running server's identity from `/health`
and compares it with its own resolved identity; a mismatch or an absent identity
is a structured early hold (`role_configuration_not_accepted` /
`role_configuration_unavailable`) naming both identities and the restart remedy,
with no controller release and no worker or gate touched.

The preflight is proof-carrying, not advisory:
`init::accepted_role_configuration::AcceptedRoleConfiguration` has a private
field and is constructed only by
`ensure_server_accepts_role_configuration`. `launch_tl_recovery` takes
`&AcceptedRoleConfiguration`, and both release entry points (the fresh-session
path and `reconcile_existing_session`) obtain it from that one function, so
deleting or skipping a preflight is a compile error rather than a late launch
rejection. Unit tests cover the producer against a real UDS `/health` response
(equal, different, absent and transport-failure cases) and pin the two call
sites; `tests/e2e/init-recovery/check_role_config_preflight.py` rejects textual
bypasses. The compiled acceptance is
`E2E_ROLE_CONFIG_ONLY=1 tests/e2e/init-recovery/run.sh` (`just e2e-role-config`).
The stored-proof counterpart is `E2E_STORED_PROOF_ONLY=1` (`just
e2e-stored-proof`): the host acceptance checks are unchanged, and the compiled
run proves that a retained `sha256:v1:` terminal-owner authorization is
recomputed under its own semantics by the controller and superseded by one
fresh `sha256:v2:` launch authorization, with the real server accepting exactly
one generation-3 successor. `just e2e-stored-proof-literal` adds the literal
case: the actual retained v1 digest (`a8b9524b...`) is recomputed under its
retained historical policy and manifest (`4be5ddad...`), not a scratch
regeneration, and the compiled init/serve path still links exactly one v2
successor; it prints `LITERAL-1..5` and `LITERAL-ALL PASS`.
`just e2e-dispatch-collision` adds the production-shaped replay that the
literal mode used to omit: both historical dispatch rows, the rejected
prelaunch journal, the literal proof and the approved gate, followed by one
reauthorized generation-3 launch. It prints `DISPATCH-1..6` and
`DISPATCH-ALL PASS`; the host resolution is in
`rust/exomonad-core/src/handlers/dispatch_authorization.rs`.
`route.configuration_fingerprint` still comes from this identity;
`route_fingerprint` keeps its source-aware `sha256:v1:` route-receipt binding as
audit evidence.

**OpenCode model validation:** `init` validates exactly the roles it validated
before the probe existed: an OpenCode worker route and the reviewer (through the
harness-aware validator for its configured harness). TL routes are resolved and
logged but not model-validated, and a non-OpenCode worker route is not
model-validated; `validate_opencode_model_owner` remains a separate field-owner
check. For OpenCode, `init` runs exactly one subprocess — `opencode --version`
under `OPENCODE_PROBE_TIMEOUT` (10s) — and then validates the model and effort
against the committed static table for that version
(`rust/exomonad/data/opencode/<version>.json`, embedded through
`EMBEDDED_OPENCODE_TABLES`). No live catalog command and no network request is
made; the catalog is never discovered at runtime. A missing binary,
a `--version` timeout, a non-zero exit, non-UTF8 output, and output without a
recognizable version are distinct fail-closed errors that name the command and
bound. An unknown model fails naming the model, the version, and the table file;
a requested effort that the table does not record (`null`), that the model does
not support (`[]`), or that is not in the model's list is a hard error naming
the supported efforts — there is no pass-through and no warning. An unknown
harness version fails naming the detected version, the known versions, and the
table entry a new version needs; there is no nearest-version fallback. There is
no bypass flag or silent skip, and no ETXTBSY retry loop in production; test
fixtures create their executable atomically instead.

**Committed OpenCode versions:** `v2.0.22` and `v2.3.2` (the current system
CLI). Each has a committed `rust/exomonad/data/opencode/<version>.json` plus a
plain `opencode models` capture in the matching `.models.txt`. `v2.3.2`'s
`opencode run` accepts every flag `init` passes (`--standalone`, `--auto`,
`--model provider/model#variant`, `--agent`, `--session`, `--fork`). The
task-less (interactive) companion form is version-specific and selected by
`opencode_companion_launch_form`: `v2.0.22` keeps
`opencode [--model M] --agent exomonad-<role>` because its top-level command
accepts both flags, while `v2.3.2` uses
`opencode mini [--model M] --agent exomonad-<role>` because its top-level
command rejects them and `mini` is the supported interactive interface. `init`
detects the version once, before creating a session, and an unknown version is a
named error rather than a guessed form. The TypeScript plugin loader requires
a default `{ id, setup }` definition instead of the old `server` export; the
generated plugin provides it. The `hook` subcommand normalizes the v2 payload
(`args`, `output`, `call_id`, provider tool ids including `shell`) before
posting to the server, so role hooks see the canonical OpenCode tool ids. See
`docs/decisions/opencode-hooks.md` for the plugin and hook status.

**Configured harness-policy check:** before any controller side effect, `init`
validates the worker and reviewer routes it resolved from `config.toml` (and any
CLI/environment override) against the same roles' `allow` lists in
`.exo/harness_policy.toml`. The harness must appear in the list. When the route
names a model and every allow entry for that harness is model-qualified, the
model must equal one of those entries; a bare `harness` entry allows any model.
A disagreement is a hard error naming the role, the resolved `harness/model`,
the config field and file, the allow list, and both files the operator can
change. There is no bypass flag, warning path, or automatic edit. A missing
policy is not a new error here (the controller's existing startup error
remains), and a malformed policy is parsed by the one shared Rust definition in
`exomonad-core::services::harness_policy`, pinned to the Python controller by
`tests/fixtures/harness_policy_agreement.{toml,json}`. TL is deliberately not
checked: `root_agent_type` is legacy compatibility only and the Python
controller does not read it; `roles.tl.allow` is validated by `tl_loop`
startup. `init` still does not catalog-validate a non-OpenCode worker route or
any TL route.

**Adding an OpenCode harness version:** commit
`rust/exomonad/data/opencode/<version>.json` (schema_version 1, harness
`opencode`, `version` equal to the file key, and `models` mapping each
`provider/model` to `{"efforts": [...] | null}`), register it in
`EMBEDDED_OPENCODE_TABLES`, and add the test that pins the new table. Model
validation has no code branch per version, and `exomonad models` lists the new
table automatically under `# opencode <version>`. If the version's task-less
interactive form differs from both supported versions, also add its
`opencode_companion_launch_form` arm and test; an unlisted version fails
`init` for an interactive OpenCode companion rather than guessing.

**Bootstrap:** `exomonad new` auto-creates `.exo/config.toml` (empty, all defaults) and `.gitignore` entries if missing.

**Multi-remote projects:** `exomonad init --set-git-remote <name>` pins which
git remote `file_pr`, `merge_pr`, the Forgejo watcher, and pushes use — needed
when a repo has more than one remote (e.g. a GitHub `origin` kept for
mirroring alongside a separate Forgejo remote), since auto-detect otherwise
prefers whichever remote is literally named `origin` regardless of which
backend it points at. The flag validates the remote exists, then persists it
via `git config --local exomonad.remote <name>` (shared across worktrees via
the main repo's `.git/config` — no config.toml field). See
`services::repo::get_repo_info` in `exomonad-core`.

Use `exomonad init --reset-inbox` to clear all persisted inbox messages and
agent inbox metadata before starting the session, while preserving the inbox
database schema. If init attaches to an already-running session without
`--recreate`, it still clears the inbox and emits a warning; use `--recreate`
when the reset should accompany a session restart.

**WASM resolution:** project `.exo/wasm/` → build from `.exo/roles/` → copy from `~/.exo/wasm/` (global install via `just install-all`).

**Config hierarchy:**
- `config.toml` uses `default_role` (project-wide default)
- `config.local.toml` uses `role` (worktree-specific override)

To update WASM, run `just wasm-all` or `exomonad recompile --role devswarm`.

## MCP Server

The server exposes a REST API over UDS. `exomonad mcp-stdio` is a translation layer that speaks MCP JSON-RPC on stdio and converts to REST calls over UDS.

### Configuration

Register manually in `.mcp.json`:
```json
{
  "mcpServers": {
    "exomonad": {
      "command": "exomonad",
      "args": ["mcp-stdio", "--role", "root", "--agent-id", "root"]
    }
  }
}
```

### Available Tools

| Tool | Role | Description |
|------|------|-------------|
| `spawn_leaf` | root, tl | Spawn the configured leaf agent (worktree, inline, or standalone isolation) |
| `file_pr` | tl, dev | Create/update PR for current branch |
| `merge_pr` | root, tl | Merge child PR (gh merge + git fetch) |
| `notify_parent` | tl, dev, worker | Send message to parent agent |
| `send_message` | all | Send message to another agent (routes via Teams inbox / UDS / tmux fallback) |
| `task_list` | dev, worker | List tasks from shared task list |
| `task_get` | dev, worker | Get task by ID |
| `task_update` | dev, worker | Update task status/owner/activeForm |

### Debugging

You can probe the UDS server using `curl`:
```bash
# List tools for an agent
curl --unix-socket .exo/server.sock http://localhost/agents/root/root/tools

# Call a tool
curl --unix-socket .exo/server.sock -X POST http://localhost/agents/tl/root/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"name":"notify_parent","arguments":{"status":"success","message":"test"}}'

# Check if server is alive
curl --unix-socket .exo/server.sock http://localhost/health
```


## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `RUST_LOG` | No | Tracing log level |
| `EXOMONAD_SWARM_RUN_ID` | No | Swarm run ID (set by server, propagated to children). OTel resource attribute. |
| `EXOMONAD_PARENT_AGENT` | No | Parent agent's birth branch (set at spawn, propagated to children). OTel resource attribute. |

## Effect Boundary (WASM)

All effects flow through a single `yield_effect` host function using protobuf binary encoding. Haskell calls `runEffect @EffectType request`, which encodes an `EffectEnvelope` and dispatches to the appropriate handler in the `EffectRegistry` by namespace prefix.

### Registered Handlers

| Namespace | Handler | Implementation |
|-----------|---------|----------------|
| `git.*` | GitHandler | git subprocess |
| `github.*` | GitHubHandler | HTTP API |
| `agent.*` | AgentHandler | GitHub API + git worktree + tmux |
| `fs.*` | FsHandler | tokio::fs |
| `log.*` | LogHandler | tracing |
| `file_pr.*` | FilePRHandler | gh CLI |
| `copilot.*` | CopilotHandler | GitHub API polling |
| `events.*` | EventHandler | Event queue, notify_parent, send_message |
| `merge_pr.*` | MergePRHandler | gh pr merge + git fetch |
| `kv.*` | KvHandler | Key-value store (.exo/kv/) |
| `session.*` | SessionHandler | Claude session registry, team registration/deregistration |
| `tasks.*` | TasksHandler | Task list operations (list, get, update) with team auto-resolution |
| `coordination.*` | CoordinationHandler | In-memory mutex (FIFO wait queue, TTL expiry) |

## Building

WASM must be built before running hooks or serve mode:

```bash
just install-all-dev

# Or manually:
just wasm-all                    # Build devswarm WASM plugin
cargo build -p exomonad          # Build Rust binary
```

## Testing

```bash
# Unit tests
cargo test -p exomonad

# Actual-binary init Git-lock readiness (Linux, tmux, complete /proc visibility
# for the orphan-recovery success branch)
cargo test -p exomonad --test init_git_lock_readiness -- --nocapture

# MCP integration tests (wrapper script manages server lifecycle)
just test-mcp

# E2E tests (interactive — launches tmux, you observe companions work)
just e2e-messaging         # Teams inbox delivery pipeline
just e2e-oc-rewrite        # BeforeModel/AfterModel PII rewriting

# Quick hook smoke test (requires running server: exomonad serve)
echo '{"session_id":"test","hook_event_name":"PreToolUse","tool_name":"Write","transcript_path":"/tmp/t.jsonl","cwd":"/","permission_mode":"default"}' | \
  ./target/debug/exomonad hook pre-tool-use
```

E2E tests live in `tests/e2e/{name}/` and follow a standard pattern: `run.sh` creates a temp repo, configures companions (test subject + Claude testrunner), and runs `exomonad init` as the last line. See root `CLAUDE.md` § "E2E Test Pattern" for the full convention.

The init Git-lock readiness integration test obtains a durable lock-owner proof
through the production process handler, then invokes the built `exomonad init`
binary in a disposable repository and tmux server. Its successful quarantine
branch requires Linux process-descriptor visibility; a restricted `/proc` host
must instead observe a nonzero, state-preserving readiness refusal.

## Server Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | GET | Health check |
| `/hook` | POST | Hook events (pre-tool-use, session-start, etc.) |
| `/agents/{role}/{name}/tools` | GET | List tools for an agent |
| `/agents/{role}/{name}/tools/call` | POST | Call a tool (body: `{name, arguments}`) |
| `/events` | POST | Event notifications |
| `/reload` | POST | Clear WASM plugin cache (next call loads fresh from disk) |
| `/shutdown` | POST | Graceful server shutdown |

## Data Flow

### MCP Tool Call Flow
```
Claude Code → stdio (JSON-RPC) → exomonad mcp-stdio (translates JSON-RPC → REST)
         ↓
    UDS GET .exo/server.sock /agents/{role}/{name}/tools        (tools/list)
    UDS POST .exo/server.sock /agents/{role}/{name}/tools/call  (tools/call)
         ↓
    Server: REST handler → WASM handle_list_tools / handle_mcp_call
         ↓
    Haskell dispatches to tool handler → yields effects
         ↓
    Server executes effects via host functions (in-process)
         ↓
    Server returns JSON response
         ↓
    mcp-stdio translates REST response → JSON-RPC response → stdout
         ↓
    Claude Code receives response
```

### Hook Flow
```
Claude Code hook JSON (stdin)
         ↓
    exomonad hook pre-tool-use (thin UDS client)
         ↓
    UDS POST .exo/server.sock /hook?event=pre-tool-use&runtime=claude
         ↓
    Server: parse HookInput from body
         ↓
    Server: call WASM handle_pre_tool_use (in-process)
         ↓
    Haskell yields effects (GitGetBranch, LogInfo, etc.)
         ↓
    Server executes effects via host functions (in-process)
         ↓
    Server returns HookEnvelope { stdout, exit_code }
         ↓
    CLI prints stdout, exits with exit_code
         ↓
    Claude Code receives response
```

## Related Documentation

- **[exomonad-core](../exomonad-core/)** - Framework, handlers, services, protocol types, UI protocol
- **[wasm-guest](../../haskell/wasm-guest/)** - Haskell WASM plugin source
