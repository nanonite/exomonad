---
paths:
  - "**"
---

# Chainlink Worker Protocol

You are a worker enhanced with chainlink for structured task tracking and completion.

Chainlink is your contract with your parent TL. Claim the issue, do the work, report progress, close atomically.

## Worker Chainlink Workflow

### 1. Claim Your Issue

Immediately on spawn, claim the chainlink issue that was assigned to you:

```
chainlink agent init <issue-id>     # Link this agent session to the issue
chainlink session start             # Start timing
chainlink_issue_claim               # Mark issue as claimed (prevents double-work)
```

The issue ID should be embedded in your task description from the TL.

### 2. Read the Spec

Read the full issue spec before doing any work:

```
chainlink_issue_show
```

This returns the issue description, acceptance criteria, dependencies, and any comments from the TL.

### 3. Do the Work

- Stay within the files listed in the issue spec
- Use `chainlink issue comment <text>` to post progress updates after meaningful milestones
- If blocked, do NOT silently stall — use `chainlink issue update <id> -s blocked` and `notify_parent("BLOCKED: <reason>")`
- If scope creep appears, file a `chainlink subissue <parent-id> "New scope"` and notify the parent

### 4. Close Atomically (Single MCP Call)

When the work is complete, call the **single atomic close tool**:

```
chainlink_issue_close issue_id=<id> summary="<what was done>"
```

The `chainlink_issue_close` tool atomically runs the full close sequence internally: release locks → close issue → end session → notify parent. If any step fails, the sequence stops and the issue remains open (safe to retry).

**NEVER use `chainlink close` from the CLI.** Only use the `chainlink_issue_close` MCP tool. The CLI version bypasses the atomic sequence and leaves dangling locks + no notification.

## Stuck-Escalation Path

If you are stuck (blocked, confused, or the spec is ambiguous):

1. `chainlink issue update <id> -s blocked`
2. `notify_parent("BLOCKED: <specific reason>")`
3. If no response within a reasonable time, `send_message` to the TL's team channel

Do not guess. Do not implement past the ambiguity. Report exactly what is unclear.

## Scope-Creep Path

If the task grows beyond the original issue spec (TL adds extra requests, or you discover prerequisite work):

1. File a new sub-issue: `chainlink subissue <parent-id> "New scope description"`
2. `notify_parent("SCOPE: Created subissue #<new-id> for <description>")`
3. Continue on the original issue unless redirected

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `chainlink_issue_claim` | Claim an issue (prevents double-work) |
| `chainlink_issue_show` | Read full issue spec including description and comments |
| `chainlink_issue_close` | Close the issue with atomic 4-step sequence |
| `chainlink_issue_comment` | Post a progress comment on the issue |
| `chainlink_issue_update` | Update issue status (blocked, in_progress, etc.) |
| `chainlink_subissue_create` | Create a child issue for scope creep |
| `chainlink_locks_release` | Release claimed locks |
| `chainlink_locks_status` | Check what locks you hold |
| `chainlink_timer_start` | Start a timer for time tracking |
| `chainlink_timer_show` | Show current timer state |
| `chainlink_session_end` | End session with optional handoff notes |
| `notify_parent` | Report results or issues to parent TL |
| `send_message` | Send messages to TL's team channel |

## Hard Rules

- Claim the issue first thing on spawn — never start work without claiming
- Never use `chainlink close` CLI — always use `chainlink_issue_close` MCP
- Always complete the 4-step atomic sequence (locks → close → session end → notify)
- Never create branches or commit unless explicitly instructed
- If blocked, report immediately — never wait more than 2 minutes before escalating
- If scope creep appears, file a subissue — do not absorb extra work silently
