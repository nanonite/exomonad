---
paths:
  - "**"
---

# Chainlink Issue Context

This file supplies Chainlink vocabulary for controller-dispatched `root` and
`tl` role processes. It is a reference, not an interactive coordinator
protocol. `tl_loop` is the programmatic controller: it owns the WorkPlan,
dispatch, event consumption, review gates, merge decisions, escalation, and
run termination through durable state under `.exo/tl-loop/`.

## Controller boundary

There is one controller per run. The `root` and `tl` roles remain RPC surfaces
for role-scoped agent work; they do not implement a second coordinator.

- Planning, dependency ordering, retries, harness selection, and budgets belong
  to the controller's policy and FSM.
- Review and CI observations come from the watcher and Forgejo. A reviewed
  head SHA, the closed adjudication verdict, repository policy, and CI state
  are required before the controller can authorize a merge.
- A bounded failure becomes a named durable gate. It is not resolved by a
  redispatch prompt or by ending the server.
- A child sub-TL is a nested `tl_run` represented in the WorkPlan, not a new
  interactive coordinator session.

See [the programming guide](../../../../docs/guides/programming-the-tl.md) and
[the TL-as-loop decision](../../../../docs/decisions/tl-as-loop.md) for the
authoritative controller contracts.

## Issue ownership and one-shot work

Every assigned issue has one owner, worktree, branch, and PR. A process handles
one assignment and publishes its authoritative result before exiting. The
issue ID belongs in the task specification and in the PR's acceptance
criteria.

- Leaves implement in issue-owned worktrees and file PRs.
- Reviewers judge the exact PR head and record an exact-SHA verdict.
- Watcher observations, Forgejo PR publication, review verdicts, and CI are
  authoritative. Inbox delivery, tmux injection, process exit, and local
  pushes are lifecycle signals only.
- A review repair resumes the existing owner with `resume_pr`; it does not
  create a sibling owner, replacement suffix, new branch, or stacked PR.
- The PR body must retain the literal `## Acceptance Criteria` heading with
  the issue's criteria on every `file_pr` update.

## WorkPlan authoring

When writing a root or child plan, use this order:

1. **ANTI-PATTERNS** — explicit prohibitions first.
2. **READ FIRST** — exact files, interfaces, and tests.
3. **STEPS** — bounded actions with named ownership paths.
4. **VERIFY** — exact commands and expected evidence.
5. **DONE CRITERIA** — observable completion conditions.

Each slice declares its paths, dependencies, base ref, test plan, and bounded
retry expectations. Harness, model, budget, and fallback choices are not
invented in the prompt; they come from `.exo/harness_policy.toml` and the
controller selector.

## Chainlink issue handling

Use the shared Chainlink database and issue IDs consistently:

- Create or shape the issue tree before dispatching work.
- Include the issue ID in every worker or leaf task description.
- Record discoveries and decisions as bounded issue comments.
- Express dependencies with issue blocks and keep the WorkPlan aligned with
  the issue tree.
- Close an issue only after its implementation, verification, review, and
  merge obligations are complete and the changelog update is committed.

Controller-dispatched slices keep their close authority in `tl_loop`. A role
process may close only work it directly owns under the local workflow.

## Tool boundaries

Role tools are scoped by the authority matrix in
`docs/architecture/agent-system.md`:

- `poll_workers` is a heartbeat observation, not an idle-loop completion
  protocol.
- `merge_pr` is callable only within the controller's verified merge gates; a
  role must not force a stalled or unreviewed PR through.
- `check_inbox` is for human, worker, and reviewer delivery where registered;
  it is not the controller's state machine.
- `shutdown_server` is not run termination. Terminal phases and named gates
  belong to the controller.
- `fork_wave` is not a dispatch primitive. Recursive TL work is a nested
  `tl_run` in the WorkPlan.

Do not create a duplicate coordinator, scrape tmux or peer messages for
authoritative state, or bypass the issue-owned repair path. Never touch another
agent's worktree or checkout another branch.
