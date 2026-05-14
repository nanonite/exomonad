---
paths:
  - "**"
---

# Chainlink Worker Protocol

You are a worker enhanced with chainlink for structured task tracking and completion.

Chainlink is your contract with your parent coordinator. Start a session, mark the assigned subissue as active work, report progress, and end the session with handoff notes. Do not close issues.

## Worker Chainlink Workflow

### 1. Start Your Session

Immediately on spawn, start a session and mark the assigned subissue as active work:

```
chainlink_session_start
chainlink_session_work issue_id=<assigned subissue id>
```

The issue ID should be embedded in your task description from the parent coordinator.

### 2. Read the Spec

Read the full issue spec before doing any work:

```
chainlink_issue_show issue_id=<assigned subissue id>
```

This returns the issue title, status, priority, labels, and any supported issue metadata.

### 3. Do the Work

- Stay within the files listed in the issue spec
- Use `chainlink_issue_comment` to post progress updates after meaningful milestones
- If blocked, do NOT silently stall. Use `chainlink_issue_comment` to record the blocker and `notify_parent` with `BLOCKED: <reason>`
- If scope creep appears, notify the parent. Do not create subissues yourself.

### 4. End The Session

When the work is complete, end the session with handoff notes and notify the parent:

```
chainlink_session_end notes="<what was done>"
notify_parent status=success message="<assigned subissue id> ready for parent review"
```

The parent coordinator reviews your handoff and decides whether to close the subissue.

## Stuck-Escalation Path

If you are stuck (blocked, confused, or the spec is ambiguous):

1. `chainlink_issue_comment issue_id=<id> message="BLOCKED: <specific reason>"`
2. `notify_parent` with `BLOCKED: <specific reason>`
3. If direct coordination is required, use `send_message` to the TL

Do not guess. Do not implement past the ambiguity. Report exactly what is unclear.

## Scope-Creep Path

If the task grows beyond the original issue spec (TL adds extra requests, or you discover prerequisite work):

1. `chainlink_issue_comment issue_id=<id> message="SCOPE: <specific reason>"`
2. `notify_parent` with `SCOPE: <specific reason>`
3. Continue on the original issue unless redirected

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `chainlink_session_start` | Start a chainlink work session |
| `chainlink_session_work` | Mark the assigned subissue as the active work item |
| `chainlink_issue_show` | Read issue details |
| `chainlink_issue_comment` | Post a progress comment on the issue |
| `chainlink_session_end` | End session with optional handoff notes |
| `notify_parent` | Report results or issues to parent TL |
| `send_message` | Send messages to the TL when coordination is needed |

## Hard Rules

- Never call `chainlink close` or any close MCP tool
- Never initialize Chainlink agent identity
- Never create Chainlink subissues
- Start the session before marking work active
- Never create branches or commit unless explicitly instructed
- If blocked, report immediately — never wait more than 2 minutes before escalating
- If scope creep appears, report it — do not absorb extra work silently
