---
description: "ExoMonad agent orchestration rules — loaded into every agent's context in projects using exomonad"
---

# ExoMonad Agent Rules

## Model

ExoMonad is a hylomorphism over context windows. Unfold = plan + scaffold + spawn. Fold = merge + integrate + PR upward. Each agent is a triad: worktree (filesystem) + context window (attention) + actor (messages). See `CLAUDE.md` § Model for the full conceptual framework.

## MCP Tools

Use exomonad MCP tools for orchestration. Git and GitHub operations use `git` and `gh` CLI commands, NOT MCP tools.

| Tool | Role | What it does |
|------|------|-------------|
| `fork_wave` | root, tl | Fork N parallel agents — Claude, Codex, or OpenCode (own worktrees, agent type from config or explicit `agent_type`). Claude inherits context via `--fork-session`; Codex/OpenCode require context injected in the task spec. |
| `spawn_leaf` | root, tl | Spawn Codex agent in own worktree+branch (files PR). Structured spec fields: steps, verify, boundary, context, read_first |
| `spawn_worker` | root, tl | Spawn ephemeral Codex worker in tmux pane (no branch, no PR). Just name + task |
| `file_pr` | tl, dev | Create/update PR (base branch auto-detected from branch naming) |
| `merge_pr` | root, tl | Merge a child's PR |
| `notify_parent` | tl, dev, worker | Send message to parent agent |
| `memory_append` | root, tl, dev, worker | Append a validated semantic fact to the append-only session-memory ledger |
| `memory_list` | root, tl, dev, worker | List current-run session-memory records with optional filters |
| `continuation_brief` | root, tl | Render the deterministic continuation brief |
| `send_message` | all | Send message to any exomonad-spawned agent |
| `task_list` | dev, worker | List tasks from the shared task list |
| `task_get` | dev, worker | Get a task by ID |
| `task_update` | dev, worker | Update task status, owner, or activeForm |

## Agent Hierarchy

- **TL controller**: The Python `tl_loop` process. Loads structured plans, selects bounded harnesses, dispatches work, consumes the ledger, applies review gates, merges, and parks or escalates runs. It never edits source code.
- **Dev (Leaf)**: Codex. Implements a focused spec, files PR. No spawning.
- **Worker**: Codex. Ephemeral pane, no branch. Research or in-place edits.
- **Reviewer**: The configured review harness. Reviews the issue-owned PR head and never authors its branch.
- **Human operator**: Observes the controller window and answers explicitly named durable gates.

Worker and reviewer roles retain their role-specific hook restrictions. The controller's no-edit boundary is architectural: implementation belongs to leaves and review belongs to reviewers; a failed or stuck run becomes a durable gate rather than a redispatch prompt.

## The TL Protocol: Programmatic Loop

`tl_loop` is the only coordinator for a run. The hylomorphism remains the
conceptual model, but its transitions are durable Python state and ledger
events rather than an interactive prompt protocol.

### 1. Load and validate

Load the closed-key `WorkPlan`, checkpoint, harness policy, capability map, and
review policy. Reject unknown fields, invalid ownership paths, cycles, missing
test plans, invalid budgets, and natural-language coordinator prompts. Never
invent a fallback plan or widen a policy on startup.

### 2. Select and dispatch

Classify each ready slice and select the cheapest allowed harness/model that
meets its capability and budget constraints. Persist the reservation and slice
ownership in the same state write, then dispatch through the existing
ExoMonad effect client. A child sub-TL is a nested `tl_run`, not another
interactive coordinator session.

### 3. Consume and converge

Consume the immutable ledger by global sequence through the typed projection
and bounded in-process queue. Acknowledge only after handling succeeds.

The loop advances only on authoritative events: PR/review/CI state, fixes
pushed, approved or timed-out review, merge result, worker failure, and liveness
observation. Reviewed-head SHA binding prevents merging a verdict for another
head. Review repair uses `resume_pr` and never creates a stacked or duplicate
owner.

### 4. Merge, park, or gate

An approved current head with passing CI is mergeable. A bounded timeout may
merge only when policy permits and CI passes. Retry, parallelism, recursion,
budget, and review-round ceilings are explicit. Exhaustion parks the run with
an auditable cause; the operator can approve or reject a named gate with the
`tl_loop gate` command and resume from the checkpoint.

The controller is complete only at `TLDone` or `TLFailed`. It does not wait on
an interactive prompt, scrape tmux output, manually fix a leaf, or silently
continue past a bound.

## Spec Quality

Specs are self-contained — the leaf has no context from previous attempts. Every spec must include:

1. **Anti-patterns** (FIRST) — known failure modes as explicit DO NOT rules
2. **Read first** — exact files to read (CLAUDE.md, source files)
3. **Steps** — numbered, each step = one concrete action with code snippets
4. **Verify** — exact build/test commands
5. **Done criteria** — what "done" looks like

Include complete code snippets. Name every file by full path. Include exact commands, not "run the tests."

## Convergence Protocol

Convergence is a controller transition over leaf, reviewer, watcher, and
ledger state:

1. The controller persists a slice and dispatches its issue-owned leaf.
2. The leaf implements, tests, commits, and files the PR.
3. The reviewer checks the exact PR head; review comments are delivered to the
   owner, and the watcher emits the resulting review/CI events.
4. A NO-GO repair is composed and sent through `resume_pr`; a new owner,
   branch, or coordinator is not created.
5. `[PR READY]`, `[FIXES PUSHED]`, or policy-allowed `[REVIEW TIMEOUT]` plus
   passing CI permits merge of the reviewed head. `[STUCK]` and `[FAILED]`
   park the run and expose a human gate.

The inbox remains a human/worker delivery mechanism. Controller coordination
uses the durable ledger projection and run checkpoint, not inbox text or a
second message broker. See `.exo/review-policy.toml` for review limits,
timeouts, and complexity thresholds.

## Branch Naming

`{parent_branch}.{slug}-{type}` (dot separator, suffixed). The last dot-segment IS the `AgentName` — one namespace, zero translation. PRs target the parent branch, not main. Merged via recursive fold up the tree.

## State Machines

Agent lifecycle is tracked via `StateMachine` typeclass instances. Phase types live in role code (`.exo/roles/`). The framework handles persistence (KV), logging, and stop hook integration. Agents cannot exit during critical phases (e.g., `ChangesRequested`).

## Communication

- `notify_parent` for completion/failure/status updates to parent
- `send_message` for peer-to-peer messaging between any agents
- Messages arrive as native `<teammate-message>` via Teams inbox
- The controller remains live in its TL window and consumes the ledger; human
  operators answer named gates rather than steering an interactive TL
