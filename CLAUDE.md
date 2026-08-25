# ExoMonad

Type-safe LLM agent orchestration. Haskell WASM is a typed configuration DSL — tool schemas, handlers, decision trees, hook logic, event routing — with the full power of a type system and effect system behind it. Rust executes the I/O effects that the DSL yields. tmux provides isolation and multiplexing.

---

## Model

ExoMonad is a **hylomorphism over context windows**. The unfold is planning + scaffolding + spawning. The fold is merging + integrating + PR-upward. The recursion scheme gives you the entire system.

### The Agent Triad

Each node in the tree is three things born, living, and dying together:

- **Worktree** — filesystem state (code isolation via git worktree)
- **Context window** — attention state (what the agent knows and can reason about)
- **Actor** — message-processing entity (receives notifications, yields effects)

These are 1:1:1. You cannot have a worktree without a context window to operate on it, or a context window without an actor to drive it. When the actor shuts down, the worktree is cleaned up and the context window is gone. The triad IS the agent.

### The Hylomorphism

**Unfold (coalgebra = the scaffold commit).** A TL plans one level down, commits the shared foundation (types, interfaces, stubs, CLAUDE.md), and spawns children. The scaffold commit is the coalgebra — it defines the shape of the decomposition. This is the most important thing a TL does. Children fork from this commit and cannot see each other.

**Fold (algebra = merge + integrate).** As children complete, the TL merges their PRs, wires outputs together, and writes an integration commit. Each merge is paramorphic — the TL accumulates understanding from what children produced. This accumulated context informs the next wave's specs. After all waves are folded, the TL files a PR upward, and its parent folds it in turn.

The operational realization is the programmatic controller described in [Tech Lead Praxis](#tech-lead-praxis).

### Depth over Breadth

Sub-TLs are **compression boundaries**. A root TL with 3 sub-TLs, each managing 4 leaves, sees O(3) — not O(12). The sub-TL absorbs implementation detail into its context window, and surfaces only the integrated result upward.

This is why the tree prevents context window drift: each node's cognitive load is proportional to its fan-out, not the total number of leaves beneath it. A 4-level-deep tree with branching factor 3 has 81 leaves, but no single context window ever reasons about more than 3 children.

### Waves as Rhythm

Within a single TL's scope, work proceeds in waves. Wave N produces merged code. Wave N+1 builds on it. The wave boundary is where understanding accumulates — the TL reads the merged diffs, learns what the children actually built, and uses that knowledge to write sharper specs for the next wave.

Long-running waves may persist an explicit objective, deadline, and completion
predicate in run state. When the event queue is idle, the configured heartbeat
interval re-observes worker liveness through poll_workers and PR state through
watcher_pr_state. Reconciliation is idempotent and stays inside the shared
state writer: it never invents event-log sequence numbers or budget charges.
A dead or stalled slice is escalated through the M5.3 parking path with an
auditable cause; tmux scraping is not a liveness source.

### Branch Naming as Coordinate System

`{parent}.{name}` (dot separator) encodes tree address, where `name = {slug}-{type}` (e.g., `auth-claude`, `oauth-provider-codex`). `dev.auth-claude.oauth-provider-codex` tells you: root is `dev`, first-level TL is `auth` (Claude), leaf is `oauth-provider` (Codex). The last dot-segment IS the `AgentName` — one namespace, zero translation. PRs target the parent branch, not main — merged via recursive fold up the tree. The git DAG IS the computation trace.

---

## Rules

### Style

ALWAYS update CLAUDE.md files when you make changes. Adding new documentation is critical, as is removing stale documentation.

Comments should always focus on what is or will be. Never leave comments about why you deleted something, its in the git history which is enough.

The repository should be kept clean of dead code, placeholders, and half-done heuristics.

Always prefer failure to an undocumented heuristic or fallback.

### Single Code Path

Never maintain two code paths that do the same thing. Redundant paths cause bug risk — fixes applied to one path get missed on the other. If there's a "debug mode" or "legacy mode" that duplicates a primary path, cut it.

### Chainlink Timers

Chainlink timers are TL-owned and explicit per issue. Start timers with the assigned issue id, and stop timers with that same issue id. Do not rely on a global active timer: Chainlink supports concurrent timers across issues while enforcing one active timer per issue.

### All Tools and Hooks in Haskell WASM

**Never add direct Rust MCP tools.** All MCP tools and hooks are defined in Haskell WASM — tool schemas, argument parsing, dispatch logic, everything. Rust is the I/O runtime: it executes effects that the Haskell DSL yields. If a new tool needs new I/O capabilities, add a new effect handler in Rust and a corresponding effect type in Haskell. The tool itself lives in `haskell/wasm-guest/src/ExoMonad/Guest/Tools/`.

This is the entire architectural premise. Haskell WASM is the single source of truth for tool definitions. Rust never defines tool schemas, never parses tool arguments, never contains tool logic.

### Crosscutting Rules

When you learn something that applies to a crosscutting context (a programming language, a tool like git worktrees, a pattern that spans directories), **create or update a `.claude/rules/*.md` file** rather than documenting it in a directory-specific CLAUDE.md.

Examples: language idioms (`.claude/rules/haskell.md`, `.claude/rules/rust.md`), tool usage patterns (git, cabal, cargo, tmux), architectural patterns that span the codebase.

Rules files use YAML frontmatter to scope when they load:
```yaml
---
paths:
  - "**/*.hs"
---
```

### Logging

Silent failures are unacceptable. When code shells out to subprocesses, calls external services, or crosses process/container boundaries, **log aggressively**:

1. **Before the call**: Log what you're about to do (command, key parameters)
2. **After the call**: Log exit code, status, response size
3. **On error**: Log stderr, error messages, enough context to debug without reproducing
4. **On success**: Log the result summary (e.g., `button=submit`, `items=5`)

**Haskell pattern:**
```haskell
logInfo logger $ "[Component] Starting operation: " <> summary
(exitCode, stdout, stderr) <- readProcessWithExitCode cmd args ""
logInfo logger $ "[Component] Exit code: " <> T.pack (show exitCode)
case exitCode of
  ExitFailure code -> logError logger $ "[Component] FAILED: " <> T.pack stderr
  ExitSuccess -> logInfo logger $ "[Component] Success: " <> resultSummary
```

**Rust pattern:**
```rust
tracing::info!("Executing: {} {}", cmd, args.join(" "));
let status = Command::new(cmd).args(&args).status()?;
tracing::info!("{} returned: {:?}", cmd, status);
if !status.success() {
    tracing::error!("{} failed with status: {}", cmd, status);
}
```

---

## Getting Started

### Session Entry Point

**`exomonad new` is the one-time project bootstrap.** It creates `.exo/config.toml`, `.gitignore`, and copies WASM plugins and rules templates.

**`exomonad init` is the idempotent entry point for development sessions.** It creates a tmux session with:
- **Server window**: Runs `exomonad serve` (the MCP server, binds to `.exo/server.sock`)
- **TL window**: Runs the programmatic Python controller (`python3 ~/.exo/tl_loop.pyz`), which owns planning, dispatch, event consumption, merge decisions, and durable run state.

The server must be running before the controller or any worker can use MCP effects. Without it, effect calls fail explicitly. Init also writes `.mcp.json` with the `tl/root` identity, `.exo/agents/root/identity.json`, `.exo/tl-loop/plan.json` when supplied, and `.claude/settings.local.json` for worker hooks.

Init uses a bounded five-second startup liveness gate for the TL pane. This
confirms that the controller process appears and survives startup; it is not a
semantic ready signal or heartbeat protocol. Later hangs or failures remain
observable through the Watcher, `status`, and the durable controller-exit
record.

```bash
cd exomonad/                  # Run from the project root
exomonad new                  # One-time setup: creates config, WASM, rules
exomonad init                 # Current default: --recreate (mode is recorded)
# Explicit lifecycle choices (mutually exclusive):
exomonad init --start         # Fresh-run intent
exomonad init --continue      # Resume intent
exomonad init --recreate      # Tear down and rebuild intent
# The TL window now runs the controller. Give it a JSON WorkPlan:
python3 ~/.exo/tl_loop.pyz status --project-root .
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root --name <gate> --approve
```

The controller waits for `.exo/tl-loop/plan.json` when no structured
`initial_prompt` is configured. A plan document contains a `plan` object with
`workers`, `leaves`, and/or `sub_tls`; legacy natural-language TL prompts are
rejected because there is no interactive TL fallback.

The three init modes are explicit and recorded in `.exo/tl-loop/session-mode.json`
and the run checkpoint. In this surface-only phase, omitting a mode retains the
legacy `--recreate` default; the migration task will make `--continue` the safe
default. Supplying more than one mode is rejected. `tl_loop status` reports the
recorded mode alongside the durable state.

When continuing, ExoMonad compares .exo/tl-loop/plan.json byte-for-byte with
the immutable .exo/tl-loop/plan.snapshot recorded at session creation. Plan
drift is rejected before orchestration resumes. Existing agent directories are
classified from their invocation record and verified publication ownership;
the decision is recorded in each directory's continuation.json. A preserved
invocation keeps its original ID verbatim, while an invocation classified for
recreation carries an explicit reason for the later spawn path.

Optional local Forgejo CI stack:
```bash
cd forgejo
docker compose up -d
```

**Project setup:**
```bash
exomonad new                     # Bootstrap new project (.exo/config.toml, WASM, rules)
```

**Server management:**
```bash
exomonad reload                  # Clear WASM plugin cache (next call loads fresh from disk)
exomonad shutdown                # Gracefully shut down the running server
```

**Log and evidence management:**
```bash
exomonad logs import --source <path> [--source <path>] [--format auto|jsonl|json|sqlite|text] [--dry-run] [--rebuild]
                                 # Normalize log sources into .exo/analysis/atlas.db. Explicit,
                                 # idempotent, and read-only with respect to the source files.
exomonad logs drop-segments --older-than-seconds 2592000 [--dry-run]
                                 # Whole-segment retention on closed ledger segments
exomonad logs export --mode aggregate --output .exo/analysis/export
                                 # L4 compile — the only shareable output
exomonad logs measure --output .exo/analysis/measurement [--require-ready]
                                 # Local detectors, incidents, adjudication, measurement gate
```
See [docs/guides/migrating-to-the-tl-loop.md](docs/guides/migrating-to-the-tl-loop.md)
for using `logs import` to bring an older project's history into the same
Failure Atlas view.

### MCP Registration

`exomonad init` automatically registers the `tl/root` MCP identity in `.mcp.json`; the Python controller uses the same UDS runtime boundary. Worker and companion harnesses receive their own role/name registrations. For manual Codex access, register via stdio:
```bash
codex mcp add exomonad --command "exomonad mcp-stdio"
```

### Zero-Config for Consuming Repos

After running `just install-all` (which installs WASM to `~/.exo/wasm/`), any project works out of the box:

```bash
cd ~/new-project && git init
exomonad new                  # Creates .exo/config.toml, copies WASM from ~/.exo/wasm/
exomonad init                 # Starts server, registers tl/root, creates the controller TL window
```

For custom roles, copy `.exo/roles/` and `.exo/lib/` from exomonad and `exomonad new` will build WASM from source instead.

### Building

```bash
# One-command install (recommended - uses debug build for fast iteration)
just install-all-dev

# Or install release build (optimized, slower compile)
just install-all

# Compatibility forms
just install all
just install all-dev

# WASM builds (two equivalent options)
just wasm-all                     # Build all WASM via nix
exomonad recompile --role devswarm # Build specific role's WASM via nix
# Both are standalone CLI commands — neither requires the server to be running.
# Output: .exo/wasm/wasm-guest-devswarm.wasm

# Rust sidecar only
cargo build -p exomonad

# Hot reload: server checks WASM mtime per tool call, so after recompile
# the next MCP call picks up the new WASM automatically.
# For immediate reload: `exomonad reload` clears the plugin cache explicitly.

# Verify that the installed TL archive is built from this checkout.
just tl-loop-archive-test
python3 scripts/check_tl_loop_archive.py "$HOME/.exo/tl_loop.pyz" --source "$PWD/tl_loop"
# The archive stamp contains the Git commit and deterministic tl_loop tree hash.
# `tl_loop status` reports the same fingerprint, and controller startup warns
# when an installed archive is stale; install again with `just install-all-dev`.
```

**What `just install-all-dev` does:**
1. Builds devswarm WASM plugin via nix
2. Builds exomonad Rust binary (debug mode)
3. Copies binary to `~/.cargo/bin/exomonad`

**WASM build pipeline:**
1. Role configs in `.exo/roles/devswarm/` define tool composition per role (`RootRole.hs`, `TLRole.hs`, `DevRole.hs`, `WorkerRole.hs`)
2. `AllRoles.hs` registers all roles; `Main.hs` provides FFI exports
3. `cabal.project.wasm` lists the devswarm package alongside `wasm-guest` SDK
4. `just wasm-all` builds via `nix develop .#wasm -c wasm32-wasi-cabal build ...`
5. Compiled WASM copied to `.exo/wasm/wasm-guest-devswarm.wasm`
6. `exomonad serve` loads devswarm WASM from `.exo/wasm/` at runtime (hot reload via mtime check)

### Configuration

**Bootstrap:** `exomonad new` auto-creates `.exo/config.toml` (empty, all defaults), `.gitignore` entries, and `.forgejo/workflows/ci.yml` if missing. Works in any project directory. All fields are optional — auto-detection handles the common case. The CI scaffold uses GitHub Actions syntax with language-specific defaults, and leaves workspace-specific build/test customization where needed. **Claude rules:** `exomonad new` copies `.exo/rules/exomonad.md` → `.claude/rules/exomonad.md` (if the template exists and the destination doesn't). Template resolution: project-local `.exo/rules/` → global `~/.exo/rules/`. This gives fresh Claude instances automatic knowledge of exomonad MCP tools.

```toml
# All fields below are optional — shown with their auto-detected defaults
default_role = "tl"          # auto-detected from .exo/roles/ if exactly one role exists
project_dir = "."
shell_command = "nix develop" # environment wrapper for TL tab + server
wasm_dir = ".exo/wasm"       # project-local (default), override for shared installs
wasm_name = "devswarm"       # auto-detected from .exo/roles/ if exactly one role exists
model = "sonnet"             # legacy root-model compatibility; ignored by the programmatic TL
root_agent_type = "claude"   # legacy compatibility; init always starts the Python TL controller
spawn_agent_type = "codex"   # default harness for workers, leaves, and companions
# Worker/reviewer CLI flags override their values. Root TL effort is ignored.
tl_effort_level = "medium"   # legacy compatibility; not used by the controller
worker_effort_level = "medium"
poll_interval = 60           # optional — GitHub poll cycle in seconds (default: 60)
# TL loop operational timeouts (seconds). Lifecycle progress is driven by authoritative events.
  tl_transport_timeout_seconds = 10.0           # UDS RPC timeout to the local server (default: 10)
  tl_active_tail_timeout_seconds = 30.0         # ledger tailer active-tail timeout (default: 30)
  tl_task_timeout_seconds = 3600.0              # project task ceiling; 0 disables enforcement
  tl_preflight_runtime_paths = [".my-tool/runtime/"] # extra relative runtime paths ignored by spawn preflight
forgejo_url = "http://localhost:3000"           # optional — Forgejo base URL
forgejo_token = "forgejo_pat"                      # optional — Forgejo API token
forgejo_webhook_secret = "shared-secret"           # optional — webhook signature secret

# Extra MCP servers (HTTP or stdio). Included in .mcp.json for all agents.
[extra_mcp_servers.metacog]
type = "http"
url = "http://localhost:8080"

# Opencode agent configuration.
[opencode]
use_embedded_key = true  # true = use embedded key in opencode binary, false = use OPENROUTER_API_KEY
# tl_model/worker_model accept OpenCode model IDs; effort is sent as --variant.

[reviewer]
agent_type = "claude"     # claude | opencode | codex | shoal
effort_level = "medium"  # reviewer-only override

[extra_mcp_servers.notebooklm]
type = "stdio"
command = "notebooklm-mcp"
args = []

# Companion agents spawned alongside the root TL during init.
[[companions]]
name = "sleeptime"
agent_type = "claude"          # claude | opencode | codex | shoal | process
role = "sleeptime"             # WASM role for MCP tools (default: "worker")
command = "claude --dangerously-skip-permissions"
task = "You are sleeptime"     # optional — omit for interactive session
model = "haiku"                # optional — passed as --model flag to companion
```

Gemini is retired; old configurations fail closed with: `agent_type 'gemini' is retired; use 'codex' (model gpt-luna). See CLAUDE.md Configuration.`

**Role-specific harness and effort:** `--tl`, `--worker`, and `--reviewer` select the
root, worker/companion, and reviewer harnesses independently. Effort precedence is
CLI flag > local config > global config > medium default. Worker effort is inherited
by forked TLs, leaves, ephemeral workers, and companions. OpenCode receives the
resolved level as its model `--variant`; Codex receives the resolved level as
`model_reasoning_effort` in its rendered config, while Shoal alone logs that effort is
ignored because it has no stable effort interface.

**TL-loop harness policy:** `.exo/harness_policy.toml` is the required human-authored
allowlist and budget boundary for the programmatic controller. It must contain exactly
`roles.tl`, `roles.worker`, and `roles.reviewer`. Each role declares `allow` (ordered
`harness/model` entries), `cost_rank` (one positive rank for every allowed entry),
`token_budget` (a positive role ceiling), `per_harness_budget` (optional positive
ceilings whose keys must be allowed), and `escalate_after_attempts` (a positive
NO-GO threshold). A missing or invalid file is a startup error; the selector never
creates a permissive default or widens these boundaries.

Task duration uses one explicit precedence: a slice's `task_timeout_seconds` in
`plan.json` overrides the role's value in `.exo/harness_policy.toml`, which
overrides project `tl_task_timeout_seconds`, which defaults to 3600 seconds.
A value of `0` or explicit `null` means no ceiling. Elapsed time never infers
that a worker is dead or stalled; it may enforce only this declared, typed,
journaled task ceiling.

**Multiple git remotes:** By default, PR/CI operations (`file_pr`, `merge_pr`, the
Forgejo watcher, and pushes) auto-detect the git remote to use, preferring one
named `origin`. If a project has more than one remote — e.g. a GitHub `origin`
kept for mirroring alongside a separate remote pointing at a local Forgejo
instance — that auto-detect can pick the wrong one, sending one backend's
owner/repo to the other's API. Run `exomonad init --set-git-remote <name>` to
pin which remote to use; it validates the remote exists, then persists the
choice via `git config --local exomonad.remote <name>`. Git worktrees share the
main repo's `.git/config`, so the setting applies to every spawned agent
automatically. Omit the flag to keep today's auto-detect behavior.

**Config hierarchy:**
- `config.toml` uses `default_role` (project-wide default)
- `config.local.toml` uses `role` (worktree-specific override)
- Resolution: `local.role > global.default_role`
  - WASM: `wasm_dir` in config > `.exo/wasm/` (project-local)

**TL spawn preflight and Chainlink state:** ExoMonad-owned runtime paths are
excluded from the source-cleanliness check: `.chainlink/`, `.exo/`, and the
generated harness state paths. The optional `tl_preflight_runtime_paths` list
adds project-specific relative runtime directories; `config.local.toml`
overrides the project list for a role worktree. The effective rules are passed
to the controller and printed in any preflight failure. Keep
`.chainlink/issues.db` ignored and track a JSON mirror (for example
`.chainlink/issues.json`) when reviewable issue state is needed. Source edits
remain blocking; do not use `EXOMONAD_TL_PREFLIGHT_ACK=1` as a substitute for
the ignore rule.

**Hook configuration** is auto-generated in two places:
- **`exomonad init`**: Writes `.claude/settings.local.json` with hooks for workers and companions; the root TL controller is not an interactive harness session
- **`spawn_leaf`**: Writes `.claude/settings.local.json` into each spawned Claude worktree

The `SessionStart` hook is critical for child processes that request context inheritance — it registers the Claude session UUID in `ClaudeSessionRegistry` for the host spawn handler. Without it, such child processes start with no inherited context.

Codex agents use per-agent `.codex/config.toml` files for MCP and hook settings.

**Claude Code settings help:** We have a Claude Code configuration specialist (preloaded with official documentation) available as an oracle for hook syntax, settings structure, MCP setup, and debugging.

### Review Policy

The reviewer convergence loop is configured via `.exo/review-policy.toml`. To override only the running session without rewriting that file, use `exomonad init --reviewer-max-rounds 5` (and `--recreate` when replacing an existing session). The precedence is init override, then policy file, then built-in defaults.

| Setting | Default | Description |
|---------|---------|-------------|
| `min_review_rounds` | 1 | Minimum review rounds before merge is permitted |
| `reviewer_max_rounds` | 5 | Max completed verdicts before `tl_loop` parks and opens a human gate |
| `reviewer_max_wait_seconds` | 1200 | Max wait for reviewer response (20 min) |
| `reviewer_max_rate_limit_retries` | 2 | Max rate-limit retries for reviewer agents |
| `review_freshness_window_secs` | 1200 | Window for a review to be considered "fresh" |
| `external_review_threshold` | 300 | Lines changed to trigger mandatory second review |
| `external_review_paths` | `["proto/**", "rust/../handlers/**"]` | Path globs always requiring second review |
| `require_second_reviewer_complexity` | false | Require second reviewer for complex PRs |
| `complexity_line_threshold` | 500 | Line threshold for complexity-based second review |

**Reviewer identity discipline:** Each reviewer agent operates under a distinct git identity (`user.name=exomonad-reviewer-{name}`). The reviewer never commits to a branch it didn't author — the Authoring-Agent line in the PR body establishes traceability. An agent never reviews under the identity that authored the PR.

**Stuck state:** `tl_loop` counts completed reviewer verdicts in durable `SliceState.review_rounds`, across head resets. When the count reaches `reviewer_max_rounds` without convergence, the controller parks the slice with `review_rounds_exhausted`, opens a named human gate, and emits bounded round/ceiling telemetry. The watcher remains an observation source and never owns this control decision.

### Notification Vocabulary

- `[REVIEW ACTION REQUIRED]` — reviewer comments and changes-requested events are delivered directly to the parent TL by `tlPrReviewHandler`; the live PR owner also receives the review event. These outcomes do not emit `[REPAIR HANDOFF]`.
- `[REPAIR HANDOFF]` — retained for `approved` and `merge_ready` review outcomes. Existing `ci_blocked` and `stuck` handoffs remain authoritative and unchanged.

### Companion Agents

Companion agents are persistent agents spawned alongside the root TL during `exomonad init`. Claude companions get their own git worktree at `.exo/companions/{name}/` on branch `companion/{name}`, providing isolated `.mcp.json` discovery via CWD — the same mechanism used by child spawning.

Each Claude companion worktree contains:
- `.mcp.json` — MCP config with the companion's role/name identity
- `.claude/settings.local.json` — hooks (SessionStart, PreToolUse, etc.)
- `.exo/server.sock` — symlink to project root's server socket
- `.git` — worktree git file pointing to the main repo

Worktrees persist across `--recreate` (only the tmux session is torn down). Codex/Shoal companions use their existing env-var/flag-based config approach.

**Process companions** (`agent_type = "process"`) are plain long-running processes — no MCP config, no agent identity, no worktree, no hooks. Just a command in a tmux window. Use for mock servers, log tailers, or any background process that should live alongside the session.

---

## Capabilities

What you can do with exomonad right now, end-to-end.

### Orchestration

Spawn heterogeneous agent teams as a recursive tree:

- **`spawn_leaf`** — Spawn a leaf agent in own worktree+branch. Files PR when done. Agent type set by server config or explicit `agent_type`. Structured spec fields (steps, verify, boundary, context, read_first). Continuation context is prefixed automatically when available, while the caller's task remains verbatim after a blank-line separator.
- **`resume_pr`** — Resume the existing issue-owned PR worktree and invocation after review or CI feedback. Continuation context is prefixed automatically and review feedback is scoped to the exact current PR head SHA.
- **`resume_blocked_leaf`** — Resume one externally blocked, no-PR leaf after explicit human gate approval. The host verifies the parked event, exact dormant invocation, branch, and dirty-worktree fingerprint, then reuses the same owner/worktree/harness.
- **`spawn_worker`** — Spawn an ephemeral worker in a tmux pane. No branch, no PR. Just name + task.
- **`spawn_codex`** — Spawn a Codex leaf agent in its own worktree+branch. Files PR when done.

**Agent Types:** `Claude` (🤖), `OpenCode` (💻), `Codex` (🤖), `Shoal` (🌊). Codex agents use per-agent `.codex/config.toml` for MCP/instructions and a shared ExoMonad-managed hook block in the Codex user config for shell-native hooks. Shoal is for custom binary agents that connect via rmcp MCP client and receive notifications via HTTP-over-Unix-domain-socket at `.exo/agents/{name}/notify.sock`.

**Multi-WASM:** The server loads multiple WASM modules from `.exo/wasm/`. Convention: if `wasm-guest-{role}.wasm` exists, it's used for that role; otherwise falls back to `wasm-guest-{wasm_name}.wasm` (default). Drop a WASM file, it's available.

**Standalone repo mode:** Available via the lower-level `spawn_leaf_subtree` core function with `standalone_repo=true`. Creates a fresh `git init` repo instead of a worktree. Claude's native project discovery treats the local `.git` as the boundary — the agent cannot traverse into the parent repository. Use this for information segmentation (e.g., enterprise customers with proprietary root-level IP).

**Branch naming:** `{parent_branch}.{slug}-{type}` (dot separator, suffixed). PRs target parent branch, not main — merged via recursive fold up the tree.

**Identity:** Birth-branch as session ID (immutable, deterministic). Root TL = "root". Filesystem IS the registry — scan `.exo/worktrees/` and `.exo/agents/` to discover agents.

### Coordination

Push-based parallel worker coordination via **Claude Code Teams inbox**:

1. TL spawns workers and **returns** (no blocking wait)
2. Each worker gets `EXOMONAD_SESSION_ID` env var (parent's birth-branch)
3. When worker completes, it calls `notify_parent`
4. Server resolves parent agent from caller identity, writes to the parent's Teams inbox (`~/.claude/teams/{name}/inboxes/{inbox}.json`)
5. Claude Code's InboxPoller detects the new message and delivers it as a native `<teammate-message>` in the parent's conversation
6. TL sees the message and wakes up — no polling, no hacks

This is **native Claude Code Teams integration**. Messages from child agents arrive exactly like messages from Claude Code teammates — structured, attributed, and delivered through the official inbox mechanism. The TL doesn't poll, doesn't block, and doesn't parse raw text. It gets a proper teammate notification.

Teams inbox registration and delivery are Claude Code-only. OpenCode, Codex, and Shoal agents use their supported non-Teams delivery paths until those runtimes expose native team inbox support.

**Pipeline:** `notify_parent` → server resolves parent via `TeamRegistry` → `teams_mailbox::write_to_inbox()` → CC InboxPoller → `<teammate-message>` delivered to parent conversation.

**Bidirectional Messaging:** The `send_message` tool enables arbitrary bidirectional messaging between any exomonad-spawned agents, routing via Teams inbox, UDS, or tmux fallback depending on the target agent's type and connection status.

**Fallback:** If Teams inbox delivery fails (no team registered, inbox write error), falls back to tmux STDIN injection via buffer pattern (`load-buffer` + `paste-buffer`).

### PR Workflow

- **`file_pr`** — Create or update a PR for the current branch. Auto-detects base branch from dot-separated naming convention.
- **`merge_pr`** — Merge a child's PR (`gh pr merge` + `git fetch` for auto-rebase). TL role only.

### Built Infrastructure

| Feature | Status |
|---------|--------|
| **Teams inbox delivery** | **Live.** `notify_parent` → Teams inbox → native `<teammate-message>` in parent conversation. Full E2E verified. |
| **Cross-runtime inbox delivery** | **Built.** Messages are recorded in the ExoMonad inbox, delivered through Claude Teams inbox when available, and otherwise fall through to HTTP-over-UDS or tmux STDIN. |
| **HTTP-over-UDS delivery** (Shoal/custom agents) | **Built.** `notify_parent` → POST to `.exo/agents/{name}/notify.sock`. Fire-and-forget with 5s timeout. For custom binary agents that run their own HTTP server on a Unix socket. |
| **Event router** (tmux STDIN fallback) | Built. Fallback path: `notify_parent` → `inject_input` into parent pane via tmux buffer pattern. |
| **Event handlers** (WASM dispatch for world events) | **Built.** Third dispatch category alongside tools and hooks. Worktree event watcher calls `handle_event` on agent's PluginManager for PR review events (reviews, approvals, timeouts) and **sibling merge events**. Handlers return `EventAction` (InjectMessage, NotifyParent, NoAction). |
| **Worktree event watcher** (PR status → events) | Built. Background service watches local git worktree PRs, fires WASM event handlers, and injects notifications into agent panes. Tracks `first_seen`, `last_review_state`, and `notified_parent_timeout` per PR. |
| **OTel observability** | **Built.** Axum middleware auto-attributes every agent request span with `agent_id`, `agent.role`, `agent.parent`, `swarm.run_id`. `swarm.run_id` persisted to `.exo/run_id`, set as OTel resource attribute, propagated to children via env. Query all spans in a run: `resource.swarm.run_id = '{id}'`. Reconstruct spawn tree: `groupBy agent.parent, agent_id`. |
| **Observability contracts** | **Phase 0 scaffold.** The versioned envelope and event namespace live in `docs/observability/event-registry.json`; denominator rules live in `docs/observability/expected-events.v1.json`; validate both with `just validate-observability-contracts`. Local L1-L3 evidence may be sensitive; only the allowlisted L4 compiler output is shareable. |
| **Coordination mutexes** | Built. In-memory `MutexRegistry` with FIFO wait queues, TTL auto-expiry, idempotent acquire. Effect-only (`coordination.acquire_mutex`, `coordination.release_mutex`) — no MCP tool exposed. |
| **Tempo observability** | **Built.** Grafana Tempo for lightweight trace storage (~100-200MB RAM). Agents query traces via `curl` + TraceQL against Tempo's HTTP API (port 3200). Optional Grafana UI at `http://localhost:3000`. |
| **NotebookLM MCP** (optional) | **Vendored.** `vendor/notebooklm-mcp/` — stdio MCP server that automates Google NotebookLM via browser automation. Source-grounded, citation-backed answers from uploaded documentation. Opt-in via `extra_mcp_servers` in `config.toml`. |
| **OpenCode hooks** (TypeScript plugin bridge) | **Built.** OpenCode agents get `tool.execute.before` / `tool.execute.after` / `event` hooks via a Bun TypeScript plugin written to `.exo/opencode-plugin/` at spawn time. The plugin shells out to `exomonad hook <event> --runtime opencode`, routing to the same WASM dispatch path as Claude Code and Codex hooks. Enables role-based tool filtering and MCP call context steering (e.g. enforcing `file_pr` body format, `notify_parent` vocabulary). See `docs/decisions/opencode-hooks.md`. |
| **Codex hooks and config** | **Built.** Codex agents share `.codex/config.toml`, the ExoMonad MCP server, developer instructions, optional model, extra MCP servers, shell hooks, and lifecycle dispatch. ExoMonad installs shared Codex hook commands for `PreToolUse`, `PostToolUse`, and `Stop` into the active Codex user config so spawned worktrees do not create new hook trust prompts. Hook commands call `exomonad hook <event> --runtime codex` and use the same WASM dispatch path as the other runtimes. See `docs/decisions/codex-integration.md` and `docs/decisions/codex-hook-wire-format.md`. |

### Tempo Observability

Grafana Tempo provides lightweight trace storage with TraceQL query support. Agents query traces directly via `curl` against Tempo's HTTP API — no MCP tools needed.

```bash
# Start Tempo
docker compose -f .exo/otel/docker-compose.yml up -d

# Start Tempo + Grafana UI
docker compose -f .exo/otel/docker-compose.yml --profile grafana up -d

# Set otlp_endpoint in .exo/config.toml:
# otlp_endpoint = "http://localhost:4317"

# Endpoints:
#   OTLP:       localhost:4317 (gRPC), localhost:4318 (HTTP)
#   Tempo API:  http://localhost:3200 (TraceQL queries)
#   Grafana UI: http://localhost:3000 (optional, with --profile grafana)
```

**Querying traces (TraceQL via curl):**
```bash
# All spans in a run
curl -s 'http://localhost:3200/api/search?q=%7B+resource.swarm.run_id+%3D+%22abc%22+%7D&limit=50&spss=100'

# Find error spans for an agent
curl -s 'http://localhost:3200/api/search?q=%7B+span.agent_id+%3D+%22my-agent%22+%26%26+span%3Astatus+%3D+error+%7D'

# Parent-child structural query
curl -s 'http://localhost:3200/api/search?q=%7B+span.agent_id+%3D+%22tl%22+%7D+%3E%3E+%7B+span.agent_id+%3D+%22worker-1%22+%7D'

# Full trace by ID
curl -s 'http://localhost:3200/api/traces/{traceID}'
```

Without Tempo running, spans still appear in stderr via the tracing fmt layer.

---

## Architecture

### Components

```
Human operator in tmux
    ├── Server window: exomonad serve
    │       └── Rust runtime ↔ Haskell WASM RPC surface
    └── TL window: Python tl_loop controller
            ├── WorkPlan + durable FSM/run state
            ├── LedgerReader/Queue consumes child and watcher events
            ├── Pending gates are logged and answered explicitly
            └── EffectClient over the ExoMonad UDS
                    └── Rust runtime ↔ Haskell WASM effects
                            └── Claude Code/Codex/OpenCode workers and reviewers
                                    ├── worktree: dev.feature-a
                                    └── worktree: dev.feature-b
```

The Python controller sits between the agent harnesses and the Rust runtime:
it owns orchestration policy and durable transitions, while Haskell defines
the RPC surface and Rust executes the resulting effects. The human-facing TL
window is an observation and gate surface, not another agent coordinator.

**Haskell WASM = Embedded DSL**
- Defines tool schemas, handlers, decision logic
- Yields typed effects (no I/O)
- Compiled to WASM32-WASI, loaded via Extism
- Single source of truth for MCP tools
- Hot reload: serve mode checks mtime per tool call

**Rust = Runtime**
- Hosts WASM plugin, executes all effects (git, GitHub API, filesystem, tmux)
- Owns the process lifecycle
- REST server on UDS (started by `exomonad init`), `mcp-stdio` translates MCP JSON-RPC to REST

**Worktrees + tmux = Isolation/Multiplexing**
- Git worktrees for code isolation (no Docker containers)
- tmux windows for Claude subtrees, panes for ephemeral workers
- Each agent = worktree + window (or pane), managed by Rust runtime

### Data Flows

**MCP Tool Call:**
```
Claude Code → stdio (JSON-RPC) → exomonad mcp-stdio (translates JSON-RPC → REST)
→ UDS GET /agents/{role}/{name}/tools (list) or POST /agents/{role}/{name}/tools/call (call)
→ exomonad serve REST handler → WASM handle_list_tools / handle_mcp_call
→ Haskell dispatches to tool handler → yields effects
→ Rust executes effects via host functions → result returned
→ mcp-stdio translates REST response → JSON-RPC → stdout → Claude Code
```

**Hook Call:**
```
Claude Code → exomonad hook pre-tool-use (reads stdin JSON)
→ UDS request to server → WASM handle_pre_tool_use
→ Haskell decides allow/deny → HookEnvelope { stdout, exit_code }
→ Claude Code proceeds or blocks
```

**Session Start:**
```
Claude Code starts → exomonad hook session-start
→ WASM validates CHAINLINK_DB and yields SessionRegister plus stale-phase cleanup effects
→ Server stores in ClaudeSessionRegistry
→ root/TL WASM hooks yield memory.brief and append a nonempty continuation brief
  after the TeamCreate instruction; unavailable memory fails open with the instruction alone
→ host child spawning uses this ID when --fork-session is explicitly requested
```

**Event Handler Call:**
```
Worktree event watcher detects world event (reviewer agent review, CI status, timeout)
→ Poller resolves agent's PluginManager from plugins map
→ Calls WASM handle_event with { role, event_type, payload }
→ Haskell dispatches to EventHandlerConfig handler → returns EventAction
→ Rust acts on EventAction: InjectMessage (deliver to agent pane) or NotifyParent (deliver to parent)
```

**Fail-open:** If the server is unreachable, `exomonad hook` prints `{"continue":true}` and exits 0.

### MCP Tools Reference

All tools implemented in Haskell WASM (`haskell/wasm-guest/src/ExoMonad/Guest/Tools/`):

| Tool | Role | Description |
|------|------|-------------|
| `spawn_leaf` | root, tl | Spawn a leaf agent in own worktree+branch. Files PR when done. Agent type set by server config or explicit `agent_type`. Structured spec fields: steps, verify, boundary, context, read_first. Continuation context is prefixed automatically when available. |
| `resume_pr` | root, tl | Resume an existing issue-owned PR worktree and invocation with continuation context prefixed automatically; review feedback is scoped to the exact current PR head SHA. |
| `spawn_opencode` | root, tl | Spawn OpenCode agent in own worktree+branch. Files PR when done. Structured spec fields: steps, verify, boundary, context, read_first. |
| `spawn_codex` | root, tl | Spawn Codex agent in own worktree+branch. Files PR when done. Structured spec fields: steps, verify, boundary, context, read_first. |
| `spawn_worker` | root, tl | Spawn an ephemeral worker in a tmux pane (no branch, no PR). Just name + task. |
| `file_pr` | tl, dev | Create/update PR (auto-detects base branch from naming) |
| `merge_pr` | root, tl | Merge child PR (gh merge + git fetch) |
| `notify_parent` | tl, dev, worker | Send message to parent agent. Auto-routed via Teams inbox (primary) or tmux STDIN (fallback) |
| `memory_append` | root, tl, dev, worker | Append a validated semantic fact to the append-only session-memory ledger |
| `memory_list` | root, tl, dev, worker | List current-run session-memory records with optional filters |
| `continuation_brief` | root, tl | Render the deterministic continuation brief for the current root/TL session |
| `send_message` | all | Send message to another exomonad-spawned agent (routes via Teams inbox, UDS, or tmux) |
| `task_list` | dev, worker | List tasks from the shared Claude Code task list (auto-resolves team from TeamRegistry) |
| `task_get` | dev, worker | Get a task by ID from the shared task list |
| `task_update` | dev, worker | Update task status, owner, or activeForm in the shared task list |

**Note**: Git operations (`git status`, `git log`, etc.) and GitHub operations (`gh pr list`, etc.) use the Bash tool with `git` and `gh` commands, not MCP tools.

---

## Developing ExoMonad

### Package Inventory

All Haskell packages live under `haskell/`. See `haskell/CLAUDE.md` for full details.

| Package | Purpose |
|---------|---------|
| `haskell/wasm-guest` | WASM guest with MCP tool definitions (freer-simple) |
| `haskell/proto` | Generated Haskell proto types |
| `haskell/vendor/ginger` | Typed Jinja templates (vendored) |
| `haskell/vendor/freer-simple` | Effect system (vendored, GHC 9.12 patches) |
| `haskell/vendor/exomonad-pdk` | Extism PDK (vendored) |
| `haskell/vendor/proto3-runtime` | Protobuf runtime (vendored) |

### Where Things Go

| Thing | Location |
|-------|----------|
| New MCP tool | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/` |
| New WASM effect | `haskell/wasm-guest/src/ExoMonad/Guest/Effects/` |
| New Rust effect handler | `rust/exomonad-core/src/handlers/` |
| New proto type | `proto/` + `rust/exomonad-proto/proto/` |
| New event handler | `haskell/wasm-guest/src/ExoMonad/Guest/Events.hs` (types), `.exo/lib/` (handlers) |

### Building & Testing

```bash
cabal build all            # Build Haskell
cargo test --workspace     # Rust tests (from repo root)
just pre-commit            # Run all checks
cabal test all             # Haskell tests

# E2E tests (interactive — launches tmux session, you observe)
just e2e-messaging         # Teams inbox delivery pipeline
just e2e-oc-rewrite        # BeforeModel/AfterModel PII rewriting
just e2e-tl-loop-shadow    # Live TL trajectory beside the read-only shadow loop
just e2e-tl-loop-active    # Programmatic TL loop over a scratch repository
just e2e-slice-abandon-redispatch # Real-server closed-PR recovery acceptance
```

`just e2e-tl-loop-active` is intentionally non-interactive: it uses a bounded
Python controller, a bare scratch remote, deterministic effect/review stubs,
and the exact default TL-window command without creating an ExoMonad session.

`just rust-test` is the fast library-only Rust loop. `just test` runs the full
Rust workspace test set, including native integration targets, after building
the WASM plugins. New Rust tests must be reachable from `just test`.

### E2E Test Pattern

All E2E tests live in `tests/e2e/{name}/` and follow the same structure:

**Files:**
| File | Purpose |
|------|---------|
| `run.sh` | Setup script: creates temp repo, configures companions, runs `exomonad init` |
| `testrunner.md` | Test plan for the Claude testrunner companion (copied to `.exo/roles/devswarm/context/testrunner.md`) |
| `e2e-test.md` | Root TL rules for this test (copied to `.claude/rules/e2e-test.md`) |

**Structure of `run.sh`:**
1. **Preconditions** — Check `exomonad` binary, WASM plugins, `tmux`, `git`
2. **Temp environment** — `mktemp -d`, bare remote, working repo, `exomonad new`, symlink WASM
3. **Config** — Write `config.toml` with `yolo = true`, companions for the test scenario
4. **`exomonad init`** — Last line of the script. Creates tmux session, starts server, spawns companions, attaches.

**Companion roles:**
- **Test subject** — The agent being tested (e.g., Codex with dev role for hook rewriting)
- **Testrunner** — Claude (haiku) companion with `testrunner` role. Observes results via bash (read-only), reports via `notify_parent`

**Key conventions:**
- `shell_command = "bash"` (not nix develop — temp env has no flake)
- `yolo = true` (skip interactive prompts)
- `export GITHUB_TOKEN="test-token-e2e"` (dummy token to avoid auth errors)
- Cleanup via `trap cleanup EXIT` (kills tmux session, removes temp dir)
- Testrunner uses only `notify_parent` MCP tool + read-only bash observation
- Scenario harnesses configure their required agents; the programmatic TL
  controller consumes the plan and ledger without an interactive coordinator.

**Adding a new E2E test:**
1. Create `tests/e2e/{name}/run.sh` following the pattern above
2. Create `testrunner.md` with the test plan (phases, assertions, report format)
3. Create `e2e-test.md` with scenario-specific controller or harness rules
4. Add `just e2e-{name}` recipe to `justfile`

### Task Tracking

GitHub Issues. Branch naming: `gh-{number}/{description}`. Reference issue in commits (`[#123] ...`). Issues closed via PR merges (`Closes #123`).

### Key Design Decisions

1. **freer-simple for effects** — Standardized on freer-simple for reified continuations (WASM yield/resume)
2. **Haskell WASM as typed config DSL** — All tool/hook/event logic in Haskell, all I/O in Rust runners. The WASM yields effects; Rust executes them. Agents themselves have full tool access (bash, files, git).
3. **Haskell WASM = embedded DSL** — All logic in Haskell, Rust handles I/O only

---

## Tech Lead Praxis

The hylomorphism in [Model](#model) remains the system's conceptual frame.
The executable TL is `tl_loop`, a bounded controller that turns a structured
plan into durable dispatch, review, merge, and escalation transitions. There
is one controller per run; the tmux TL window only exposes its logs and human
gates.

### Controller contract

The controller's workflow is:

**load plan → validate state → select harnesses → dispatch → consume ledger
events → apply review gates → merge or park**.

It owns the FSM, checkpoints, budget ledger, harness selection, reviewed-head
binding, repair handoffs, merge decisions, and upward PR effects. Rust still
executes effects, Haskell still defines the RPC surface, and workers/reviewers
still own implementation and review. The controller never edits source code
or manually reviews a diff.

### Structured plans and bounded judgment

The root plan is JSON with closed keys for workers, leaves, and recursive
sub-TLs. A plan declares paths, dependencies, base refs, tests, and bounded
parallelism/retries; it cannot choose a harness, model, budget, or hidden
fallback. The selector applies `.exo/harness_policy.toml` and capability
ratings without widening an allowlist or ceiling.

Model calls are narrow structured judgments: `decompose`,
`adjudicate_review`, and `compose_repair`. Their schemas, context budgets,
attempts, token charges, and replay records are explicit. Control flow stays
in Python and the state writer, not in a free-form prompt.

### Dispatch and convergence

1. The controller validates the plan, reserves budget, and persists each
   slice before dispatching through the ExoMonad effect client.
2. A leaf works in its issue-owned branch/worktree, commits, and files its PR.
3. The watcher and reviewer produce ledger events; the controller consumes
   them by sequence number and acknowledges only after handling succeeds.
4. Review comments go to the live PR owner. A changed head is repaired through
   `resume_pr`; a fresh branch or duplicate coordinator is never created.
5. A fresh TL-adjudicated GO based on binding reviewer findings, with passing
   CI, permits `merge_pr`. Review timeout parks the slice. The controller
   verifies the post-merge state before advancing dependents.

Review orchestration has one path: watcher facts are projected into durable
state, `derive_next_action` selects one intent or wait reason, and the
`EffectJournal` executor performs the effect. A reviewer is one-shot for an
exact head. Its terminal verdict is persisted before exit; GO waits for
same-head CI and then uses compare evidence for `merge_pr`, while NO-GO or
same-head CI failure uses one same-owner `resume_pr`. A head change invalidates
the prior verdict and queues one reviewer for the new head. Pane liveness never
authorizes a merge or repair, and no path messages an exited reviewer, rewinds
the ledger cursor, edits `plan.json`, or creates a sibling owner.

### Depth, waves, and recursion

`max_parallel_slices`, dependency readiness, retry ceilings, and recursion
depth are durable gates. A child sub-TL is another `tl_run` with nested state,
not another Claude session. Parallel slices share no ownership paths; the
controller advances the next wave only after its dependencies are merged or
parked.

### Human gates and bounded failure

Review disagreement, missing CI, stale heads, budget exhaustion, liveness
failure, and harness-switch requests become named durable gates or terminal
parking causes. The operator answers a gate explicitly:

```bash
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root --name <gate> --approve
python3 ~/.exo/tl_loop.pyz gate --project-root . --run-id root --name <gate> --reject
```

The controller resumes from the checkpoint. It does not coax a model to keep
working, silently retry beyond a ceiling, or ask a second interactive TL to
make the decision.

### Plan quality

Every plan follows the same compact contract:

1. **ANTI-PATTERNS** — explicit DO NOT rules first.
2. **READ FIRST** — exact files and existing interfaces.
3. **STEPS** — concrete bounded actions.
4. **VERIFY** — exact commands and expected gates.
5. **DONE CRITERIA** — observable completion conditions.

The controller enforces the machine-checkable portions; the reviewer and
human gate cover the remainder. See [the TL ADR](docs/decisions/tl-as-loop.md)
for the boundary, borrowed patterns, and rejected alternatives.

### Event vocabulary

The controller consumes ledger projections for `[FIXES PUSHED]`, `[PR READY]`,
`[REVIEW TIMEOUT]`, `[STUCK: agent-id]`, `[FAILED: agent-id]`, CI state, PR
state, and merge conflicts. Informational worker messages remain messages;
they cannot approve a merge. Every handled event advances the durable cursor
or parks the run with an auditable cause.

---

## Documentation Tree

```
CLAUDE.md  ← YOU ARE HERE (project overview)
├── proto/CLAUDE.md    ← Protocol buffers (FFI boundary types)
├── haskell/CLAUDE.md  ← Haskell package organization
│   ├── wasm-guest/CLAUDE.md    ← MCP tool definitions (WASM guest logic)
│   └── proto/CLAUDE.md         ← Generated Haskell types for proto
├── rust/CLAUDE.md             ← Rust workspace overview (3 crates)
│   ├── exomonad/CLAUDE.md  ← MCP server + hook handler (binary)
│   ├── exomonad-core/CLAUDE.md ← Unified library: framework, handlers, services, protocol, UI types
│   └── exomonad-proto/     ← Proto-generated types (prost) for FFI + effects
├── tl_loop/CLAUDE.md          ← Programmatic TL controller (FSM + Rust UDS client boundary)
├── .exo/roles/devswarm/context/tl.md ← Decompose-prompt reference (not the TL protocol)
├── tests/e2e/                 ← E2E tests (see § E2E Test Pattern)
│   ├── messaging/             ← Teams inbox delivery test
│   └── hook-rewrite/          ← PII rewriting hooks test
├── docs/guides/               ← Operator-facing how-to guides
│   ├── programming-the-tl.md  ← Authoring harness_policy.toml, review-policy.toml, plan.json (+ worked examples)
│   └── migrating-to-the-tl-loop.md ← Carrying an existing project forward (credentials + log import)
├── docs/architecture/         ← Cross-cutting architecture references
│   └── agent-system.md        ← Role × tool matrix, hook deny rules, per-role state machines, PR review flow, controller gates (+ .html view)
└── docs/decisions/            ← Architecture decision records (living docs)
    └── tl-as-loop.md          ← Programmatic TL boundary and borrowed patterns
```

| I want to... | Read this |
|--------------|-----------|
| Program the TL for a new project | `docs/guides/programming-the-tl.md` |
| Migrate an existing ExoMonad project | `docs/guides/migrating-to-the-tl-loop.md` |
| Add FFI boundary types | `proto/CLAUDE.md` |
| Understand MCP tool architecture | `rust/exomonad/CLAUDE.md` |
| Work on exomonad-core framework | `rust/exomonad-core/CLAUDE.md` |
| Work on effect handlers or services | `rust/exomonad-core/` (handlers/, services/) |
| Extend the effect framework | `rust/exomonad-core/` (effects/) |
| Understand shared protocol types | `rust/exomonad-core/` (protocol/) |
| Work with external service clients | `rust/exomonad-core/` (services/external/) |
| Work on WASM guest (MCP tools) | `haskell/wasm-guest/CLAUDE.md` |
| Work on the programmatic TL controller | `tl_loop/CLAUDE.md` |
| Understand the TL architecture decision | `docs/decisions/tl-as-loop.md` |
| Write or review a decompose prompt | `.exo/roles/devswarm/context/tl.md` |
| Add or modify E2E tests | `CLAUDE.md` § E2E Test Pattern + `tests/e2e/messaging/` as reference |
| Understand architectural decisions | `docs/decisions/` |
| See role tool matrix, hook rules, state machines, PR review flow | `docs/architecture/agent-system.md` |

---

## References

- [rust/exomonad/CLAUDE.md](rust/exomonad/CLAUDE.md) — MCP server + WASM host
- [haskell/wasm-guest/CLAUDE.md](haskell/wasm-guest/CLAUDE.md) — MCP tool definitions
- [freer-simple](https://hackage.haskell.org/package/freer-simple) — Effect system
- [Anthropic tool use](https://docs.anthropic.com/en/docs/tool-use)


The TL-loop configuration also requires `.exo/harness_capability.toml`, whose capability entries must cover every harness allowed by `.exo/harness_policy.toml`.
