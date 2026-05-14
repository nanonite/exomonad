# Chainlink Timer Scope

Status: accepted

Date: 2026-05-14

Chainlink: #196 (supersedes investigation #178)

## Context

ExoMonad uses git worktree branch names as agent birth identity. The branch name encodes parentage, PR base detection, and the coordination boundary between TLs, dev workers, and reviewers.

Chainlink also has a lock subsystem for multi-agent coordination. Local investigation found that `chainlink locks list --json` creates a helper git worktree at `.chainlink/.locks-cache` on the fixed branch `chainlink/locks`. That branch is internal Chainlink state, but it appears in `git worktree list` beside ExoMonad agent worktrees.

This conflicts with ExoMonad's branch-as-agent-identity model.

## Decision

Do not integrate Chainlink locks or Chainlink agent commands into the ExoMonad spawned-task MCP workflow.

ExoMonad's git birth-branch/worktree model is the coordination and identity mechanism. Adding Chainlink locks around spawned work would duplicate coordination, introduce a non-agent branch into worktree discovery, and risk confusing identity and PR topology.

Chainlink agent commands are also out of scope for ExoMonad MCP integration. `chainlink agent init` creates local identity state, and `chainlink agent status` reaches into lock state. That creates another identity layer that conflicts with ExoMonad's branch-derived agent identity without adding required functionality.

Chainlink lock support can be reconsidered only if Chainlink changes its lock storage semantics so it does not create or require a git worktree/branch in the project repository.

## Timer

Chainlink timer support remains useful if it is independent of locks.

Source review of Chainlink shows:

- `chainlink timer start <issue_id>` verifies that the issue exists, checks for an existing active timer, then inserts into `time_entries`.
- `chainlink timer stop` closes the active row in `time_entries` and computes `duration_seconds`.
- The timer path does not call the lock backend.

Do not attach ExoMonad timer integration to `chainlink session work`. Timer is coordinator-owned lifecycle tracking; session work is work-state telemetry from the implementing agent.

The preferred ExoMonad timer lifecycle is TL-owned and task-level:

- start when a TL assigns/spawns work for a Chainlink issue
- stop when the corresponding worker PR is approved and merged back through the TL flow

Expose timer MCP support only to the TL role. Dev workers, leaf workers, and reviewers should not receive timer tools, because they do not own the full task lifecycle. The TL is the actor that can observe both ends of the interval: task assignment/spawn and reviewed merge.

If future Chainlink timer changes make timer start/stop depend on locks, timer integration is a no-go.

## Session Semantics

Session commands are work telemetry, not completion authority.

- `chainlink_session_start`: the agent has begun a Chainlink work session.
- `chainlink_session_work <issue_id>`: the active session is now attached to a specific issue/subissue.
- `chainlink_session_end`: the agent is done with its active assignment and has left handoff notes.
- `chainlink_session_status`: read-only coordinator visibility into active/ended session state.

`chainlink session work` currently calls Chainlink's lock check. Empirical and source review show it does not create `.chainlink/.locks-cache` when `.chainlink/agent.json` is absent, but it does create the lock worktree after `chainlink agent init`. ExoMonad therefore does not expose Chainlink agent commands. The intended Chainlink-side semantics are DB-backed issue occupancy checks, where a second active session on the same issue is denied without using git locks.

## Role MCP Surface

### Root / Main TL

The main TL creates, decomposes, assigns, observes, times, and closes work. It normally does not call `session_start`, `session_work`, or `session_end` for itself; it uses `session_status` to observe descendants.

Keep:

- issue creation/list/show/comment/update
- subissue creation
- block/relate/cascade
- milestone create/list
- session status
- issue close
- timer start/stop/status

Drop:

- Chainlink agent commands
- Chainlink sync/lock commands
- lock-backed worker status

### SubTL

SubTL uses the same TL role. It is both an assignee relative to its parent and a coordinator for descendants.

Keep the TL surface, with these semantics:

- may call `session_start`, `session_work`, and `session_end` for its assigned parent issue so its parent can observe progress
- uses `session_status` to supervise child dev leaves/workers
- creates subissues under its assigned parent issue
- uses timer tools for work it coordinates
- closes only issues it owns as coordinator, after review/CI/merge conditions are satisfied

### Dev Leaf

Dev leaves implement assigned issues on PR branches. They report their own progress through session commands but do not close their assigned issue. They may decompose narrow worker-scoped subissues inside their worktree.

Keep:

- session start/work/end
- session status for child worker visibility
- issue show/comment
- subissue create
- subissue close for worker-scoped child subissues after reviewing worker handoff

Drop:

- issue close for the dev leaf's own assigned issue
- top-level issue create/list/update/block/relate/cascade/milestones
- timer tools
- Chainlink agent/sync/lock tools

### Worker

Workers execute one narrow assigned subissue in the parent worktree. They do not decompose and do not close issues.

Keep:

- session start/work/end
- issue show/comment

Drop:

- issue close
- subissue close/create
- issue list/update/block/relate/cascade/milestones
- session status
- timer tools
- Chainlink agent/sync/lock tools

### Reviewer

Reviewers should not mutate Chainlink state. Review state flows through PR review tools.

## Agent Identity Findings

`chainlink agent init <id>` writes machine-local `.chainlink/agent.json`; it does not write the shared sqlite issue database. `chainlink session start` reads that file and stores the current agent id into `sessions.agent_id`.

That could help correlate Chainlink sessions with ExoMonad agent identities in a generic Chainlink workflow, but ExoMonad already has stronger identity primitives. The current `chainlink agent status` command also initializes/fetches lock state to display held locks.

For ExoMonad, do not expose Chainlink agent commands through MCP. Audit correlation should use the existing ExoMonad identity chain:

- birth branch
- tmux/session id
- local PR registry entry
- reviewer result
- merge event

Any future reconsideration of Chainlink agent support must avoid lock semantics, avoid `chainlink agent status` as currently implemented, and prove it adds value over branch-derived identity.

## Consequences

- Remove lock claim/release from pending ExoMonad Chainlink MCP integration plans.
- Remove Chainlink agent command MCP integration plans.
- Remove lock assertions from E2E plans for Chainlink agent audit.
- Timer MCP work should call timer commands directly, not via `chainlink_session_work`.
- Timer MCP tools should be TL-only.
- `chainlink_issue_close` must run only `chainlink close`; it must not release locks, end sessions, or notify parents.
- `chainlink_subissue_close` is separate coordinator authority for dev leaves to close worker-scoped child subissues.

## Related Code

- Chainlink timer: `/home/goya/agent-workspace/chainlink/chainlink/src/commands/timer.rs`
- Chainlink time entries: `/home/goya/agent-workspace/chainlink/chainlink/src/db/time_entries.rs`
- Chainlink agent identity: `/home/goya/agent-workspace/chainlink/chainlink/src/identity.rs`
- Chainlink agent status: `/home/goya/agent-workspace/chainlink/chainlink/src/commands/agent.rs`
- Chainlink session work lock check: `/home/goya/agent-workspace/chainlink/chainlink/src/commands/session.rs`
- ExoMonad worktree model: `docs/decisions/hylo-worktree-model.md`
