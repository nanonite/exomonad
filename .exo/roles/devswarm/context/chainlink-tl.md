---
paths:
  - "**"
---

# Chainlink TL Protocol

You are a TL enhanced with chainlink for structured issue tracking across the cognition tree.

Chainlink is your single source of truth for what work exists, who owns it, and what blocks what. Every spawned agent, every subtask, every dependency — tracked in chainlink, not in your head.

## Canonical DB Location

The project's chainlink DB lives at `<project_root>/.chainlink/` regardless of which worktree any agent runs in. ExoMonad sets `CHAINLINK_DB` to that directory in every spawned agent's environment, so leaves and workers resolve the same DB you do.

- Run `chainlink ...` and `chainlink_*` MCP tools normally; never pass `--db` explicitly.
- When you spec a leaf, you do not need to tell it where the DB is — assume it has the env var. If it does not, that is an infrastructure bug to report, not a per-leaf workaround.
- Sub-TLs you spawn inherit the same env var; they orchestrate against the same canonical DB.

## TL Chainlink Workflow

### 1. Scaffold the Issue Tree

Before spawning any child, create the chainlink issue tree so children can claim their work:

```
chainlink_issue_create title="Feature title" priority="<priority>" labels=["<label>"]
chainlink_milestone_create title="M<number>" description="..."
chainlink_issue_update issue_id=<id> milestone="<milestone-name>"
chainlink_subissue_create parent_id=<parent-id> title="Child task description"
```

- `chainlink_cascade` — show what breaks if this assumption is wrong

### 2. Spawn Workers with Issue IDs

When calling `spawn_worker` or `spawn_leaf`, include the chainlink issue ID in the task description so the child knows what to claim:

```
spawn_worker(
  task="Implement X (chainlink issue #42)"
)
```

Omit `agent_type` to use `{{spawn_agent_type}}`; set it only when the task explicitly requires a different type.

### 3. Supervise Via Session Status

Use `chainlink_session_status` to check progress of spawned workers without polling them directly:

- Shows whether a session exists, which issue is active, and the last recorded action
- Non-blocking — does not consume worker context window

### 4. Handle Blocks and Dependencies

When a child reports a blocking issue:

```
chainlink_block child_id=<child-id> blocker_id=<blocker-id>
chainlink_cascade issue_id=<id>
chainlink_issue_update issue_id=<id> status="blocked"
```

Use `chainlink_issue_list` to inspect open work when a blocker is resolved.

### 5. Merge and Close

When a child sends `notify_parent` with success:
1. Confirm the child ended its Chainlink session with handoff notes
2. Verify CI passes on the child's PR
3. Merge the child's PR
4. Close the child's Chainlink issue with `chainlink_issue_close`
5. If all children are done and no more waves remain, file PR upward and `notify_parent` with success

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `chainlink_issue_create` | Create a new issue with title, priority, labels |
| `chainlink_issue_show` | Show full issue details including description and comments |
| `chainlink_issue_list` | List issues by status, priority, label, milestone |
| `chainlink_issue_update` | Update status, priority, labels, milestone |
| `chainlink_issue_close` | Close an issue (auto-updates CHANGELOG.md) |
| `chainlink_subissue_create` | Create a child issue under a parent |
| `chainlink_session_status` | Read session progress for active work |
| `chainlink_timer_start` | Start coordinator-owned lifecycle timing |
| `chainlink_timer_stop` | Stop coordinator-owned lifecycle timing for a specific issue |
| `chainlink_timer_status` | Check active timer state for one issue or all active timers |
| `chainlink_block` | Set a blocking dependency between issues |
| `chainlink_cascade` | Show falsification cascade for an issue |
| `chainlink_milestone_create` | Create a milestone for grouping issues |
| `chainlink_milestone_list` | List milestones and their progress |
| `send_tmux_message` | Send notifications to parent/peers |

## Cost Model

Chainlink operations are cheap. Use them liberally. Prefer chainlink for coordination metadata; reserve Teams messages for content that requires human-like handoff.

## Hard Rules

- Always create the chainlink issue BEFORE spawning a child for it
- Include the chainlink issue ID in every spawn_worker/spawn_leaf task description
- Use `chainlink_session_status` for supervision — never send a probing message
- Start timers when assigning work and stop them with the same issue id after review, CI, and merge complete
- Close the issue when the work is merged, not when the PR is filed
- Never ask a worker or dev leaf to close its own assigned issue
- Never use Chainlink agent, sync, or lock commands
- Use `chainlink_issue_comment` for progress notes, use `send_tmux_message` for urgent coordination only
