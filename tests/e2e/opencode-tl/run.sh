#!/usr/bin/env bash
set -euo pipefail

# E2E OpenCode TL Test
# Validates full ACP delivery chain: exomonad init → opencode serve → port captured →
# opencode run --attach delivers initial_prompt → OpenCode uses MCP →
# notify_parent reaches testrunner via Teams inbox.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

# --- Phase 0: Preconditions ---

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

if ! command -v opencode &>/dev/null; then
    echo "ERROR: opencode binary not found in PATH."
    exit 1
fi
echo "  opencode: $(command -v opencode)"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

for cmd in tmux git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  tmux, git: OK"

# --- Phase 1: Create temp environment ---

echo ">>> [Phase 1] Creating temp environment..."

WORK_DIR="$(mktemp -d /tmp/exomonad-e2e-oct.XXXXXXXX)"
echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t e2e-opencode-tl 2>/dev/null || true
    echo "  Killed tmux session"
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

# Create bare remote
REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q

# Create working repo
REPO_DIR="$WORK_DIR/repo"
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git commit --allow-empty -m "initial commit" -q
git push -u origin main -q

# Bootstrap via exomonad new
if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

# Symlink WASM from project
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done

# Write config: OpenCode root TL + Claude haiku testrunner companion
cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-opencode-tl"
root_agent_type = "opencode"
yolo = true
initial_prompt = "You are an E2E test subject. Do exactly these two steps and nothing else: (1) Write a file named opencode-tl-test.txt in the current directory containing the single line: OpenCode TL test passed (2) Call the send_message MCP tool with target_name='test-runner' and message='[OC-TL-DONE] OpenCode root TL test complete. File written successfully.' Then stop."

[opencode]
tl_model = "opencode-go/deepseek-v4-flash"
use_embedded_key = true

[[companions]]
name = "test-runner"
agent_type = "claude"
role = "testrunner"
model = "haiku"
command = "claude --dangerously-skip-permissions"
task = "Execute the test plan from your role context. Start immediately."
EOF

# Copy testrunner context into role
mkdir -p .exo/roles/devswarm/context
cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md

# e2e-test.md is informational for the testrunner companion (OpenCode doesn't use .claude/rules)
mkdir -p .claude/rules
cp "$SCRIPT_DIR/e2e-test.md" .claude/rules/e2e-test.md

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"

# --- Phase 2: Set environment ---

echo ">>> [Phase 2] Configuring environment..."
export GITHUB_TOKEN="test-token-e2e"
echo "  GITHUB_TOKEN=test-token-e2e"

# --- Phase 3: Run exomonad init ---

echo ">>> [Phase 3] Launching exomonad init..."

echo ""
echo "============================================"
echo "  E2E OpenCode TL Test Ready"
echo "  Session: e2e-opencode-tl"
echo "  Work dir: $WORK_DIR/repo"
echo ""
echo "  Chain under test:"
echo "    exomonad serve starts opencode"
echo "    → ACP port captured"
echo "    → initial_prompt delivered via opencode run --attach"
echo "    → OpenCode writes opencode-tl-test.txt"
echo "    → notify_parent → Teams inbox → testrunner"
echo "============================================"
echo ""

"$EXOMONAD_BIN" init --session e2e-opencode-tl
