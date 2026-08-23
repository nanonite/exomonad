---
description: "ExoMonad agent orchestration rules — loaded into every agent's context in projects using exomonad"
---

# ExoMonad Agent Rules

## MCP Tools

Use exomonad MCP tools for orchestration. Git operations use `git` CLI. **Never use `gh pr create`** — use the `file_pr` MCP tool instead (works with or without a GitHub remote).

**Never run `exomonad init`, `exomonad serve`, or `exomonad new`** — the server is already running. Those commands manage the session itself and will kill the current session including yourself.

| Tool | Role | What it does |
|------|------|-------------|
| `fork_wave` | root, tl | Fork N parallel agents (Claude, Codex, or OpenCode; own worktrees, context inheritance is runtime-specific) |
| `spawn_leaf` | root, tl | Spawn a leaf agent in its own worktree+branch (files PR when done). Agent type defaults to server config; pass `agent_type` only when this leaf needs a specific supported runtime. |
| `spawn_worker` | root, tl | Spawn an ephemeral worker in a tmux pane (no branch, no PR). Agent type defaults to server config; pass `agent_type` only when this worker needs a specific supported runtime. |
| `file_pr` | tl, dev | Create/update PR (base branch auto-detected from branch naming) through the configured Forgejo API. |
| `merge_pr` | root, tl | Merge a child's PR |
| `notify_parent` | tl, dev, worker | Send message to parent agent |
| `send_tmux_message` / `send_mailbox_message` | all | Send message to any exomonad-spawned agent |
| `poll_workers` | root, tl | Snapshot spawned agent pane liveness, Chainlink session state, issue status, and age before idling |
| `task_list` | dev, worker | List tasks from the shared task list |
| `task_get` | dev, worker | Get a task by ID |
| `task_update` | dev, worker | Update task status, owner, or activeForm |

## PR Status (Forgejo)

PRs are tracked in Forgejo. Do NOT use `gh` commands — they will fail. The worktree event watcher reads Forgejo PR/review/CI state, automatically spawns a reviewer, and delivers `[PR READY]` / `[FIXES PUSHED]` / `[MERGE READY]` notifications. You do not need to poll PR status manually.

## Agent Hierarchy

- **TL controller**: The Python `tl_loop` process. Loads structured plans, selects bounded harnesses, dispatches work, consumes the ledger, applies review gates, merges, and parks or escalates runs. It never edits source code.
- **Dev (Leaf)**: Configured agent type (OpenCode, Codex, or Shoal — set via `--worker` flag at init). Implements a focused spec, files PR via `file_pr` MCP. No spawning.
- **Worker**: Ephemeral pane agent. Research or non-conflicting in-place edits. No branch, no PR.
- **Reviewer**: The configured review harness. Reviews the issue-owned PR head and never authors its branch.
- **Human operator**: Observes the controller window and answers explicitly named durable gates.

## Harness and effort selection

`exomonad init --tl`, `--worker`, and `--reviewer` select role harnesses; the
same fields are available in `.exo/config.toml` and `[reviewer]`. Effort resolves
as CLI > local config > global config > medium default. Worker effort is inherited
by forked TLs, leaves, ephemeral workers, and companions. OpenCode receives effort
as a model-aware `--variant`; Codex and Shoal log that effort is ignored.

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

A dev invocation that exits without an authoritative completion, notify-parent,
or PR handoff is never treated as successful, including exit code 0. The
heartbeat records the invocation and Git evidence, preserves the worktree, and
parks a `missing_handoff` blocker behind a durable human gate. A later matching
ledger handoff wins over pane death; repeated observations do not emit another
blocker.

Externally blocked leaves without a PR use `resume_blocked_leaf` only after the
operator approves the named durable gate. The request must carry the exact
dormant invocation, branch, and dirty-worktree fingerprint; the host resolves
one persisted owner and starts a fresh invocation in that same worktree. Never
invent a sibling identity, infer approval from a message, or discard dirty
state.

### 4. Merge, park, or gate

An approved current head with passing CI is mergeable. A timeout is never an
approval: the controller parks at the durable `tl-timeout` gate. Retry,
parallelism, recursion, budget, and review-round ceilings are explicit.
Exhaustion parks the run with
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

### Closed-PR replacement recovery

When a human approves replacing a closed, unmerged PR, use the root/TL-only
`replace_close_pr` MCP command. Provide the still-open Chainlink issue id, the
closed PR number, the exact old author leaf identity, a fresh bare leaf slug,
the complete replacement task, and `human_approved: true`. The command keeps
the old PR branch and exact head SHA as recoverable source, targets the old PR
base branch for the new PR, clears old watcher/reviewer state, and retires the
old leaf's tmux identity, resolver record, config, and worktree.

Do not use it for an open PR (`restart_review` is the same-PR path), a merged PR,
or a new Chainlink issue. If it reports cleanup or spawn failure, preserve the
returned source SHA and retry the same replacement request; the durable record
under `.exo/replacements/` makes retries idempotent and prevents duplicate
fresh leaves.

## Branch Naming

`{parent_branch}.{slug}` (dot separator). PRs target the parent branch, not main. Merged via recursive fold up the tree.

## Communication

- `notify_parent` for completion/failure/status updates to parent
- `send_tmux_message` / `send_mailbox_message` for peer-to-peer messaging between any agents
- Messages arrive as native `<teammate-message>` via Teams inbox
- The controller remains live in its TL window and consumes the ledger; human
  operators answer named gates rather than steering an interactive TL
