# Agent Lifecycle Invariants

**Status:** Proposed

**Date:** 2026-05-19

**Chainlink:** #302, #303, #304, #305

## Context

Three orchestration smells observed against the running review loop:

1. **Orphan dev-leaves.** When a dev-leaf reports a blocker via `notify_parent` and the TL responds by re-decomposing (spawning a worker, closing the original issue), the leaf is left idle at its agent prompt holding a worktree, branch, and tmux window. Nothing connects the issue close to the leaf's lifecycle. Observed on backrooms-workspace window 3, dev-leaf `issue-97-textured-corridor-glb-codex` parked indefinitely after issue #97 was closed elsewhere.

2. **Issue closure on dirty trees.** Workers run in the TL's worktree as ephemeral panes. They modify files, call `chainlink session end`, and leave. Nothing forces the worker to commit, nothing forces the TL to commit on their behalf, and `chainlink_issue_close` does not check `git status`. Work is silently swept into a later unrelated commit, lost to `git restore`, or rots dirty.

3. **Missing worker provenance in git history.** Reviewer and dev-leaf identities appear in commit history (`exomonad-issue-NN-codex`, `exomonad-review-pr-N-codex`), but worker commits never have, because workers do not currently commit — they leak modifications into the parent worktree. When workers begin committing (per invariant 2 below) they must carry their own identity, mirroring the discipline already enforced for reviewers (see [reviewer-authorship-invariant](#related)).

## Decision

Three invariants govern when agents end, when issues can close, and how worker commits are attributed.

### Invariant 1 — Leaf lifecycle is bound to issue state

A dev-leaf's lifecycle ends when its assigned chainlink issue closes by any path other than the leaf's own PR merge. The TL keeps a leaf alive by keeping its issue open; the TL ends a leaf by closing its issue. Lifecycle correctness is structural, not a discipline rule the TL must remember.

**Mechanism.** A new `IssueClosed` world event fires from every chainlink close path (`merge_pr_local`'s post-merge close, the `chainlink_issue_close` MCP tool, the `dispose_leaf` wrapper). The dev-leaf's event handler matches on the leaf's assigned issue id, transitions its state machine to a terminal `DevDismissed` phase, injects a final `[ISSUE CLOSED]` message, and triggers a shared `dispose_agent_resources(project_dir, agent_slug)` helper extracted from the existing merge cleanup in `merge_pr_local.rs:303-331`. Worktree, tmux window, agent dir, and branch are released through the same code path that successful merges already use.

### Invariant 2 — Issues cannot close while the calling worktree is dirty

A chainlink issue may not be closed while uncommitted changes exist in the worktree of the calling agent. Strict and simple: `git status --porcelain` of the caller's CWD must be empty.

**Why CWD-of-caller is the natural "relevant worktree":**

| Caller | CWD | Naturally captures |
|--------|-----|--------------------|
| TL closing after a worker session | TL's worktree | Worker's dirty modifications (workers run in-place) |
| TL closing via `merge_pr` after dev-leaf PR merge | TL's worktree | Clean post-merge — leaf's worktree already absorbed and removed |
| `dispose_leaf` against an active leaf | Routes through the same close path | Inherits the precondition automatically |

No per-issue file provenance tracking. No diffing against session-start state. The agent that closes is the agent whose worktree is checked. Whoever invokes the close has the authority and the responsibility.

**Worker self-commit before session end.** Workers cannot reach `chainlink session end` while their CWD is dirty. The worker stop hook refuses session termination and instructs the worker to either commit under its own identity or use `discard_worker_output` (the named escape hatch for genuine throwaway output). This puts the commit decision at the agent that produced the change, which is also the agent with the right context to write a meaningful message.

**Escape hatches.**
- `discard_worker_output { reason: string }` — TL-only MCP tool, runs `git restore .` + `git clean -fd` in the TL's CWD, logs the reason, refuses on staged changes.
- `dispose_leaf { name, reason, force: bool }` — `force: true` permits dismissal of a leaf with a dirty worktree (for genuine-orphan recovery); the discarded file list is logged to chainlink as a session note for traceability.

### Invariant 3 — Worker commits carry worker identity

When a worker commits to fulfill invariant 2, the commit must be authored by the worker, not the TL whose worktree the worker happens to share. Worker identity is derived deterministically from the agent name (mirroring [agent-identity-model](#related)):

```
GIT_AUTHOR_NAME  = exomonad-{agent-name}
GIT_AUTHOR_EMAIL = {agent-name}@exomonad.local
GIT_COMMITTER_NAME / EMAIL = same
```

The worker spawn path in `rust/exomonad-core/src/services/agent_control/` injects these env vars when launching the worker process. Workers running in the TL's worktree pick up the env-var override at every `git commit`; the TL's own commits are unaffected because no env override is set on the TL process. This is the same provenance discipline already enforced for reviewers (see [reviewer-authorship-invariant](#related)) and dev-leaves (whose identity is set via worktree-scoped `.git/config`).

The git history audit `git log --author=exomonad-` should yield commits from every spawned agent type — workers, dev-leaves, and reviewers (reviewer commits only via merge attribution, never authored directly) — with one identity per agent.

## Session vs. issue lifecycle

Chainlink session lifecycle ≠ chainlink issue lifecycle.

- **`chainlink session work N`** — per-agent lock claim on issue N. Allowed for tl/dev/worker.
- **`chainlink session end`** — releases the per-agent lock. Allowed for tl/dev/worker. Refused by invariant 2 if the calling worktree is dirty.
- **`chainlink_issue_close N`** — closes the issue itself. TL/root only. Refused by invariant 2 if the calling worktree is dirty. Emits `IssueClosed` event triggering invariant 1.
- **`chainlink_subissue_close`** — dev-leaf authority over its own subissues. Same dirty-tree precondition applies.

When a dev-leaf ends its session (blocker, handoff), the issue remains open and assignable to another agent. Multiple sessions over an issue's lifetime are normal. The issue closes only when the coordinator decides the work is done.

## Out of scope

- **Parallel workers in one TL worktree.** Workers stay sequential and one-off. Parallelization is dev-leaf turf (own worktree per leaf, own branch, own PR). If multi-worker workflows become common, the stash-and-cherrypick dance is a future bridge.
- **Per-issue file provenance.** Tracking which files belong to which session over a session's lifespan adds complexity without a corresponding correctness gain.
- **Forcing the TL to commit before close.** The TL never has the "did the worker mean to commit this?" knowledge problem once invariant 2 holds — by the time the TL goes to close, the worker has already self-committed or self-discarded.
- **Bash-CLI close paths.** Closing issues from raw bash bypasses the WASM dispatch and the precondition. Leaves are never created from bash-CLI workflows, so this is acceptable: bash closes are operator escape hatches and the operator carries the responsibility.

## Related

- [agent-identity-model.md](agent-identity-model.md) — birth-branch as immutable identity; worker GIT_AUTHOR derivation follows the same pattern.
- [chainlink-agent-timer-lock-scope.md](chainlink-agent-timer-lock-scope.md) — chainlink locks are out of scope; ExoMonad's branch-as-identity is the coordination mechanism.
- [reviewer-authorship-invariant](../../CLAUDE.md) — chainlink #298–#301, reviewer must never author commits. Invariant 3 here is the worker-side analogue.
- [hylo-worktree-model.md](hylo-worktree-model.md) — the unfold/fold recursion that makes worktree cleanup a structural step rather than an ad-hoc one.

## Implementation tracking

| Chainlink | Priority | Covers |
|-----------|----------|--------|
| #302 | high | `IssueClosed` event, `DevDismissed` phase, shared `dispose_agent_resources` helper |
| #303 | medium (blocked by #302) | `dispose_leaf` MCP tool with `force` flag |
| #304 | high | Worker stop hook clean-tree check, `GIT_AUTHOR_*` env injection, `chainlink_issue_close` precondition, `discard_worker_output` escape hatch |
| #305 | low (deferred) | Composite E2E test exercising all three invariants — placeholder until E2E investment makes sense |
