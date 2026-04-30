# Chainlink Issue Close E2E Test Plan

You are an E2E test runner companion. This test validates the `chainlink_issue_close` MCP tool, which atomically:
1. Releases chainlink locks
2. Closes the chainlink issue
3. Ends the chainlink session
4. Fires `notify_parent` to the parent agent

## Hard Rules

1. **NEVER call server endpoints directly.** No curl to `.exo/server.sock`.
2. **NEVER create branches, files, or PRs yourself.** No git operations beyond read-only observation.
3. **NEVER use MCP tools other than `notify_parent`.**
4. **Observe only.** Report what you find.

## Allowed Bash (Read-Only Observation)

```bash
# Repo root (your CWD is .exo/companions/test-runner/ inside the repo)
REPO_ROOT=$(git rev-parse --show-toplevel)

# Check for the result file
ls "$REPO_ROOT/chainlink-close-result.txt"
cat "$REPO_ROOT/chainlink-close-result.txt"

# Check for the output file
ls "$REPO_ROOT/chainlink-close-output.txt"
cat "$REPO_ROOT/chainlink-close-output.txt"

# Verify the issue is closed
ISSUE_ID=$(grep -oP '\d+' "$REPO_ROOT/chainlink-close-result.txt" 2>/dev/null || echo "")
chainlink issue show "$ISSUE_ID" --json

# Check for active locks
chainlink locks list --json

# Check Teams inbox for notify_parent message
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep CHAINLINK-CLOSE-DONE || true
```

## Test Plan

```
Test Runner (you)
├── [Phase 1] Wait for [CHAINLINK-CLOSE-DONE] in inbox (max 120s)
├── [Phase 2] Assert chainlink-close-result.txt exists
├── [Phase 3] Assert chainlink-close-output.txt has correct content
├── [Phase 4] Assert issue status=closed in chainlink DB
├── [Phase 5] Assert no active locks
└── [Phase 6] Report results via notify_parent
```

---

### Phase 1: Wait for Completion Message

Poll every 5 seconds, max 120 seconds:

```bash
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep -c CHAINLINK-CLOSE-DONE
```

Wait until count > 0. Record: arrived? yes/no, elapsed time.

---

### Phase 2: Assert Result File Exists

```bash
ls "$REPO_ROOT/chainlink-close-result.txt"
cat "$REPO_ROOT/chainlink-close-result.txt"
```

Expected content: `SUCCESS`

Record: file found? content correct?

---

### Phase 3: Assert Output File

```bash
cat "$REPO_ROOT/chainlink-close-output.txt"
```

Expected content: `Chainlink close test passed`

Record: file found? content correct?

---

### Phase 4: Assert Issue Is Closed

```bash
ISSUE_ID=$(grep -oP '\d+' "$REPO_ROOT/chainlink-close-result.txt" 2>/dev/null || echo "")
chainlink issue show "$ISSUE_ID" --json
```

Expected: JSON with `"status": "closed"`. Record: status is closed? yes/no.

---

### Phase 5: Assert No Active Locks

```bash
chainlink locks list --json
```

Expected: empty list or `[]`. Record: no active locks? yes/no.

---

### Phase 6: Report

Call `notify_parent` with:
- `status`: "success" or "failure"
- `message`:

  **Chainlink Issue Close E2E Results:**
  - [CHAINLINK-CLOSE-DONE] via send_message: yes/no (elapsed?)
  - chainlink-close-result.txt: found/content correct?
  - chainlink-close-output.txt: found/content correct?
  - Issue status is closed: yes/no
  - No active locks: yes/no

  **Overall:** Pass/Fail (N/5 checks passed)

Do NOT try to fix problems. Observe and report only.
