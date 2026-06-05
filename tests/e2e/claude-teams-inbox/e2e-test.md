# E2E Claude Teams Inbox Review Chain

This is an automated E2E test. Execute only the steps in this file. Do not inspect files, browse, research, or ask for clarification. All values needed for the test are written below.

You are the root TL in the Claude Teams inbox review-chain test. The validator process observes tmux, the mock Forgejo API, generated Claude hook settings, Teams inbox delivery logs, and reviewer activity.

Do these steps now:

Step 1. Call TeamCreate to create a Claude Team for this session.

Step 2. Call the ExoMonad chainlink_issue_create MCP tool with title E2E Claude Teams inbox dev leaf, priority low, and labels e2e, claude, teams. Save the returned issue id.

Step 3. Call the ExoMonad spawn_leaf MCP tool exactly once with these arguments:
name: teams-inbox-dev
agent_type: claude
task: Implement the E2E marker for Chainlink issue #ISSUE_ID, replacing ISSUE_ID with the id from Step 2. In the Claude dev leaf, do exactly this: create or overwrite teams-inbox-marker.txt with the single line Claude Teams inbox review chain passed. Run git status --short. Commit the change with message Add Claude Teams inbox marker. Call the ExoMonad file_pr MCP tool with title fix: add Claude Teams inbox marker and body [CLAUDE-TEAMS-DEV-PR] Claude dev leaf filed PR for reviewer path. Then stop and idle. The reviewer and watcher path is under test. Do not call notify_parent.

Step 4. After spawn_leaf returns, stop and idle. Do not merge. Do not call merge_pr.

Hard rules:
1. Do not run gh commands.
2. Do not ask for assignment, issue id, file path, content, commit message, or notification values; they are specified above.
3. Do not create more than one leaf.
4. Do not implement the leaf task yourself in the root TL.
5. Do not merge the PR.
