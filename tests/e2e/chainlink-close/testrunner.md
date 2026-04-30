# Chainlink Issue Close E2E Test Plan

You are an E2E test runner companion. Validates `chainlink_issue_close`:
TL → spawn_worker → worker claims issue → does work → chainlink_issue_close
→ close atomically releases locks, closes issue, ends session, fires notify_parent to TL.

## Hard Rules

1. **NEVER curl server endpoints, create files/branches/PRs, or use MCP tools other than `notify_parent`.**
2. **Observe only.** Report what you find.

## Allowed Bash (Read-Only)

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)

# Results written by TL after notification
ls "$REPO_ROOT/chainlink-close-result.txt" 2>/dev/null
cat "$REPO_ROOT/chainlink-close-result.txt"

# Worker output file (same directory as TL)
ls "$REPO_ROOT/chainlink-close-output.txt" 2>/dev/null
cat "$REPO_ROOT/chainlink-close-output.txt"

# Check chainlink state
chainlink issue list --json --status closed 2>/dev/null
chainlink locks list --json 2>/dev/null
```

## Test Plan

```
Phase 1: Wait for chainlink-close-result.txt (max 120s, poll 5s)
Phase 2: Assert result = SUCCESS
Phase 3: Assert worker chainlink-close-output.txt exists with correct content
Phase 4: Assert issue status=closed in chainlink DB
Phase 5: Assert no active locks
Phase 6: Report via notify_parent
```

### Phase 1: Poll for Result File

```bash
ls "$(git rev-parse --show-toplevel)/chainlink-close-result.txt" 2>/dev/null
```

### Phase 2-5: Assertions

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)

echo "=== Result ==="
cat "$REPO_ROOT/chainlink-close-result.txt"

echo "=== Worker Output ==="
cat "$REPO_ROOT/chainlink-close-output.txt" 2>/dev/null || echo "FILE_NOT_FOUND"

echo "=== Closed Issues ==="
chainlink issue list --json --status closed 2>/dev/null

echo "=== Locks ==="
chainlink locks list --json 2>/dev/null
```

### Phase 6: Report

Call `notify_parent` with:
- `status`: "success" or "failure"
- `message`:

  **Chainlink Issue Close E2E Results:**
  - TL received worker notify_parent: yes/no
  - chainlink-close-result.txt = SUCCESS: yes/no
  - Worker chainlink-close-output.txt found: yes/no
  - Issue closed in DB: yes/no
  - No active locks: yes/no
  **Overall:** Pass/Fail
