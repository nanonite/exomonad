#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink sqlite block test.
# Validates PreToolUse denies direct `.chainlink/issues.db` access for
# Claude-shaped, Codex-shaped, and OpenCode-shaped hook invocations.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

e2e_preflight chainlink git python3

e2e_phase "Phase 1" "Creating temp environment..."
e2e_create_work_dir "chainlink-sqlite-block"
e2e_install_cleanup_trap
SQLITE_MARKER="$WORK_DIR/sqlite3-executed"

e2e_init_repo "Exomonad E2E" "e2e@example.com"
e2e_run_exomonad_new
e2e_install_project_wasm_and_roles
e2e_chainlink_init
e2e_write_basic_config "e2e-chainlink-sqlite-block"

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/sqlite3" <<EOF
#!/usr/bin/env bash
touch "$SQLITE_MARKER"
exit 42
EOF
chmod +x "$WORK_DIR/bin/sqlite3"

e2e_log "Repo: $REPO_DIR"
e2e_log "Server log: $SERVER_LOG"

e2e_phase "Phase 2" "Starting exomonad serve..."
e2e_start_server "PATH=$WORK_DIR/bin:$PATH"

e2e_phase "Phase 3" "Probing runtime hook payloads..."

validate_deny_json() {
    local runtime="$1"
    local output="$2"

    python3 - "$runtime" "$output" <<'PY'
import json
import sys

runtime, raw = sys.argv[1:3]
data = json.loads(raw)
hook = data.get("hookSpecificOutput", {})

if runtime == "codex":
    decision = hook.get("permissionDecision")
    reason = hook.get("permissionDecisionReason", "")
    ok = decision == "deny" and "Chainlink sqlite" in reason
else:
    decision = hook.get("permissionDecision")
    reason = data.get("stopReason", "") or hook.get("permissionDecisionReason", "")
    ok = data.get("continue") is False and decision == "deny" and "Chainlink sqlite" in reason

if not ok:
    print(raw)
    raise SystemExit(1)
PY
}

run_probe() {
    local runtime="$1"
    local payload="$2"
    local output
    local status

    set +e
    output="$(
        printf '%s' "$payload" | \
            EXOMONAD_ROLE=dev \
            EXOMONAD_AGENT_ID="sqlite-block-$runtime-dev" \
            EXOMONAD_SESSION_ID=main \
            "$EXOMONAD_BIN" hook pre-tool-use --runtime "$runtime" 2>/dev/null
    )"
    status=$?
    set -e

    if [[ "$runtime" != "codex" && "$status" -ne 2 ]]; then
        echo "ERROR: expected $runtime hook deny exit code 2, got $status"
        printf '%s\n' "$output"
        exit 1
    fi
    if [[ "$runtime" == "codex" && "$status" -ne 0 ]]; then
        echo "ERROR: expected codex hook deny to exit 0 with deny JSON, got $status"
        printf '%s\n' "$output"
        exit 1
    fi

    validate_deny_json "$runtime" "$output"
    e2e_log "$runtime: denied sqlite .chainlink/issues.db access"
}

SQLITE_COMMAND="sqlite3 .chainlink/issues.db 'select * from issues'"
CLAUDE_PAYLOAD="$(python3 - "$REPO_DIR" "$SQLITE_COMMAND" <<'PY'
import json
import sys

repo, command = sys.argv[1:3]
print(json.dumps({
    "session_id": "sqlite-block-claude",
    "hook_event_name": "PreToolUse",
    "tool_name": "bash",
    "tool_input": {"command": command},
    "transcript_path": "/tmp/sqlite-block-claude.jsonl",
    "cwd": repo,
    "permission_mode": "default",
}))
PY
)"
OPENCODE_PAYLOAD="$(python3 - "$REPO_DIR" "$SQLITE_COMMAND" <<'PY'
import json
import sys

repo, command = sys.argv[1:3]
print(json.dumps({
    "session_id": "sqlite-block-opencode",
    "hook_event_name": "PreToolUse",
    "tool_name": "bash",
    "tool_input": {"command": command},
    "transcript_path": "/tmp/sqlite-block-opencode.jsonl",
    "cwd": repo,
    "permission_mode": "default",
}))
PY
)"
CODEX_PAYLOAD="$(python3 - "$REPO_DIR" "$SQLITE_COMMAND" <<'PY'
import json
import sys

repo, command = sys.argv[1:3]
print(json.dumps({
    "session_id": "sqlite-block-codex",
    "hook_event_name": "PreToolUse",
    "tool": "bash",
    "args": {"command": command},
    "cwd": repo,
}))
PY
)"

run_probe "claude" "$CLAUDE_PAYLOAD"
run_probe "codex" "$CODEX_PAYLOAD"
run_probe "opencode" "$OPENCODE_PAYLOAD"

if [[ -f "$SQLITE_MARKER" ]]; then
    echo "ERROR: sqlite3 marker exists; sqlite3 command was executed."
    exit 1
fi
e2e_log "sqlite3 process marker absent"

e2e_phase "Phase 4" "Checking hook trace logs..."
for runtime in claude codex opencode; do
    if ! grep -qi "runtime=$runtime" "$SERVER_LOG"; then
        echo "ERROR: server log missing hook trace for runtime=$runtime"
        exit 1
    fi
done
e2e_log "Runtime hook traces found"

echo ">>> PASS: Chainlink sqlite PreToolUse block E2E"
