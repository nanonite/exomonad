# Hylomorphism Worktree Model

**Status:** Accepted

## Decision

The agent tree is a git worktree tree. The `tl_loop` controller unfolds a
validated WorkPlan into issue-owned worktrees and branches. Authoritative
review, CI, and policy evidence then lets the controller fold completed work
back through PRs targeting their parent branches.

Every PR targets its parent branch, not main. The tree collapses via recursive merge, not flat integration.

## Branching

Dot-separated naming encodes the tree structure:

```
main
 └── main.feature
      ├── main.feature.auth
      │    ├── main.feature.auth.middleware
      │    └── main.feature.auth.tests
      └── main.feature.api
```

The parent of `main.feature.auth.middleware` is `main.feature.auth`. PRs always target the parent branch. This is derived mechanically from the branch name — no configuration or metadata needed.

## Unfold Phase (Spawn)

The controller decomposes work and dispatches children:

1. TL writes a spec commit (type stubs, interface definitions, failing tests)
2. `tl_loop` dispatches a `spawn_leaf` or `spawn_worker` slice
3. Server creates a git worktree at `.exo/worktrees/{slug}/`
4. Server creates a new branch `{parent_branch}.{slug}`
5. Server creates a tmux window for the child agent
6. Child agent works in its isolated worktree

Each child gets its own worktree — full filesystem isolation. No file-level conflicts between concurrent agents.

### Why Worktrees

- **Isolation**: Each agent has its own working directory. No merge conflicts during development.
- **Cheap**: Git worktrees share the object store. Creating one is instant.
- **Familiar**: Standard git operations work. No custom VCS layer.
- **Identity**: The worktree's branch name IS the agent's identity (see agent-identity-model).

## Fold Phase (Merge)

Work flows back up the tree via PRs:

1. Child completes work, pushes to its branch, files PR against parent branch
2. The watcher records Forgejo review and CI observations for the exact PR head
3. The controller applies its adjudication, policy, and reviewed-head gates
4. The controller calls `merge_pr` only after all gates pass
5. The controller records completion and advances the durable WorkPlan

### Merge Strategy

| Node Type | Strategy | Rationale |
|-----------|----------|-----------|
| Worker (Codex leaf) | Squash | Single logical change, clean history |
| Subtree (Claude) | Merge commit | Preserves child PR history |
| Root → main | Squash | Clean main branch history |

### Recursive Collapse

The tree collapses bottom-up:
1. Leaves merge into their parent branches
2. Intermediate nodes merge into THEIR parent branches
3. The root branch merges into main

Each level of the tree is one PR. The controller performs the merge cascade
from durable state; role processes do not run a second interactive merge queue.

## Implementation

- `spawn_leaf`: Creates an issue-owned worktree and branch for a dev assignment.
- `spawn_worker`: Creates an ephemeral same-worktree worker assignment.
- `file_pr`: Publishes the owner's PR against its validated base branch.
- `resume_pr`: Resumes the same owner, worktree, branch, and PR for repair.
- `merge_pr`: Merges a PR only after the controller's review, CI, policy, and
  reviewed-head gates pass.

## Consequences

- Forgejo and the immutable ledger are the audit trail for PR and review history
- The watcher records review and CI evidence; the controller owns adjudication
- Concurrent agents never conflict at the filesystem level
- Branch naming identifies ownership, while Chainlink and durable controller
  state record issue and lifecycle coordination
- Worktree cleanup follows the controller's owner and PR lifecycle
- Nested TL work is represented by nested `tl_run` plans rather than an
  interactive fork protocol

## Why Not Alternatives

**Shared directory (no worktrees)**: File-level conflicts between concurrent agents. Requires task-level coordination to avoid touching the same files.

**Docker containers**: Solves isolation but adds heavyweight infrastructure. Git worktrees achieve the same isolation with zero overhead.

**jj (Jujutsu)**: Previously used for automatic rebase cascade. Replaced with plain git worktrees — simpler, fewer edge cases with colocated mode, better tool ecosystem compatibility.

**Flat PRs to main**: Loses the tree structure. With nested PRs, each parent can review its children's work in context before folding up. Flat PRs would require the root TL to review everything.
