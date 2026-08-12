---
paths:
  - "**"
---

# Root / TL Role Context

This file is the instruction context for an agent spawned under the `root` or
`tl` role. It is **not** a coordinator protocol.

Run-level orchestration belongs to `tl_loop`, the programmatic TL controller.
It owns planning, dispatch, harness selection, budgets, review gates, merge
decisions, escalation, and run termination, and it drives them from durable
state under `.exo/tl-loop/<run_id>/run.json`. There is one controller per run.

What follows is the tool surface and the boundaries you operate under.

## You are not the coordinator

Do not do any of these. They belong to the controller or no longer exist:

- **No idle loop.** Do not poll `check_inbox` on a counter, do not track
  consecutive empty results, and do not call `shutdown_server` to end a run.
  The controller's terminal phase predicates in `tl_loop/fsm/terminal.py` are
  the sole completion mechanism.
- **No plan/fork/merge/repeat cycle.** Waves, dependency ordering, and
  recursion are the controller's `WorkPlan` and FSM. A child sub-TL is a nested
  `tl_run`, not another agent session.
- **No merge queue.** The controller decides what merges, after four
  independent gates: current-head reviewer approval and CI
  `success`/`neutral` observations, a closed adjudication verdict, the
  `.exo/review-policy.toml` policy veto, and reviewed-head SHA binding. The
  watcher reports observations; it does not authorize merging. Do not diagnose
  a stalled PR by hand and call `merge_pr` without those gates.
- **No run termination.** A bounded failure parks with an auditable cause and
  waits for a named human gate answered with
  `python3 -m tl_loop gate --run-id <id> --name <gate> --approve|--reject`.

See [docs/guides/programming-the-tl.md](../../../../docs/guides/programming-the-tl.md)
for how a run is actually programmed, and
[docs/decisions/tl-as-loop.md](../../../../docs/decisions/tl-as-loop.md) for why
the boundary sits here.

## Hard boundaries

The `root` and `tl` roles have a PreToolUse guard that denies `Edit`, `Write`,
`MultiEdit`, and `NotebookEdit`. The denial text is the redispatch nudge:
follow it by steering an existing worker with `send_tmux_message`, letting the
leaf handle reviewer feedback, or spawning a new `spawn_leaf` / `spawn_worker`.

Never touch another agent's worktree. Never checkout another branch.

Never run `exomonad init`, `exomonad serve`, or `exomonad new` — the server is
already running, and `init` kills the current session including yourself.

The continuation brief is injected automatically into the SessionStart context
after the TeamCreate instruction. Do not call `continuation_brief` manually at
startup; use it only for a mid-session refresh.

## Fixing an Existing PR's CI/Review Problem

- **DO NOT** call `spawn_leaf` with a new, unrelated `name` to fix another PR's CI failure, review comments, or merge conflicts. A new name always creates a disconnected sibling branch from the caller's branch, often targeting `main`, rather than continuing the target PR.
- **DO** first call `watcher_pr_state` for the PR number and confirm it is open, unmerged, and has a head branch and SHA.
- **DO** read the review, diagnose the root cause, propose the solution, and call `resume_pr` with a complete structured repair handoff. The host re-fetches the head SHA, resolves exactly one persisted owner, and resumes its existing worktree.
- The handoff must include ROOT CAUSE, PROPOSED SOLUTION, READ FIRST, STEPS, VERIFY, BOUNDARY, and DONE CRITERIA. Preserve the owning Chainlink issue; do not create or close one during review repair.
- **DO NOT** pass a leaf name, branch name, agent type, or invented suffix. The host owns identity resolution and rejects stale, duplicate, ambiguous, or mismatched metadata.
- If the PR is closed or cannot be safely recovered, use `replace_close_pr` only with explicit human approval and reconcile the superseded PR. Never create an automatic `-2` branch.

The dev process is one-shot per assignment: after publishing its PR and handoff
it exits, while the watcher remains authoritative for review and CI. `resume_pr`
starts a fresh invocation in the same owner worktree, branch, and PR when more
work is needed; pending inbox guidance is shown at startup.

## Worker Correction Loop

Workers are ephemeral pane agents with no PR. When a worker reports a blocker via `notify_parent`:

1. **Assess**: Can you resolve the blocker with a clarification or a narrower spec? If yes:
   - Use `send_tmux_message` with `to: worker-name` to inject the correction directly into the worker's pane.
   - The worker is still running and will receive the message.
   - Wait for the worker's follow-up `notify_parent`.

2. **Escalate to human**: If you cannot resolve the blocker alone (missing domain knowledge, ambiguous requirement, external dependency):
   - Surface the issue clearly in your response so the human operator can see it.
   - Tell the human: what the worker tried, what failed, and what clarification is needed.
   - Once the human provides clarification, relay it to the worker via `send_tmux_message`.

3. **Re-spec**: If the original task was fundamentally mis-scoped:
   - Close the stuck worker (it will idle until the session ends).
   - Spawn a new worker with a corrected spec.
   - If you want to end a leaf and reuse its slot, call `dispose_leaf`. If you want to keep the leaf alive and unblock it, use `send_tmux_message`.

**Never wait silently** for a stuck worker. Either steer it, escalate to the human, or re-spec.

### Harness and no-op guardrails

Keep coding retries and respawns on the configured worker harness. A
[STUCK: harness-switch] event means the configured harness could not
proceed; surface it to the human and request guidance instead of
unilaterally selecting another harness. resume_pr is the same-owner repair
path and preserves the persisted harness, model, effort, worktree, branch, and
PR. An explicit cross-harness coding request is permitted only with the human
approval flag EXOMONAD_ALLOW_HARNESS_SWITCH=1 and is audited.

No-commit, no-PR, and no-op failure handoffs emit durable agent.stuck
guidance. Retry the same harness with a narrower task or escalate the exact
failure to the human; do not silently replace the worker.

Harness and model selection for controller-dispatched work is not yours to
make. The selector reads the human-authored allowlist in
`.exo/harness_policy.toml` and cannot widen it or raise a ceiling.

## Manual Orphan Leaf Cleanup

Use `cleanup_leaf` when a dead dev leaf needs on-demand disposal and the normal
Chainlink close/reconciler path is not the right trigger. Pass `name` for one
leaf, or `sweep=true` to inspect every orphan worktree; use `dry_run=true`
first when the target set is uncertain. The host performs the safety checks
itself: tmux must be dead, the worktree must be clean, exactly one PR must
match its branch, and that PR must be merged or closed-unmerged. Dirty,
open, missing, or ambiguous targets are reported and left in place. This tool
shares the existing resource-disposal implementation with `cleanup_orphan`;
it does not force cleanup, close PRs, or replace the automatic reconciler.

## Chainlink Coordination

Issue shaping and observation are available to this role. Merge decisions and
final close authority for controller-dispatched slices belong to `tl_loop`.

- Use `chainlink_issue_create` and `chainlink_subissue_create` to shape work before spawning.
- Prefer dev leaves for work that needs PR review, CI, or non-trivial implementation.
- Use same-worktree workers only for narrow subissues where direct commits to the parent worktree are acceptable.
- Use `chainlink_timer_start` with the assigned issue id when assigning/spawning owned work.
- Use `chainlink_timer_stop` with the same issue id after review, CI, and merge are complete. Timer stop is explicit per issue; do not infer a global active timer.
- Use `chainlink_session_status` to observe whether child agents have started, attached to an issue, or ended with handoff notes.
- Use `chainlink_issue_close` only for work you own directly, and only after merge, verification, and the implementing agent's session end are complete.
- After closing a worker-owned Chainlink issue, stage and commit `CHANGELOG.md` in your worktree before spawning the next wave. Worker changes are already committed in-place; the issue close is what dirties the changelog.
- Treat Chainlink `review-stuck` issues as human-clarification inputs. Do not automatically close, respawn, or replace the dev leaf that owns the PR worktree.

The TL loop classifies raw timeout, stuck, and CI-blocked review observations as
`dev_not_pushing`, `reviewer_not_responding`, `reviewer_never_started`, or
`ci_failed` and persists that classification with the matching PR head. The
watcher only supplies the evidence; do not branch on an unverified producer
classification.

Do not use Chainlink agent, sync, or lock commands. Do not ask workers or dev leaves to close their own assigned issue.

Always pass the resolved absolute Chainlink db path to workers and leaves; they must not discover it themselves. Include `CHAINLINK_DB=/absolute/project/root/.chainlink/issues.db` in every task spec that references Chainlink, resolving the path from the current project root before spawning. Workers read `$CHAINLINK_DB` or the explicit path; they do not enumerate for it.

## Cost Model

Your tokens cost 10-30x a leaf's. Every file read for implementation detail,
every line of code you write, is wasted budget. Decompose, spec, spawn — that's
it.

## Spec Template

1. ANTI-PATTERNS — known failure modes as explicit DO NOT rules (FIRST)
2. READ FIRST — exact files to read (CLAUDE.md, source files)
3. STEPS — numbered, each step = one concrete action with code snippets
4. VERIFY — exact build/test commands
5. DONE CRITERIA — what "done" looks like
