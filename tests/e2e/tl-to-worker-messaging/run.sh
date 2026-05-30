#!/usr/bin/env bash
set -euo pipefail

# E2E TL-to-Worker Messaging Test
# Validates Codex TL -> OpenCode spawn_worker pane tmux messaging round-trip.

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

for cmd in codex opencode tmux git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  codex: $(command -v codex)"
echo "  opencode: $(command -v opencode)"
echo "  tmux, git, python3: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

for tool in spawn_worker send_tmux_message notify_parent fork_wave; do
    if grep -q "$tool" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
        echo "  MCP tool '$tool': FOUND"
    else
        echo "ERROR: MCP tool '$tool' missing from WASM binary."
        exit 1
    fi
done

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/tl-to-worker-messaging.XXXXXXXX")"
SESSION="e2e-tl-to-worker-messaging"
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
    if [[ "${KEEP_E2E_WORKDIR:-0}" == "1" ]]; then
        echo "  Keeping $WORK_DIR"
    else
        rm -rf "$WORK_DIR"
        echo "  Removed $WORK_DIR"
    fi
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
# TL-to-Worker Messaging E2E Fixture

This repository is created by tests/e2e/tl-to-worker-messaging/run.sh.
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
print(value.replace('"""', '\"\"\"'))
PY
)"

VALIDATOR_WRAPPER="$WORK_DIR/run-validator.sh"
cat > "$VALIDATOR_WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$SCRIPT_DIR/validate.sh" "$REPO_DIR" "$SESSION" "$RESULT_FILE"
EOF
chmod +x "$VALIDATOR_WRAPPER"

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "codex"
spawn_agent_type = "codex"
model = "gpt-5.4-mini"
yolo = true
poll_interval = 5
initial_prompt = """
$ROOT_PROMPT
"""

[opencode]
worker_model = "opencode-go/deepseek-v4-flash"
use_embedded_key = true

[[companions]]
name = "tl-to-worker-validator"
agent_type = "process"
command = "$VALIDATOR_WRAPPER"
EOF

git add .gitignore .forgejo .exo
git commit -m "initialize exomonad fixture" -q
git push -q

if [[ -f "$HOME/.codex/auth.json" ]]; then
    cp -p "$HOME/.codex/auth.json" "$CODEX_HOME_DIR/auth.json"
fi
if [[ -f "$HOME/.codex/installation_id" ]]; then
    cp -p "$HOME/.codex/installation_id" "$CODEX_HOME_DIR/installation_id"
fi

cat > "$CODEX_HOME_DIR/config.toml" <<EOF
[projects."$REPO_DIR"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/tl-to-worker-messaging-tl-codex"]
trust_level = "trusted"
EOF

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"
echo "  CODEX_HOME: $CODEX_HOME_DIR"

echo ">>> [Phase 2] Configuring environment..."
unset FORGEJO_TOKEN
unset FORGEJO_API_URL
export CODEX_HOME="$CODEX_HOME_DIR"
export EXOMONAD_LOG_FORMAT=""
echo "  GitHub auth unset"
echo "  Codex config isolated to $CODEX_HOME"

echo ">>> [Phase 3] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E TL-to-Worker Messaging Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  Chain under test:"
echo "    Codex root -> Codex TL"
echo "    Codex TL -> spawn_worker(agent_type=opencode)"
echo "    Codex TL -> send_tmux_message -> OpenCode worker pane"
echo "    OpenCode worker -> notify_parent -> Codex TL"
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
