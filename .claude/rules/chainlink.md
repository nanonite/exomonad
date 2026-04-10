---
paths:
  - "**"
---

# Planning Workflow: Chainlink + Exomonad

## Project Setup

```bash
chainlink init --no-hooks   # creates .chainlink/issues.db — no Claude Code hooks
exomonad new                # bootstrap .exo/config.toml, WASM, rules
exomonad init               # start tmux session: server window + TL window
```

`--no-hooks` is required. Without it, chainlink installs hooks that block all Write/Edit/Bash operations without an active issue — incompatible with the TL's scaffolding workflow.

## Planning Phase (before exomonad init)

Use chainlink to define the work before spawning agents:

```bash
chainlink milestone create "Milestone Name"   # one milestone per parallelizable wave
chainlink issue create "Issue title" --milestone "Milestone Name"
chainlink issue list                          # verify plan
chainlink milestone list                      # verify structure
```

## TL Scaffolding Protocol

When the TL session starts, read the chainlink state first:

```bash
chainlink milestone list
chainlink milestone show "<wave-1-milestone>"
```

Then:
1. **Scaffold** — write shared types/interfaces/stubs for the milestone's issues, commit + push
2. **Fork** — `fork_wave` or `spawn_gemini` once per issue (or group of non-conflicting issues)
3. **Converge** — merge PRs as children complete, integration commit
4. **Repeat** for the next milestone

## Context Handoff to Gemini Agents

Context-mode tools are Claude-only. Before spawning Gemini agents:

```
ctx_search(["relevant issue title", "key types", "scaffold commit summary"])
```

Put the compressed result in the `context` field of `spawn_gemini`. Gemini agents receive this as their full project context — make it complete enough that they don't need to ask.

Claude sub-TLs (fork_wave) inherit full context automatically via `--fork-session`.

## Chainlink in Child Agents

Gemini specs should include the chainlink issue ID so the agent can:
- `chainlink locks claim <issue_id>` before starting work
- `chainlink locks release <issue_id>` after filing PR
- `chainlink timer start <issue_id>` / `chainlink timer stop` for time tracking

This prevents two agents working the same issue and provides a coordination audit trail.
