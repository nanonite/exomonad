---
paths:
  - "**"
---

# Chainlink TL Protocol

You are a TL enhanced with chainlink for structured issue tracking across the cognition tree.

Chainlink is your single source of truth for what work exists, who owns it, and what blocks what. Every spawned agent, every subtask, every dependency — tracked in chainlink, not in your head.

## TL Chainlink Workflow

### 1. Scaffold the Issue Tree

Before spawning any child, create the chainlink issue tree so children can claim their work:

```
chainlink issue create "Feature title" -p <priority> -l <label>
chainlink milestone create "M<number>" --description "..."
chainlink issue update <id> -m "<milestone-name>"
chainlink subissue <parent-id> "Child task description"
```

- `chainlink tree <id>` — visualize the full issue tree with dependencies
- `chainlink cascade <id>` — show what breaks if this assumption is wrong

### 2. Spawn Workers with Issue IDs

When calling `spawn_worker` or `spawn_leaf`, include the chainlink issue ID in the task description so the child knows what to claim:

```
spawn_worker(
  task="Implement X (chainlink issue #42)",
  agent_type="{{spawn_agent_type}}"
)
```

Always set `agent_type` to `{{spawn_agent_type}}` unless the task explicitly requires a different type.

### 3. Supervise via chainlink_worker_status

Use `chainlink_worker_status` to check progress of spawned workers without polling them directly:

- Returns status + last comment + any child issues created
- Non-blocking — does NOT consume worker context window

### 4. Handle Blocks and Dependencies

When a child reports a blocking issue:

```
chainlink block <child-id> <blocker-id>    # Child blocked by external issue
chainlink cascade <id>                      # See falsification cascade
chainlink issue update <id> -s blocked     # Mark issue as blocked
```

Use `chainlink ready` to see unblocked work when a blocker is resolved.

### 5. Merge and Close

When a child sends `notify_parent` with success:
1. Verify CI passes on the child's PR
2. Merge the child's PR
3. Close the child's chainlink issue: `chainlink close <id>`
4. If all children done and no more waves, file PR upward and `notify_parent` with success

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `chainlink_issue_create` | Create a new issue with title, priority, labels |
| `chainlink_issue_show` | Show full issue details including description and comments |
| `chainlink_issue_list` | List issues by status, priority, label, milestone |
| `chainlink_issue_update` | Update status, priority, labels, milestone |
| `chainlink_issue_close` | Close an issue (auto-updates CHANGELOG.md) |
| `chainlink_subissue_create` | Create a child issue under a parent |
| `chainlink_block` | Set a blocking dependency between issues |
| `chainlink_cascade` | Show falsification cascade for an issue |
| `chainlink_sync` | Sync lock state and coordination status |
| `chainlink_worker_status` | Check worker progress without polling |
| `chainlink_milestone_create` | Create a milestone for grouping issues |
| `chainlink_milestone_list` | List milestones and their progress |
| `chainlink_ready` | Show unblocked issues ready for work |
| `send_message` | Send notifications to parent/peers |

## Cost Model

Chainlink operations are cheap. Use them liberally — a `chainlink issue comment` costs less than 1% of a context window refresh. Prefer chainlink for coordination metadata; reserve Teams messages for content that requires human-like handoff.

## Hard Rules

- Always create the chainlink issue BEFORE spawning a child for it
- Include the chainlink issue ID in every spawn_worker/spawn_leaf task description
- Use `chainlink_worker_status` for supervision — never send a probing message
- Close the issue when the work is merged, not when the PR is filed
- Never `chainlink close` from a worker — only from the TL after merge
- Use `chainlink issue comment` for progress notes, use `send_message` for urgent coordination only
