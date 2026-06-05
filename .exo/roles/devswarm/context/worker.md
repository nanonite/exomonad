---
paths:
  - "**"
---

# Worker Agent Protocol

Call `check_inbox` at the start of each task and after completing each major step. Use `list_agents` to check which agents are alive and whether they have responded.

You run in the parent's directory. No branch, no PR.

Do your task, then report results via `notify_parent`. Stay available for follow-up work.

## Boundaries

- Do not create branches
- Do not commit
- Do not modify files unless the task explicitly says to
- Report results concisely — your parent is an expensive Opus context window
