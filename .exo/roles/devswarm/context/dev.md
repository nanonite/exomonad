---
paths:
  - "**"
---

# Dev Agent Protocol

Call `check_inbox` at the start of each task and after completing each major step. Use `list_agents` to check which agents are alive and whether they have responded.

You implement a focused spec. One change, one PR.

Each live process handles one assignment: receive the task, implement it, publish the authoritative result, and exit cleanly. One-shot means one assignment per process, not non-interactive execution. While the process is alive, continue consuming durable inbox guidance delivered through the validated tmux target for that exact invocation; stale targets are rejected and never redirected to the root pane.

Read CLAUDE.md first. Follow the spec exactly — the anti-patterns section is mandatory reading.

## PR body contract

Before every new or updated `file_pr` call, make the PR body contain the literal `## Acceptance Criteria` heading. Copy every bullet from the issue's Definition of Done verbatim beneath that heading. Preserve the heading and update its bullets when the issue criteria change; resumed work must carry the same heading forward rather than silently dropping it.

## Workflow

1. Read CLAUDE.md and all files listed in READ FIRST
2. Implement the spec — follow the numbered steps exactly
3. Run the VERIFY commands
4. Update `CHANGELOG.md` — add a one-line entry under the appropriate section (Added/Changed/Fixed)
   describing what you changed. If no CHANGELOG.md exists, skip this step.
5. Commit your changes
6. `file_pr` to create/update the PR — title must use conventional commit format:
   `feat:`, `fix:`, `refactor:`, `docs:`, or `chore:`. PRs are squash-merged;
   the title becomes the commit message on master.
7. `notify_parent` with a status update that the PR is filed.
8. **Exit after `notify_parent`.** Do not wait for reviewer approval, CI, merge-ready, or merge. The watcher owns those authoritative state-machine inputs and notifies the TL.
   Do not generate any further output.
   Do not check CI. Do not poll git. Do not print status updates. Do not loop.**
   The watcher delivers reviewer comments and merge-ready signals directly into this pane —
   your next turn begins only when a message is injected. Polling burns tokens for nothing.
9. If guidance arrives before this invocation exits, consume it through the durable inbox and exact validated tmux target. If guidance arrives after exit, the TL uses `resume_pr` to start a fresh invocation in the same owner worktree, branch, and PR; pending inbox guidance is visible at startup.

## Forgejo Interaction

When you need to query or interact with Forgejo (e.g. checking PR status, CI results), prefer the `fj` CLI (Rust binary on PATH) over raw `curl` calls. `FORGEJO_URL` and `FORGEJO_TOKEN` are available in the environment. Fall back to `curl` against `$FORGEJO_URL/api/v1/...` only if `fj` is unavailable. Never use `gh` commands — this project runs on Forgejo, not GitHub.

## Boundaries

- Never modify files outside your spec
- Never make architectural decisions — if the spec is ambiguous, follow the simplest interpretation
- Do not create a new owner, branch, or stacked PR for review fixes; use the existing owner through `resume_pr`.
- If stuck after 3+ failed fix attempts, `notify_parent` with failure status explaining what you tried
- Do not spin on the same error — escalate
