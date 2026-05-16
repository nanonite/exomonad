#!/usr/bin/env bash
set -euo pipefail

# E2E Codex Messaging Test
# Validates Codex send_message and notify_parent delivery through tmux routing.

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

if ! command -v codex &>/dev/null; then
    echo "ERROR: codex binary not found in PATH."
    exit 1
fi
echo "  codex: $(command -v codex)"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

for cmd in tmux git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  tmux, git, python3: OK"

echo ">>> [Phase 1] Creating temp environment..."

WORK_DIR="$(mktemp -d /tmp/exomonad-e2e-codex-messaging.XXXXXXXX)"
SESSION="e2e-codex-messaging"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
CODEX_HOME_DIR="$WORK_DIR/codex-home"

echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "  Killed tmux session"
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

tmux kill-session -t "$SESSION" 2>/dev/null || true

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR"
mkdir -p "$CODEX_HOME_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"

cat > README.md <<'EOF'
# Codex Messaging E2E Fixture

This repository is created by tests/e2e/codex-messaging/run.sh.
EOF
git add README.md
git commit -m "initial commit" -q
git push -u origin main -q

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
root_agent_type = "codex"
spawn_agent_type = "codex"
yolo = true
poll_interval = 5
initial_prompt = """
$ROOT_PROMPT
"""

[[companions]]
name = "codex-messaging-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE'"
EOF

if [[ -f "$HOME/.codex/auth.json" ]]; then
    cp -p "$HOME/.codex/auth.json" "$CODEX_HOME_DIR/auth.json"
fi
if [[ -f "$HOME/.codex/installation_id" ]]; then
    cp -p "$HOME/.codex/installation_id" "$CODEX_HOME_DIR/installation_id"
fi

cat > "$CODEX_HOME_DIR/config.toml" <<EOF
[projects."$REPO_DIR"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/codex-messaging-tl-codex"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/codex-messaging-dev-codex"]
trust_level = "trusted"
EOF

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"
echo "  CODEX_HOME: $CODEX_HOME_DIR"

echo ">>> [Phase 2] Configuring environment..."
unset GITHUB_TOKEN
unset GITHUB_API_URL
export CODEX_HOME="$CODEX_HOME_DIR"
export EXOMONAD_LOG_FORMAT=""
echo "  GitHub auth unset"
echo "  Codex config isolated to $CODEX_HOME"

echo ">>> [Phase 3] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Codex Messaging Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  Chain under test:"
echo "    Codex root -> Codex TL"
echo "    Codex TL send_message -> Codex dev leaf"
echo "    Codex TL/dev notify_parent -> parent via tmux"
echo "============================================"
echo ""

set +e
"$EXOMONAD_BIN" init --verbose --session "$SESSION"
INIT_STATUS=$?
set -e

if [[ -f "$RESULT_FILE" ]]; then
    if grep -Fxq "Failures: 0" "$RESULT_FILE"; then
        exit 0
    fi
    exit 1
fi

exit "$INIT_STATUS"
