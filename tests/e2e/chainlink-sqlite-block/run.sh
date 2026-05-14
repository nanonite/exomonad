#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink sqlite block test.
# Validates PreToolUse denies direct `.chainlink/issues.db` access for
# Claude-shaped, Codex-shaped, and OpenCode-shaped hook invocations.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

echo ">>> [Phase 0] Checking preconditions..."

EXOMONAD_BIN=""
if command -v exomonad &>/dev/null; then
    EXOMONAD_BIN="$(command -v exomonad)"
elif [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
else
    echo "ERROR: exomonad binary not found. Run 'just install-all-dev' or 'cargo build -p exomonad'."
    exit 1
fi
echo "  exomonad: $EXOMONAD_BIN"

if ! command -v chainlink &>/dev/null; then
    echo "ERROR: chainlink binary not found in PATH."
    exit 1
fi
echo "  chainlink: $(command -v chainlink)"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

for cmd in git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  git, python3: OK"

echo ">>> [Phase 1] Creating temp environment..."

WORK_DIR="$(mktemp -d /tmp/exomonad-e2e-chainlink-sqlite-block.XXXXXXXX)"
REPO_DIR="$WORK_DIR/repo"
SERVER_LOG="$WORK_DIR/server.log"
SQLITE_MARKER="$WORK_DIR/sqlite3-executed"

echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        echo "  Stopped exomonad serve"
    fi
    if [[ -f "$SERVER_LOG" ]]; then
        echo "  Server log tail:"
        tail -n 20 "$SERVER_LOG" | sed 's/^/    /'
    fi
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git commit --allow-empty -m "initial commit" -q

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

if ! chainlink init 2>&1 | sed 's/^/  /'; then
    echo "ERROR: chainlink init failed during E2E setup."
    exit 1
fi

cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-chainlink-sqlite-block"
yolo = true
EOF

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/sqlite3" <<EOF
#!/usr/bin/env bash
touch "$SQLITE_MARKER"
exit 42
EOF
chmod +x "$WORK_DIR/bin/sqlite3"

echo "  Repo: $REPO_DIR"
echo "  Server log: $SERVER_LOG"

echo ">>> [Phase 2] Starting exomonad serve..."

RUST_LOG=info \
EXOMONAD_HOOK_TRACE=1 \
PATH="$WORK_DIR/bin:$PATH" \
"$EXOMONAD_BIN" serve >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
    if [[ -S "$REPO_DIR/.exo/server.sock" ]]; then
        echo "  Server socket ready"
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ERROR: exomonad serve exited before socket was ready."
        cat "$SERVER_LOG"
        exit 1
    fi
    sleep 0.5
done

if [[ ! -S "$REPO_DIR/.exo/server.sock" ]]; then
    echo "ERROR: timed out waiting for .exo/server.sock"
    cat "$SERVER_LOG"
    exit 1
fi

echo ">>> [Phase 3] Probing runtime hook payloads..."

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
    echo "  $runtime: denied sqlite .chainlink/issues.db access"
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
echo "  sqlite3 process marker absent"

echo ">>> [Phase 4] Checking hook trace logs..."
for runtime in claude codex opencode; do
    if ! grep -qi "runtime=$runtime" "$SERVER_LOG"; then
        echo "ERROR: server log missing hook trace for runtime=$runtime"
        exit 1
    fi
done
echo "  Runtime hook traces found"

echo ">>> PASS: Chainlink sqlite PreToolUse block E2E"
