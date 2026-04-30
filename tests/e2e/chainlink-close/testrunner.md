# Chainlink Issue Close E2E Test Plan

You are an E2E test runner companion. This test validates `chainlink_issue_close` MCP tool:
TL spawns OpenCode worker → worker claims issue → works → calls `chainlink_issue_close`
→ close atomically releases locks, closes issue, ends session, fires `notify_parent` to TL.

## Hard Rules

1. **NEVER call server endpoints directly.** No curl to `.exo/server.sock`.
2. **NEVER create branches, files, or PRs yourself.** No git operations beyond read-only observation.
3. **NEVER use MCP tools other than `notify_parent`.**
4. **Observe only.** Report what you find.

## Allowed Bash (Read-Only Observation)

```bash
# Repo root (your CWD is .exo/companions/test-runner/ inside the repo)
REPO_ROOT=$(git rev-parse --show-toplevel)

# Check for the result file written by TL after worker notification
ls "$REPO_ROOT/chainlink-close-result.txt" 2>/dev/null
cat "$REPO_ROOT/chainlink-close-result.txt"

# Check for worker output file in worktree
ls "$REPO_ROOT/.exo/worktrees/close-worker/chainlink-close-output.txt" 2>/dev/null
cat "$REPO_ROOT/.exo/worktrees/close-worker/chainlink-close-output.txt"

# Check Teams inbox for completion message
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep -o 'CHAINLINK-CLOSE-DONE.*' || true
```

## Test Plan

```
Test Runner (you)
├── [Phase 1] Wait for CHAINLINK-CLOSE-DONE (max 120s)
├── [Phase 2] Assert chainlink-close-result.txt = SUCCESS
├── [Phase 3] Assert worker chainlink-close-output.txt in worktree
├── [Phase 4] Assert issue status=closed in chainlink DB
├── [Phase 5] Assert no active locks
└── [Phase 6] Report results via notify_parent
```

---

### Phase 1: Wait for Completion Message

Poll every 5 seconds, max 120 seconds:

```bash
ls "$(git rev-parse --show-toplevel)/chainlink-close-result.txt" 2>/dev/null
```

Wait until file exists. Record: elapsed time.

---

### Phase 2: Assert Result File

```bash
cat "$(git rev-parse --show-toplevel)/chainlink-close-result.txt"
```

Expected: `SUCCESS`

Record: content correct? yes/no.

---

### Phase 3: Assert Worker Output

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cat "$REPO_ROOT/.exo/worktrees/close-worker/chainlink-close-output.txt" 2>/dev/null || echo "FILE_NOT_FOUND"
```

Expected: `Worker close test passed`

Record: file found? content correct?

---

### Phase 4: Assert Issue Is Closed

Parse the issue ID from the completion message or result file, then:

```bash
# Find issue ID from send_message or from chainlink list
chainlink issue list --json --status closed 2>/dev/null
```

Expected: at least one closed issue with title "E2E chainlink close test".
Record: issue closed? yes/no.

---

### Phase 5: Assert No Active Locks

```bash
chainlink locks list --json 2>/dev/null
```

Expected: empty list or `[]`. Record: no active locks? yes/no.

---

### Phase 6: Report

Call `notify_parent` with:
- `status`: "success" or "failure"
- `message`:

  **Chainlink Issue Close E2E Results:**
  - TL received worker notify_parent: yes/no
  - chainlink-close-result.txt = SUCCESS: yes/no
  - Worker wrote chainlink-close-output.txt: yes/no
  - Issue status is closed: yes/no
  - No active locks: yes/no

  **Overall:** Pass/Fail (N/5 checks passed)

Do NOT try to fix problems. Observe and report only.
