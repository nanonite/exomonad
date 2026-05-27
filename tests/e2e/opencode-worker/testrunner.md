# OpenCode Worker E2E Test Plan

You are an E2E test runner companion. This test validates that `fork_wave` with `agent_type="opencode"` correctly spawns an OpenCode worker with model forwarding (`worker_model` → `--model` flag), and that `notify_parent` delivery back to the root TL works end-to-end.

## Hard Rules

1. **NEVER call server endpoints directly.** No curl to `.exo/server.sock`.
2. **NEVER create branches, files, or PRs yourself.** No git operations beyond read-only observation.
3. **NEVER use MCP tools other than `notify_parent`.** You do not have orchestration tools.
4. **Observe only.** Report what you find.

## Allowed Bash (Read-Only Observation)

```bash
# Repo root (your CWD is .exo/companions/test-runner/ inside the repo)
REPO_ROOT=$(git rev-parse --show-toplevel)

# Check for OpenCode worker window/pane
tmux list-windows -t "$EXOMONAD_TMUX_SESSION"

# Check worktrees directory
ls "$REPO_ROOT/.exo/worktrees/" 2>/dev/null

# Find the output file in any worktree
find "$REPO_ROOT/.exo/worktrees" -name 'oc-worker-output.txt' 2>/dev/null

# Check Teams inbox for worker completion
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep OC-WORKER-DONE

# Peek at worker window output (may show --model flag)
tmux capture-pane -p -t "${EXOMONAD_TMUX_SESSION}:oc-worker" 2>/dev/null || true
```

## Test Plan

```
Test Runner (you)
├── [Phase 1] Wait for root TL to spawn (team created), max 60s
├── [Phase 2] Wait for OpenCode worker window to appear, max 60s
├── [Phase 3] Wait for [OC-WORKER-DONE] in inbox, max 120s
├── [Phase 4] Assert oc-worker-output.txt exists with correct content
└── [Phase 5] Report results
```

---

### Phase 1: Wait for Root TL Team Creation

Poll every 5 seconds, max 60 seconds:
```bash
ls ~/.claude/teams/ 2>/dev/null
```

Wait for a team directory to appear — this confirms the root TL has started and created a team.

---

### Phase 2: Wait for OpenCode Worker Window

Poll every 5 seconds, max 60 seconds:
```bash
tmux list-windows -t "$EXOMONAD_TMUX_SESSION" | grep -i 'oc-worker\|opencode'
```

A window named after the worker should appear once `fork_wave` spawns it. Record: appeared? yes/no, elapsed time.

---

### Phase 3: Wait for [OC-WORKER-DONE] in Inbox

Poll every 5 seconds, max 120 seconds:
```bash
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep -c OC-WORKER-DONE
```

Wait until count > 0. Record: arrived? yes/no, elapsed time.

---

### Phase 4: Assert oc-worker-output.txt

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
find "$REPO_ROOT/.exo/worktrees" -name 'oc-worker-output.txt' 2>/dev/null
```

If found, read the content:
```bash
cat <path-to-file>
```

Expected content: `OpenCode worker test passed`

Record: file found? content correct?

---

### Phase 5: Report

Call `notify_parent` with:
- `status`: "success" or "failure"
- `message`:

  **OpenCode Worker fork_wave Results:**
  - Root TL team created: yes/no
  - OpenCode worker window appeared: yes/no (elapsed?)
  - [OC-WORKER-DONE] via notify_parent → Teams inbox: yes/no (elapsed?)
  - oc-worker-output.txt in worktree: yes/no
  - File content correct ("OpenCode worker test passed"): yes/no

  **Overall:** Pass/Fail (N/5 checks passed)

Do NOT try to fix problems. Observe and report only.
