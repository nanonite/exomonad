# OpenCode TL E2E Test Plan

You are an E2E test runner companion. This test validates the full ACP delivery chain for OpenCode as root TL:
`exomonad init` → `opencode serve` starts → ACP port captured → `opencode run --attach` delivers `initial_prompt` → OpenCode uses MCP → `notify_parent` reaches testrunner via Teams inbox.

## Hard Rules

1. **NEVER call server endpoints directly.** No curl to `.exo/server.sock`.
2. **NEVER create branches, files, or PRs yourself.** No git operations beyond read-only observation.
3. **NEVER use MCP tools other than `notify_parent`.** You do not have orchestration tools.
4. **Observe only.** Report what you find.

## Allowed Bash (Read-Only Observation)

```bash
# Find the repo root (your CWD is .exo/companions/test-runner/ inside the repo)
REPO_ROOT=$(git rev-parse --show-toplevel)

# Check for the output file
ls "$REPO_ROOT/opencode-tl-test.txt"
cat "$REPO_ROOT/opencode-tl-test.txt"

# Check Teams inbox for notify_parent message
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep OC-TL-DONE

# Check tmux session windows
tmux list-windows -t "$EXOMONAD_TMUX_SESSION"
```

## Test Plan

```
Test Runner (you)
├── [Phase 1] Poll for opencode-tl-test.txt (max 90s)
├── [Phase 2] Assert file content
├── [Phase 3] Assert [OC-TL-DONE] in Teams inbox
└── [Phase 4] Report results
```

---

### Phase 1: Poll for opencode-tl-test.txt

Poll every 5 seconds, max 90 seconds:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
ls "$REPO_ROOT/opencode-tl-test.txt" 2>/dev/null
```

The OpenCode root TL receives its task via ACP (`initial_prompt` in config). It should create this file shortly after startup. If not found within 90 seconds, record TIMEOUT.

---

### Phase 2: Assert File Content

Once found:
```bash
cat "$REPO_ROOT/opencode-tl-test.txt"
```

Expected content: `OpenCode TL test passed`

Record: content matches? yes/no.

---

### Phase 3: Assert [OC-TL-DONE] in Your Own Inbox

OpenCode uses `send_message` targeting you (`test-runner`) directly, since it is the root and has no parent to `notify_parent` to. The message arrives in your own Teams inbox.

```bash
cat ~/.claude/teams/*/inboxes/*.json 2>/dev/null | grep OC-TL-DONE
```

You can also check for `<teammate-message>` delivery — if the message was delivered via Teams inbox while you were running, it should appear in your conversation. Record: message found in inbox? yes/no. Note the delivery method if visible in the JSON.

---

### Phase 4: Report

Call `notify_parent` with:
- `status`: "success" or "failure"
- `message`:

  **OpenCode TL ACP Chain Results:**
  - opencode-tl-test.txt created: yes/no (timeout after Xs?)
  - File content correct ("OpenCode TL test passed"): yes/no
  - [OC-TL-DONE] via send_message → Teams inbox: yes/no

  **Overall:** Pass/Fail

Do NOT try to fix problems. Observe and report only.
