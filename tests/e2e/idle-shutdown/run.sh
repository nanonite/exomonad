#!/usr/bin/env bash
set -euo pipefail

# E2E idle/shutdown convergence test.
# Validates that root closes a small Chainlink backlog, observes empty inbox
# checks, calls has_pending_work, then calls shutdown_server.

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
    echo "ERROR: exomonad binary not found. Run 'cargo build -p exomonad' or 'just install-all-dev'."
    exit 1
fi
echo "  exomonad: $EXOMONAD_BIN"

for cmd in chainlink claude git python3 tmux; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  chainlink: $(command -v chainlink)"
echo "  claude, git, python3, tmux: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

for tool in check_inbox has_pending_work shutdown_server chainlink_issue_list chainlink_issue_show chainlink_issue_comment chainlink_issue_close; do
    if grep -q "$tool" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
        echo "  MCP tool '$tool': FOUND"
    else
        echo "ERROR: MCP tool '$tool' missing from WASM binary."
        exit 1
    fi
done

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/idle-shutdown.XXXXXXXX")"
SESSION="e2e-idle-shutdown"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"

cleanup() {
    local code=$?
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "  Killed tmux session"
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if [[ "${KEEP_E2E_WORKDIR:-0}" == "1" ]]; then
        echo "  Keeping work dir: $WORK_DIR"
    else
        rm -rf "$WORK_DIR"
        echo "  Removed $WORK_DIR"
    fi
    echo ">>> Done."
    exit "$code"
}
trap cleanup EXIT

tmux kill-session -t "$SESSION" 2>/dev/null || true

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git commit --allow-empty -m "initial commit" -q
git push -u origin main -q

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

if ! chainlink init 2>&1 | sed 's/^/  /'; then
    echo "ERROR: chainlink init failed during E2E setup."
    exit 1
fi

chainlink create "Verify idle shutdown e2e first issue" -p low --label test >/dev/null
chainlink create "Verify idle shutdown e2e second issue" -p low --label test >/dev/null

mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" <<'PY'
import pathlib
import sys

value = pathlib.Path(sys.argv[1]).read_text()
print(value.replace('"""', '\\"\\"\\"'))
PY
)"

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "claude"
yolo = true
model = "haiku"
initial_prompt = """
$ROOT_PROMPT
"""

[[companions]]
name = "idle-shutdown-observer"
agent_type = "claude"
role = "testrunner"
model = "haiku"
command = "claude --dangerously-skip-permissions"
task = "Execute the idle shutdown observer plan from your role context. Start immediately."
EOF

mkdir -p .exo/roles/devswarm/context
cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md

git add .
git commit -q -m "initialize idle shutdown e2e fixture"

echo "  Work dir: $WORK_DIR"
echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"

echo ">>> [Phase 2] Configuring environment..."
export FORGEJO_TOKEN="test-token-e2e"
export EXOMONAD_LOG_FORMAT=""
echo "  FORGEJO_TOKEN=test-token-e2e"

echo ">>> [Phase 3] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Idle Shutdown Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  Root should close two seeded Chainlink issues"
echo "  then call has_pending_work and shutdown_server."
echo "============================================"
echo ""

set +e
"$EXOMONAD_BIN" init --verbose --session "$SESSION"
INIT_STATUS=$?
set -e

if "$SCRIPT_DIR/validate.sh" "$REPO_DIR" "$RESULT_FILE"; then
    exit 0
fi

exit "$INIT_STATUS"
