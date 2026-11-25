# Configuration reference

Moved verbatim from the root `CLAUDE.md`.

**Bootstrap:** `exomonad new` auto-creates `.exo/config.toml` (empty, all defaults), `.gitignore` entries, and `.forgejo/workflows/ci.yml` if missing. Works in any project directory. All fields are optional — auto-detection handles the common case. The CI scaffold uses GitHub Actions syntax with language-specific defaults, and leaves workspace-specific build/test customization where needed. **Claude rules:** `exomonad new` copies `.exo/rules/exomonad.md` → `.claude/rules/exomonad.md` (if the template exists and the destination doesn't). Template resolution: project-local `.exo/rules/` → global `~/.exo/rules/`. This gives fresh Claude instances automatic knowledge of exomonad MCP tools.

```toml
# All fields below are optional — shown with their auto-detected defaults
default_role = "tl"          # auto-detected from .exo/roles/ if exactly one role exists
project_dir = "."
shell_command = "nix develop" # environment wrapper for TL tab + server
# tmux session name. Optional — defaults to the project directory's own name.
# Resolution: config.local.toml > config.toml > the directory name. A '.' is
# rewritten to '_' (tmux forbids dots in session names) and the rewrite is
# logged. At most 36 characters: a longer name is REJECTED at config load, not
# truncated, because truncation would leave the server probing a session nobody
# created and reporting every agent as dead. The error names the value, its
# length, the limit, and the file to change. `exomonad init --session` is held
# to the same limit. `exomonad serve` logs the resolved name at startup.
#   tmux_session = "exomonad"
wasm_dir = ".exo/wasm"       # project-local (default), override for shared installs
wasm_name = "devswarm"       # auto-detected from .exo/roles/ if exactly one role exists
model = "sonnet"             # explicit TL model when configured; accepted task policy may select another allowed model
root_agent_type = "claude"   # legacy compatibility; init always starts the Python TL controller
spawn_agent_type = "codex"   # default harness for workers, leaves, and companions
worker_model = "gpt-6-luna"  # optional model override for Codex, Claude Code, or OpenCode workers
# Role-specific effort defaults; a controller-selected route can override them.
tl_effort_level = "medium"   # TL/subTL inference route
worker_effort_level = "medium"
poll_interval = 60           # optional — GitHub poll cycle in seconds (default: 60)
# TL loop operational timeouts (seconds). Lifecycle progress is driven by authoritative events.
# Launch-capable effects (resume_blocked_leaf, spawn_leaf/spawn_worker/spawn_reviewer)
# use LAUNCH_CAPABLE_TIMEOUT_SECONDS (180s), not tl_transport_timeout_seconds: the host
# queues them and then launches, so a client timeout never decides their outcome.
  tl_transport_timeout_seconds = 10.0           # UDS RPC timeout for non-launch tools (default: 10)
  tl_active_tail_timeout_seconds = 30.0         # ledger tailer active-tail timeout (default: 30)
  tl_task_timeout_seconds = 3600.0              # per-invocation active-work ceiling; 0 disables enforcement
  tl_preflight_runtime_paths = [".my-tool/runtime/"] # extra relative runtime paths ignored by spawn preflight
# TL loop dispatch-retry budget. Only a refusal that PROVES nothing was created is
# retried; an ambiguous refusal is held for evidence and never re-driven. The limit
# must be >= 1, both delays positive, and the cap not below the base.
  tl_dispatch_retry_limit = 3                  # scheduled retries before parking (default: 3)
  tl_dispatch_retry_base_delay_seconds = 5.0   # delay before the first retry; doubles each (default: 5.0)
  tl_dispatch_retry_max_delay_seconds = 60.0   # ceiling on that backoff (default: 60.0)
forgejo_url = "http://localhost:3000"           # optional — Forgejo base URL
forgejo_token = "forgejo_pat"                      # optional — Forgejo API token
forgejo_webhook_secret = "shared-secret"           # optional — webhook signature secret

# Extra MCP servers (HTTP or stdio). Included in .mcp.json for all agents.
[extra_mcp_servers.metacog]
type = "http"
url = "http://localhost:8080"

# OpenCode-specific compatibility configuration. These model fields apply only
# when the corresponding route uses OpenCode; generic `model`/`worker_model`
# settings take precedence for their roles.
[opencode]
use_embedded_key = true  # true = use embedded key in opencode binary, false = use OPENROUTER_API_KEY
# tl_model/worker_model accept OpenCode model IDs; CLI effort uses the
# provider/model#variant form.

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

Gemini is retired; old configurations fail closed with: `agent_type 'gemini' is retired; use 'codex' (model: the top-level \`model\` in your Codex config, or EXOMONAD_CODEX_MODEL). See CLAUDE.md Configuration.` The message names no model on purpose: every model probed against a ChatGPT-account Codex login is refused the same way, so only the account knows which ones run. `exomonad new` resolves the model its harness policy names from your Codex config and refuses to scaffold when it cannot.

**Role-specific harness, model, and effort:** `--worker` and `--reviewer` select
their harnesses independently; `--worker-model` applies to Codex, Claude Code,
and OpenCode. TL, worker/dev-leaf, and reviewer routes remain separate. An explicit
CLI route wins, then the accepted task-policy selection, then the role's configured
route, then an inherited harness default when no model was selected. A policy-selected
model is never replaced by the machine-wide Codex model. Effort precedence is CLI
flag > accepted policy > explicit environment > local config > project config > medium
default. Worker effort is inherited
by forked TLs, leaves, ephemeral workers, and companions. OpenCode receives the
resolved level as its model `--variant`; Codex receives the resolved level as
`model_reasoning_effort` in its rendered config, while Shoal alone logs that effort is
ignored because it has no stable effort interface.

Changing a role's harness, model, or effort mid-run is supported as a routing
transition: the successor generation runs the currently configured route and the
old route is retained as evidence. A configured harness is the role route from
`config.toml` plus the role's `allow` list in `.exo/harness_policy.toml`; anything
outside both still requires explicit human approval. See
[docs/guides/changing-routes-mid-run.md](docs/guides/changing-routes-mid-run.md).

`exomonad init` checks that those two sources agree before it starts or
continues a controller. For the worker and reviewer roles it resolves the route
from `config.toml` (including CLI/environment overrides) and requires the
harness to appear in that role's `allow` list. When the route names a model and
every allow entry for that harness is model-qualified, the model must equal one
of those entries; a bare `harness` entry allows any model of that harness. A
disagreement is a hard error that names the role, the resolved `harness/model`,
the config field and file, the allow list, and both files the operator can
change (`config.toml` field or `roles.<role>.allow` in
`harness_policy.toml`). There is no bypass flag, environment switch, warning
path, or automatic edit. A missing policy is left to the controller's existing
startup error, and a malformed policy is parsed once by the shared Rust
definition. `root_agent_type` is legacy compatibility only: init always starts
the Python TL controller, which does not read it, and `roles.tl.allow` is
validated by `tl_loop` startup.

**TL-loop harness policy:** `.exo/harness_policy.toml` is the required human-authored
allowlist and budget boundary for the programmatic controller. It must contain exactly
`roles.tl`, `roles.worker`, and `roles.reviewer`. Each role declares `allow` (ordered
`harness/model` entries), `cost_rank` (one positive rank for every allowed entry),
`token_budget` (a positive role ceiling), `per_harness_budget` (optional positive
ceilings whose keys must be allowed), and `escalate_after_attempts` (a positive
NO-GO threshold). A missing or invalid file is a startup error; the selector never
creates a permissive default or widens these boundaries. The Rust definition in
`rust/exomonad-core/src/services/harness_policy.rs` is shared by the spawn guard
and init, and is pinned to `tl_loop` by
`rust/exomonad-core/tests/fixtures/harness_policy_agreement.{toml,json}` and
`tl_loop/tests/test_harness_policy_agreement.py`. The Rust server reads the file
once in `AgentControlService::new`, and the controller reads it once at controller
start (`tl_run`); an edit while both are running has no effect until both restart.

`roles.tl.token_budget` covers model-authored plan proposals and review-stuck
judgments across controller restarts. Their observed token usage is added to
the run checkpoint's `budgets.ledger.role_spent.tl` counter before the result
can drive another controller action; continuation restores that counter before
allowing another roles.tl call.

A worker slice reserves budget when it is dispatched and again for each
same-slice continuation (`resume_pr`, `resume_blocked_leaf`) on the route that
continuation actually runs. For `resume_pr` the controller reserves against its
configured-allowlist guess before the effect, reads the `runtime`/`model` the
host launches from the confirmed invocation handoff, and retargets the same
charge to that allowlist entry; `resume_blocked_leaf` reserves the accepted
route it carries. Because the resume effect returns before its invocation runs,
the charge is not settled at effect time: it records the launched
`invocation_id` and stays reserved until the controller reads that invocation's
`agent.invocation.finished` ledger row. A finished row that reports usage
settles the measured actual through the same `reconcile` a dispatch uses; a
finished row with no usage keeps `actual="unknown"`, conserves the estimate, and
warns. Settlement is one atomic reconcile, so a reconciled charge is never
settled twice. A one-shot Claude invocation reports usage: the host launches it
with `claude -p --output-format json`, captures the harness envelope into the
invocation's own `.exo/agents/<agent>/usage.json`, and the finish path reads
that bounded file once, sums input tokens (including cache creation and cache
reads) plus output tokens, and writes `usage` on the finished row. Codex,
OpenCode, Shoal, `Process`, and interactive Claude companions have no
established per-invocation usage source, so their finished rows carry no usage
and their continuations conserve the estimate. No usage is ever inferred from
elapsed time, text length, or a per-agent/per-issue store. The continuation
charge names a stable continuation identity so a replayed continuation is
charged once, and it never rewrites the earlier harness's spend. If the role
ceiling or the launched route's `per_harness_budget` would be exceeded, the
resume is parked with `park_cause=budget_exhausted` and the needs-human issue
path, and the route is never silently switched to a cheaper harness. If the
confirmed handoff names no invocation, the controller resolves it from the
durable owner invocation record and the matching `agent.invocation.started`
rows; unique evidence binds and settles, while absent or ambiguous evidence
releases the reservation and parks with `missing_handoff`, the human gate, and
the continuation and reservation recorded in `park_audit`. Time and silence are
never evidence.

Task duration uses one explicit precedence: a slice's `task_timeout_seconds` in
`plan.json` overrides the role's value in `.exo/harness_policy.toml`, which
overrides project `tl_task_timeout_seconds`, which defaults to 3600 seconds.
A value of `0` or explicit `null` means no ceiling. Elapsed time never infers
that a worker is dead or stalled; it may enforce only this declared, typed,
journaled task ceiling.

That ceiling is **invocation-scoped and active-work-scoped**, not a wall-clock
lifetime for the task. Each controller invocation builds a fresh monotonic work
clock; the budget is charged only while the invocation is actually executing
proven producer/repair work for the slice. Between-invocation downtime and
replay/catch-up charge zero, the historical `dispatch_started_at` is never
subtracted from the current time (it stays as audit evidence), a completed
producer awaiting parent aggregate integration is never timed, and an inactive
interval stops charging. Consequently a controller that keeps crash-restarting
does not accumulate toward the ceiling across invocations, and `0`/`null`
remains the only way to remove the ceiling entirely.

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
  - `tmux_session`: `config.local.toml` > `config.toml` > the project directory
    name, validated at load (36-character limit, `.` rewritten to `_`)

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

**Codex project-root configs are never written, and a stale one is fatal to start.** Codex agents use per-agent `.codex/config.toml` files for MCP and hook settings — one per agent directory (`.exo/agents/<agent>/`, including `.exo/agents/<companion>/`), never at the project root. Codex applies a project-root config to *every* session opened in the project, not just to the agent it was written for, so ExoMonad writes none. ExoMonad used to write one at the project root for a Codex root agent; projects set up before that changed still carry the gitignored file, and nothing removes it for you because the path may equally be yours (`rust/exomonad-core/src/codex_stale_root_config.rs`).

Both `exomonad init` and `exomonad new` check for it before any other side effect and **refuse to start** while it is present, naming the file and the exact repair:

```bash
mv .codex/config.toml .codex/config.toml.exomonad-stale.1.bak
# or, equivalently:
exomonad init --retire-stale-codex-root-config
```

Recognition requires two independent signals, because one alone is not evidence ExoMonad wrote the file: an `mcp_servers.exomonad` entry in ExoMonad's own `mcp-stdio --role <tl|root> --name <agent>` form, **and** at least one `<exomonad> hook <pre-tool-use|post-tool-use|stop> --runtime codex` command under `hooks`. Only `hooks` subtrees are searched, so prose in `developer_instructions` quoting a hook command is not a signal; a file that does not parse as TOML is never claimed. A hand-written project-root `.codex/config.toml` is left completely alone.

The flag *moves* the file to the first unused `.codex/config.toml.exomonad-stale.<n>.bak`; it never overwrites a backup and never edits the file. ExoMonad will not silently edit or delete it. The Codex *hook trust* the retired config left in `~/.codex/config.toml` is not reclaimed automatically — that residue is operator-only, through `exomonad codex-prune-trust` (see `rust/exomonad-core/src/codex_trust_maintenance.rs`).

**Claude Code settings help:** We have a Claude Code configuration specialist (preloaded with official documentation) available as an oracle for hook syntax, settings structure, MCP setup, and debugging.
