#!/usr/bin/env bash
set -euo pipefail

# E2E Reviewer Ephemerality Test.
# Drives a local PR through reviewer verdict/disposal, duplicate-lock grace,
# synthetic SHA update, and fresh reviewer round validation.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

RUNTIME="${E2E_REVIEWER_RUNTIME:-codex}"
SESSION="e2e-reviewer-ephemerality-${RUNTIME}"

echo ">>> [Phase 0] Checking preconditions..."

EXOMONAD_BIN=""
if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
    export PATH="$PROJECT_ROOT/target/debug:$PATH"
elif command -v exomonad &>/dev/null; then
    EXOMONAD_BIN="$(command -v exomonad)"
else
    echo "ERROR: exomonad binary not found. Run 'just install-all-dev' or 'just build'."
    exit 1
fi
echo "  exomonad: $EXOMONAD_BIN"

for cmd in "$RUNTIME" codex chainlink tmux git python3 jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH. Set E2E_REVIEWER_RUNTIME to an installed runtime or install $cmd."
        exit 1
    fi
done
echo "  runtime=$RUNTIME / codex / chainlink / tmux / git / python3 / jq: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

echo ">>> [Phase 1] Creating temp environment..."
mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/reviewer-ephemerality.XXXXXXXX")"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
CODEX_HOME_DIR="$WORK_DIR/codex-home"
SERVER_LOG="$WORK_DIR/server.log"

echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if [[ -f "$SERVER_LOG" ]]; then
        echo "  Last 30 server log lines:"
        tail -n 30 "$SERVER_LOG" | sed 's/^/    /'
    fi
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

tmux kill-session -t "$SESSION" 2>/dev/null || true

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR" "$CODEX_HOME_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
cat >README.md <<'EOF'
# Reviewer Ephemerality E2E Fixture
EOF
git add README.md
git commit -m "initial commit" -q
git push -u origin main -q

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

if ! chainlink init 2>&1 | sed 's/^/  /'; then
    echo "ERROR: chainlink init failed during E2E setup."
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
mkdir -p .exo/context
cp "$SCRIPT_DIR/reviewer-checklist.md" .exo/context/reviewer-checklist.md
cp "$SCRIPT_DIR/testrunner.md" .exo/context/testrunner.md
ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).read_text().replace('"""', '\\"\\"\\"'))
PY
)"

cat >.exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "codex"
spawn_agent_type = "codex"
yolo = true
poll_interval = 3
initial_prompt = """
$ROOT_PROMPT
"""

[reviewer]
agent_type = "$RUNTIME"
context = [".exo/context/reviewer-checklist.md"]

[[companions]]
name = "reviewer-ephemerality-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$SERVER_LOG'"
EOF

if [[ -f "$HOME/.codex/auth.json" ]]; then
    cp -p "$HOME/.codex/auth.json" "$CODEX_HOME_DIR/auth.json"
fi
if [[ -f "$HOME/.codex/installation_id" ]]; then
    cp -p "$HOME/.codex/installation_id" "$CODEX_HOME_DIR/installation_id"
fi
cat >"$CODEX_HOME_DIR/config.toml" <<EOF
[projects."$REPO_DIR"]
trust_level = "trusted"
EOF

unset GITHUB_TOKEN
unset GITHUB_API_URL
export CODEX_HOME="$CODEX_HOME_DIR"
export EXOMONAD_LOG_FORMAT=""
export EXOMONAD_SERVER_LOG_FILE="$SERVER_LOG"

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"
echo "  Server log: $SERVER_LOG"
echo "  Runtime: $RUNTIME"

echo ">>> [Phase 2] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Reviewer Ephemerality Test Ready"
echo "  Runtime: $RUNTIME"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  The process validator checks:"
echo "    - verdict-triggered reviewer disposal"
echo "    - one verdict per PR/SHA after a grace window"
echo "    - fresh reviewer round after a synthetic new SHA"
echo "    - no author_branch='unknown'"
echo "============================================"
echo ""

set +e
"$EXOMONAD_BIN" init --verbose --session "$SESSION"
INIT_STATUS=$?
set -e

if [[ -f "$RESULT_FILE" ]]; then
    grep -Fxq "Failures: 0" "$RESULT_FILE"
    exit $?
fi

exit "$INIT_STATUS"
