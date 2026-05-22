---
description: "ExoMonad agent orchestration rules — loaded into every agent's context in projects using exomonad"
---

# ExoMonad Agent Rules

## MCP Tools

Use exomonad MCP tools for orchestration. Git operations use `git` CLI. **Never use `gh pr create`** — use the `file_pr` MCP tool instead (works with or without a GitHub remote).

**Never run `exomonad init`, `exomonad serve`, or `exomonad new`** — the server is already running. Those commands manage the session itself and will kill the current session including yourself.

| Tool | Role | What it does |
|------|------|-------------|
| `fork_wave` | root, tl | Fork N parallel Claude agents (own worktrees, context inherited by default via `fork_session`) |
| `spawn_leaf` | root, tl | Spawn a leaf agent in its own worktree+branch (files PR when done). Agent type defaults to server config; pass `agent_type` only when this leaf needs a specific supported runtime. |
| `spawn_worker` | root, tl | Spawn an ephemeral worker in a tmux pane (no branch, no PR). Agent type defaults to server config; pass `agent_type` only when this worker needs a specific supported runtime. |
| `file_pr` | tl, dev | Create/update PR (base branch auto-detected from branch naming) through the configured Forgejo API. |
| `merge_pr` | root, tl | Merge a child's PR |
| `notify_parent` | tl, dev, worker | Send message to parent agent |
| `send_message` | all | Send message to any exomonad-spawned agent |
| `task_list` | dev, worker | List tasks from the shared task list |
| `task_get` | dev, worker | Get a task by ID |
| `task_update` | dev, worker | Update task status, owner, or activeForm |

## PR Status (Forgejo)

PRs are tracked in Forgejo. Do NOT use `gh` commands — they will fail. The worktree event watcher reads Forgejo PR/review/CI state, automatically spawns a reviewer, and delivers `[PR READY]` / `[FIXES PUSHED]` / `[MERGE READY]` notifications. You do not need to poll PR status manually.

## Agent Hierarchy

- **TL (Tech Lead)**: Claude. Decomposes, specs, scaffolds, spawns, merges. Never implements directly.
- **Dev (Leaf)**: Configured agent type (OpenCode, Gemini, etc. — set via `--worker` flag at init). Implements a focused spec, files PR via `file_pr` MCP. No spawning.
- **Worker**: Ephemeral pane agent. Research or non-conflicting in-place edits. No branch, no PR.

## The TL Protocol: Scaffold-Fork-Converge

Every TL at every level of the tree follows this protocol:

### 1. Scaffold

Before spawning any children, commit the shared foundation they'll build against:

- **Types and interfaces** that children implement
- **Test harness and fixtures** children will use
- **Stub files** showing where children put their code
- **CLAUDE.md additions** scoping this TL's domain

Commit and push. Children fork from this commit.

### 2. Fork (spawn wave)

Spawn children for wave N. Zero dependencies between siblings in the same wave.

- **Sub-TLs**: `fork_wave` (Claude). They inherit full conversation context.
- **Devs**: `spawn_leaf`. They get a self-contained spec. The CLAUDE.md from the scaffolding commit gives them project context.

### 3. Converge (merge wave)

Wait for children to complete (notifications arrive via Teams inbox). Merge their PRs sequentially. Then write an **integration commit**:

- Wire children's outputs together
- Run integration tests
- Fix integration bugs

### 4. Next wave (if any)

Wave N+1 depends on merged wave N. Repeat from step 2.

### 5. PR to parent

After all waves are merged and integrated, file a PR to the parent TL's branch.

## Spec Quality

Specs are self-contained — the leaf has no context from previous attempts. Every spec must include:

1. **Anti-patterns** (FIRST) — known failure modes as explicit DO NOT rules
2. **Read first** — exact files to read (CLAUDE.md, source files)
3. **Steps** — numbered, each step = one concrete action with code snippets
4. **Verify** — exact build/test commands
5. **Done criteria** — what "done" looks like

Include complete code snippets. Name every file by full path. Include exact commands, not "run the tests."

## Convergence Protocol

The TL does NOT iterate on children's work. Convergence is **leaf + reviewer**, not TL:

1. Leaf implements spec, commits, files PR via `file_pr` MCP
2. Reviewer agent reviews automatically on PR creation
3. If reviewer requests changes → injected into leaf's pane → leaf fixes → pushes
4. System notifies parent: `[FIXES PUSHED]`, `[PR READY]`, `[MERGE READY]`, `[REVIEW TIMEOUT]`, or `[STUCK]`
5. TL merges on `[MERGE READY]`. On `[STUCK]`, ask the human for clarification; the leaf remains alive in its PR worktree.

The TL never manually reviews code, never fixes a leaf's implementation.
See `.exo/review-policy.toml` for review round limits, timeouts, and complexity thresholds.

## Branch Naming

`{parent_branch}.{slug}` (dot separator). PRs target the parent branch, not main. Merged via recursive fold up the tree.

## Communication

- `notify_parent` for completion/failure/status updates to parent
- `send_message` for peer-to-peer messaging between any agents
- Messages arrive as native `<teammate-message>` via Teams inbox
- TL idles between spawning and receiving notifications — no polling
