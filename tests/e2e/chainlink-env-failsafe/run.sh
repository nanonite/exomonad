#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink env failsafe test.
# Validates SessionStart aborts when CHAINLINK_DB is missing, points at a
# missing directory, or points at a phantom directory without issues.db.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

echo ">>> [Phase 0] Checking preconditions..."

EXOMONAD_BIN=""
if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
    export PATH="$PROJECT_ROOT/target/debug:$PATH"
elif command -v exomonad &>/dev/null; then
    EXOMONAD_BIN="$(command -v exomonad)"
else
    echo "ERROR: exomonad binary not found. Run 'just install-all-dev' or 'cargo build -p exomonad'."
    exit 1
fi
echo "  exomonad: $EXOMONAD_BIN"

for cmd in git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  git, python3: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/chainlink-env-failsafe.XXXXXXXX")"
REPO_DIR="$WORK_DIR/repo"
SERVER_LOG="$WORK_DIR/server.log"
PHANTOM_DB="$WORK_DIR/phantom-chainlink"

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

cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-chainlink-env-failsafe"
yolo = true
EOF

mkdir -p "$PHANTOM_DB"

echo "  Repo: $REPO_DIR"
echo "  Server log: $SERVER_LOG"

echo ">>> [Phase 2] Starting exomonad serve..."

RUST_LOG=info EXOMONAD_HOOK_TRACE=1 "$EXOMONAD_BIN" serve >"$SERVER_LOG" 2>&1 &
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

echo ">>> [Phase 3] Probing SessionStart failures..."

SESSION_START_PAYLOAD="$(python3 - "$REPO_DIR" <<'PY'
import json
import sys

repo = sys.argv[1]
print(json.dumps({
    "session_id": "chainlink-env-failsafe",
    "hook_event_name": "SessionStart",
    "transcript_path": "/tmp/chainlink-env-failsafe.jsonl",
    "cwd": repo,
    "source": "startup",
}))
PY
)"

validate_failure_json() {
    local label="$1"
    local expected="$2"
    local output="$3"

    python3 - "$label" "$expected" "$output" <<'PY'
import json
import sys

label, expected, raw = sys.argv[1:4]
data = json.loads(raw)
reason = data.get("stopReason") or data.get("systemMessage") or ""
if data.get("continue") is not False or expected not in reason:
    print(f"{label}: unexpected response")
    print(raw)
    raise SystemExit(1)
PY
}

run_probe() {
    local label="$1"
    local expected="$2"
    shift 2
    local output
    local status

    set +e
    output="$(printf '%s' "$SESSION_START_PAYLOAD" | "$@" 2>/dev/null)"
    status=$?
    set -e

    if [[ "$status" -ne 2 ]]; then
        echo "ERROR: expected $label to exit 2, got $status"
        printf '%s
' "$output"
        exit 1
    fi
    validate_failure_json "$label" "$expected" "$output"
    echo "  $label: failed loudly"
}

for runtime in claude codex opencode; do
    run_probe "$runtime unset CHAINLINK_DB" "CHAINLINK_DB not set"         env -u CHAINLINK_DB EXOMONAD_ROLE=root EXOMONAD_AGENT_ID="env-failsafe-$runtime" EXOMONAD_SESSION_ID=main "$EXOMONAD_BIN" hook session-start --runtime "$runtime"
    run_probe "$runtime missing CHAINLINK_DB path" "missing path"         env CHAINLINK_DB="$WORK_DIR/missing-chainlink" EXOMONAD_ROLE=root EXOMONAD_AGENT_ID="env-failsafe-$runtime" EXOMONAD_SESSION_ID=main "$EXOMONAD_BIN" hook session-start --runtime "$runtime"
    run_probe "$runtime phantom CHAINLINK_DB" "phantom DB directory without issues.db"         env CHAINLINK_DB="$PHANTOM_DB" EXOMONAD_ROLE=root EXOMONAD_AGENT_ID="env-failsafe-$runtime" EXOMONAD_SESSION_ID=main "$EXOMONAD_BIN" hook session-start --runtime "$runtime"
done

echo ">>> PASS: Chainlink SessionStart env failsafe E2E"
