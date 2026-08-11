---
paths:
  - "**"
---

# TL Decompose-Prompt Reference

This file is retained as prompt vocabulary for authoring and reviewing a
structured TL `WorkPlan`. It is not the TL protocol and is not an instruction
to start an interactive coordinator. The programmatic controller in
`tl_loop/` owns planning, dispatch, event consumption, review, merge, and
terminal decisions.

## Hylomorphic vocabulary

The hylomorphism remains useful language for the controller: a validated plan
and checkpoint describe the unfold, while ledger-driven review, merge, and
upward PR transitions describe the fold. The controller performs both through
durable state; this reference does not direct an agent to scaffold, fork,
idle, poll, or fold manually.

## WorkPlan authoring

Use this order when writing a root or child plan:

1. **ANTI-PATTERNS** — explicit DO NOT rules first.
2. **READ FIRST** — exact files, interfaces, and existing tests.
3. **STEPS** — bounded actions with named ownership paths.
4. **VERIFY** — exact commands and expected evidence.
5. **DONE CRITERIA** — observable completion conditions.

Each slice should declare its paths, dependencies, base ref, test plan, and
bounded retry/parallelism expectations. Harness, model, budget, and hidden
fallback choices belong to the controller's policy and selector, not to the
natural-language prompt.

## Ownership boundary

Leaves implement in issue-owned worktrees and file PRs. Reviewers judge the
exact PR head. The controller consumes authoritative ledger events and sends
repair work through `resume_pr`. Human decisions are named durable gates.

Do not use `check_inbox`, `list_agents`, `poll_workers`, tmux scraping, or
peer-message text as the controller's state machine. Do not create a second
coordinator, a duplicate branch, or a silent fallback to an interactive TL.
