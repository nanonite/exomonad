---
paths:
  - "**"
---

# Dev Agent Protocol

You implement a focused spec. One change, one PR.

Read CLAUDE.md first. Follow the spec exactly — the anti-patterns section is mandatory reading.

## Workflow

1. Read CLAUDE.md and all files listed in READ FIRST
2. Implement the spec — follow the numbered steps exactly
3. Run the VERIFY commands
4. Commit your changes
5. `file_pr` to create/update the PR
6. `notify_parent` with a status update that the PR is filed and awaiting review
7. **IDLE: After `notify_parent`, STOP. End your turn. Do not generate any further output.
   Do not check CI. Do not poll git. Do not print status updates. Do not loop.**
   The watcher delivers reviewer comments and merge-ready signals directly into this pane —
   your next turn begins only when a message is injected. Polling burns tokens for nothing.
8. When a message arrives: act on it (fix review comments, push, re-run verify). Then STOP again.
9. Stop only after the watcher injects `[MERGE READY]`; the parent TL merges after that.

## Boundaries

- Never modify files outside your spec
- Never make architectural decisions — if the spec is ambiguous, follow the simplest interpretation
- If stuck after 3+ failed fix attempts, `notify_parent` with failure status explaining what you tried
- Do not spin on the same error — escalate
