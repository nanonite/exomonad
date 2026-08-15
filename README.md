# ExoMonad

Exomonad builds on the agentic loop to provide a Tree-of-agents model that recursively unfolds work across git worktrees, accumulating scaffolding commits and context as it grows. Swarms of fast cheap agents implement specs at the leaf nodes. Each node files PRs against its parent over waves of recursively nested trunk-based development, and the tree folds back up as those PRs are reviewed, CI-checked, and merged.

The tech lead that drives this is **a program, not a prompt**. `tl_loop` is a bounded, resumable Python controller: it loads a structured plan, selects a harness within human-authored budgets, dispatches leaves and recursive sub-TLs, consumes an immutable event ledger, applies review and CI gates, and merges — or parks the run at a named human gate. The model is called only for three narrow structured judgments (`decompose`, `adjudicate_review`, `compose_repair`). Control flow lives in Python and durable state, not in a conversation.

It hooks into Claude Code and Codex CLI, using their existing binaries and your existing subscription plans. Codex implements. A reviewer agent reviews. Each model does what it's best at. All orchestration logic — tool dispatch, hooks, event handling, PR review routing — is defined in Haskell effects executed by a shared Rust server. Agents run in tmux windows and panes, isolated via git worktrees. No Docker, no web dashboard, no new UI to learn.

![tmux devswarm — TL dispatching to three Codex workers in parallel, each in its own worktree. Bottom panes show workers mid-execution.](img/exomonad_tmux_devswarm.png)

## Try It

Run ExoMonad on any GitHub repo. One command, clean container, no local dependencies beyond Docker.

```bash
git clone https://github.com/tidepool-heavy-industries/exomonad
cd exomonad
just install-all-dev                              # Build artifacts (first time only)
./try-exomonad/run.sh https://github.com/user/repo
```

This builds a Docker image with the correct tmux version, pre-built WASM, and all dependencies. You land in a tmux session with MCP tools ready. Auth is automatic — your `~/.claude` and `~/.codex` credentials are mounted from the host.

See [try-exomonad/README.md](try-exomonad/README.md) for details.

## Install (Native)

**Prerequisites:** [Nix](https://nixos.org/) (with flakes), Python 3.11 or newer, [tmux](https://github.com/tmux/tmux/wiki), and [just](https://github.com/casey/just).

Native installation packages the stdlib-only TL controller at
`~/.exo/tl_loop.pyz` and `exomonad init` refreshes it when needed. Target
repositories do not need a checkout of the `tl_loop` Python package: use
`python3 ~/.exo/tl_loop.pyz` for direct status, gate, and preflight commands.
The packaged controller requires Python 3.11 or newer. Development-only pytest
and ruff may continue to use the repo-local `tl_loop/.venv` through
`EXOMONAD_PY`; that environment is not used to run the installed controller.

Install Nix if you don't have it:

```bash
sh <(curl -L https://nixos.org/nix/install) --daemon
mkdir -p ~/.config/nix && grep -q 'nix-command flakes' ~/.config/nix/nix.conf 2>/dev/null || echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf
```

Then build and install:

```bash
git clone https://github.com/tidepool-heavy-industries/exomonad
cd exomonad
just install-all      # Release build (optimized, slower compile)
# or
just install-all-dev  # Debug build (fast compile, good for development)

# Compatibility forms
just install all       # Release install
just install all-dev   # Debug install
```

First build downloads Nix dependencies and initializes the WASM toolchain — subsequent builds are cached. Artifacts are installed to `~/.cargo/bin/exomonad` and `~/.exo/wasm/`.

## Getting Started

ExoMonad works on any Git repository. The canonical PR, review, and CI loop is
Forgejo-backed. Four controller files under `.exo/` are required:
`config.toml`, `harness_policy.toml`, `review-policy.toml`, and
`harness_capability.toml`. The structured work plan lives separately at
`.exo/tl-loop/plan.json`.

```bash
cd your-project/
exomonad new        # One-time: .exo/config.toml, .gitignore, CI + rules templates
```

Complete the project-local controller setup before starting a run. Begin with
one allowed harness/model entry per role and add capability entries for every
allowed entry:

```toml
# .exo/harness_policy.toml — the human-authored allowlist and budget ceiling.
# Required. A missing or invalid file is a startup error; no default is synthesized.
[roles.tl]
allow = ["codex/gpt-luna"]
cost_rank = { "codex/gpt-luna" = 1 }
token_budget = 120000
escalate_after_attempts = 1
# ... and the same for [roles.worker] and [roles.reviewer]
```

Set the Forgejo connection values in `.exo/config.toml` using the credentials
for the repository and webhook. Do not commit live credentials:

```toml
forgejo_url = "http://localhost:3000"
forgejo_token = "<project-token>"
forgejo_webhook_secret = "<shared-secret>"
```

The generated `.exo/review-policy.toml` is usable as-is for low-risk work, but
review its `external_review_paths` and complexity thresholds for the project's
risk surface. A merge requires reviewer approval and CI status `success` or
`neutral` for the same PR head; missing, pending, or failed CI cannot pass the
canonical merge rule.

The plan must use disjoint `boundary` globs and real verification commands:

```json
{
  "run_id": "root",
  "budgets": { "tokens": 400000, "wall_seconds": 14400 },
  "plan": {
    "leaves": [
      {
        "name": "token-refresh",
        "task": "Implement refresh-token rotation for the OAuth provider.",
        "boundary": ["src/auth/**"],
        "verify": ["cargo test -p auth refresh"]
      }
    ]
  }
}
```

Validate all required controller files before starting the server:

```bash
python3 ~/.exo/tl_loop.pyz preflight --project-root .
```

```bash
exomonad init       # Creates tmux session with Server + TL windows
                    # Writes .mcp.json (auto-registers MCP tools)
                    # Starts background server on .exo/server.sock
                    # TL window runs `python3 ~/.exo/tl_loop.pyz` — the controller
```

`exomonad init` waits for the server socket and then applies a bounded
five-second TL startup liveness gate. The controller must appear and remain
alive through that startup interval; otherwise init exits non-zero and keeps a
durable failure reason. This is intentionally not semantic controller
readiness or heartbeat monitoring: later hangs and failures remain visible
through the Watcher, `status`, and `controller-exit.json`. An authoritative
ready/heartbeat protocol is separate future work.

The **TL window is the controller**, not a harness session — do not type `claude` into it. It is an observation and gate surface:

```bash
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root --watch --interval 2
python3 ~/.exo/tl_loop.pyz gate   --project-root . --run-id root --name <gate> --approve
```

A run ends at `TLDone` or `TLFailed`. Bounded failures — retries exhausted, budget exhausted, review stuck, no capable harness, stall detected — park with an auditable cause and wait for an explicit human gate. The controller never retries past a ceiling or silently switches harness.

Child dispatch has its own durable boundary. Each worker or leaf records an
intent before the spawn effect, remains `dispatch_unconfirmed` after an
accepted request, and becomes `spawned` only when the correlated
`agent.spawned` ledger event is consumed. A rejected request opens
`tl-dispatch-failed`; a missing confirmation after the five-second dispatch
window opens `tl-dispatch-timeout` with the intent and last boundary visible in
`status`. Restart reconciliation uses the persisted intent and never issues a
duplicate spawn for the same attempt.

### Ordered recursive sub-TLs

Direct sibling sub-TLs may declare a positive numeric `order`. Missing order is
legacy shorthand for order 1; explicit orders must be present on every sibling
and contiguous from 1, so a mixture of explicit and missing values is rejected.
Different orders run sequentially. Same-order sub-TLs run concurrently within
the configured bound, then their aggregate integration is serialized in stable
sub-TL ID order before the next numeric order begins. The plan does not encode
merge priority.

Each aggregate candidate keeps review evidence bound to its reviewed head and
integration evidence bound to the current base, integrated tree, and CI. A
moving base requires base revalidation; a real conflict requires same-owner
`resume_pr` repair. Do not spawn a worker or rebase solely because the base
moved. A restart resumes the persisted stage and integration checkpoint rather
than duplicating a child or merge.

See [Programming the TL](docs/guides/programming-the-tl.md#ordered-plan-examples)
for validated JSON examples covering parallel, nested, and sequential stages.

Once a run is live you can steer it three ways, in increasing richness:

```bash
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root          # phase, slices, gates
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root --watch   # refresh continuously
python3 ~/.exo/tl_loop.pyz gate   --project-root . --run-id root --name <gate> --approve
export EXOMONAD_CONTROL_TOKEN=...                                  # unlocks /control
curl --unix-socket .exo/server.sock -H "X-Exomonad-Control-Token: $EXOMONAD_CONTROL_TOKEN" \
     http://localhost/control/runs/root
```

The `/control` surface adds a schema-versioned read model over run state
(slices, ordered stages, per-head review evidence, base-bound integration
evidence, budgets, transitions, and `next_transition`) plus the only two
mutations an operator may make: answer an existing named gate, and propose a
plan change that stays inert until confirmed. It can never merge, approve a
review, set a verdict, or widen a policy — those stay with the controller.

Afterwards, the run is measurable rather than merely reviewable. The controller's own decisions — gates opened and answered, slices parked and why, merge decisions, RLM judgment retries — land in the same append-only ledger as agent and PR activity:

```bash
exomonad logs import --source .exo/ledger/segments   # -> .exo/analysis/atlas.db
exomonad logs measure --output .exo/analysis/measurement
```

That turns "how long do my gates sit unanswered" and "which park cause dominates" into numbers instead of impressions.

> **Start here:** [**Programming the TL**](docs/guides/programming-the-tl.md) is the full guide — the four required config files, the plan schema, worked examples, park causes, and the operator control plane.
>
> Coming from an older ExoMonad project? [Migrating an existing project](docs/guides/migrating-to-the-tl-loop.md).

## How It Works

**System layers, each doing one thing:**

| Layer | What | Why |
|-------|------|-----|
| **Python `tl_loop`** | The TL controller: FSM, checkpoints, budgets, harness selection, review gates, merge decisions | Deterministic, resumable, replayable, unit-testable |
| **Haskell WASM** | Typed config DSL: tool schemas, dispatch, hooks, event routing | Deterministic, testable, hot-reloadable |
| **Rust runtime** | Executes effects (git, Forgejo API, filesystem, tmux CLI), owns the immutable ledger | Performance, safety |
| **tmux** | Process isolation (windows for subtrees, panes for workers) | Multiplexing without Docker |

Haskell WASM is a typed configuration DSL — tool schemas, dispatch logic, hooks, event routing — with the full power of a type system and effect system. The WASM yields typed effects; Rust executes the I/O. This means tool logic is deterministic, testable, and hot-reloadable — edit a Haskell tool, run `just wasm-all`, and the next MCP call picks up the change.

The controller sits on top and calls that same boundary. It owns orchestration *policy*; it never edits source code, never reviews a diff by hand, and never scrapes tmux.

**The loop:**

```
load plan → validate state → select harness → dispatch
    → consume ledger events → apply review gates → merge or park
```

Every transition is a durable write under `.exo/tl-loop/<run_id>/run.json`. Events come from the immutable ledger at `.exo/ledger/segments/` by global sequence number, and are acknowledged only after handling succeeds — so a restart resumes at `cursor + 1` rather than replaying or dropping work.

**What gates a merge.** The controller applies two authorities plus one integrity invariant:

```text
merge_allowed =
      tl_adjudicated_go(current_head_sha)
   && ci_success_or_neutral(current_head_sha)
   && current_head_sha == live_pr_head_sha
```

The reviewer supplies binding findings for the exact head; the TL's
`adjudicate_review` call turns those findings and the TL-owned acceptance
criteria into `GO`, `GO-WITH-NITS`, or `NO-GO`. CI is the machine authority and
must be `success` or `neutral` for the same head. Head binding is an integrity
invariant, not a separate approval authority.

Projects may add optional policy checks for declared risk paths or large diffs
through `.exo/review-policy.toml`. These checks are not universal approval
layers. A review timeout parks the slice at a named gate and never permits a
merge.

A `NO-GO` composes a seven-section repair handoff and dispatches it through `resume_pr` — same owner, same worktree, same branch, same PR. Never a new branch, never a `-2` suffix.

**Agent types:**

| Plan entry / tool | Creates | Isolation | Use case |
|-------------------|---------|-----------|----------|
| `leaves` / `spawn_leaf` | Codex in own worktree + window | Own branch, files PR | Implementation work that needs review and CI |
| `workers` / `spawn_worker` | Ephemeral agent in a tmux pane | Shared directory, no branch, no PR | Research or narrow in-place edits |
| `sub_tls` | A nested `tl_run` with its own checkpoint | Own branch, PR targets the parent | Recursive decomposition and stage ordering |

Sub-TLs run in numbered stages: different orders are sequential, while
same-order siblings run concurrently and integrate in deterministic sub-TL ID
order. Top-level leaves still run in parallel with no ordering. Use an order-2
sub-TL to express documentation after the order-1 code stage merges.

**Communication:** Child agents call `notify_parent` when done; messages arrive as native teammate notifications via the Teams inbox. The inbox is a human and worker delivery surface — the controller coordinates on the durable ledger, and a free-form message can never approve a merge.

## Available Tools

| Tool | Role | Description |
|------|------|-------------|
| `spawn_leaf` | root, tl | Spawn a leaf agent in its own worktree + branch; files a PR |
| `spawn_worker` | root, tl | Spawn an ephemeral worker in a tmux pane (no branch, no PR) |
| `resume_pr` | root, tl | Resume the existing issue-owned PR worktree after review or CI feedback |
| `file_pr` | tl, dev | Create or update a PR for the current branch |
| `merge_pr` | root, tl | Merge a child agent's PR and fetch changes |
| `notify_parent` | tl, dev, worker | Send message to parent agent via Teams inbox |
| `send_message` | all | Send message to any agent (Teams, UDS, or tmux) |
| `memory_append` / `memory_list` | root, tl, dev, worker | Append to / read the session-memory ledger |
| `continuation_brief` | root, tl | Render the deterministic continuation brief |
| `task_list` / `task_get` / `task_update` | dev, worker | Shared Claude Code task list |

All tools are defined in Haskell WASM. Rust never defines a tool schema, parses tool arguments, or contains tool logic.

## Development

```bash
just install-all-dev    # Full build (WASM + Rust + install)
just wasm-all           # Rebuild WASM only (after Haskell changes)
just role-hook-tests    # Run devswarm role hook/state-machine tests in WASM
just proto-gen          # Regenerate proto types (Rust + Haskell)
just test               # Full Rust workspace tests (builds WASM first)
just rust-test          # Fast library-only Rust loop
just tl-loop-golden     # Regenerate the TLPhase parity fixture after a phase change
just e2e-tl-loop-active # Bounded controller run over a scratch repo (non-interactive)
just fmt                # Format all code
```

All `just` recipes handle their own Nix dependencies — no need to be in a `nix develop` shell.

## Documentation

| I want to... | Read this |
|--------------|-----------|
| Program the TL for a new project | [docs/guides/programming-the-tl.md](docs/guides/programming-the-tl.md) |
| Migrate an existing ExoMonad project | [docs/guides/migrating-to-the-tl-loop.md](docs/guides/migrating-to-the-tl-loop.md) |
| Understand the controller boundary | [docs/decisions/tl-as-loop.md](docs/decisions/tl-as-loop.md) |
| Work on the controller itself | [tl_loop/CLAUDE.md](tl_loop/CLAUDE.md) |
| See role tool matrix, hooks, state machines, review flow | [docs/architecture/agent-system.md](docs/architecture/agent-system.md) |
| Get the full architecture and data flows | [CLAUDE.md](CLAUDE.md) |

## License

ExoMonad is released under the [BSD 3-Clause License](LICENSE).
