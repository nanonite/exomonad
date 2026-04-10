---
paths:
  - "**"
---

# Chainlink: Planning State

Chainlink is the issue/milestone tracker for this project. Before scaffolding, read the planning state to understand what needs to be built and how work is decomposed.

## Reading the Plan

```bash
# All open issues
chainlink issue list

# Milestones (each milestone = one parallelizable work unit)
chainlink milestone list

# Issues under a specific milestone
chainlink milestone show "<name>"

# Full detail on a specific issue
chainlink issue show <id>
```

## Mapping Chainlink State to fork_wave

Each **milestone** maps to one wave of parallel work. Issues within a milestone that have no dependencies on each other are candidates for simultaneous forking.

Pattern:
1. `chainlink milestone list` → identify milestones as wave boundaries
2. `chainlink milestone show "<name>"` → list issues in that milestone
3. Check issue dependencies: `chainlink issue show <id>` for `blocked_by` fields
4. Issues with no blockers in the same milestone → fork in parallel
5. Issues blocked by others → next wave after blockers resolve

## Coordination for Child Agents

Child agents (dev/worker) can use chainlink for coordination:

- `chainlink locks claim <issue_id>` — claim an issue before starting (prevents duplicate work)
- `chainlink locks release <issue_id>` — release when done or handing off
- `chainlink locks list` — see which agents hold which issues
- `chainlink timer start <issue_id>` / `chainlink timer stop` — time tracking per issue
- `chainlink sync` — sync lock state from remote coordination branch

Include the relevant `issue_id` in Gemini specs so leaf agents can claim locks.

## Convention

- One milestone = one `fork_wave` call (or one `spawn_gemini` wave)
- One issue = one leaf agent's scope
- The scaffold commit covers the interfaces all issues build against
- After all issues in a milestone merge, close the milestone: `chainlink milestone close "<name>"`
