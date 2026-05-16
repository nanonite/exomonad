#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink Issue Close Test
# Validates chainlink_issue_close MCP tool:
#   TL creates issue → spawn_worker → worker session_start/work/end + notify_parent
#   → TL closes issue via chainlink_issue_close after worker handoff

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

if ! command -v chainlink &>/dev/null; then
    echo "ERROR: chainlink binary not found in PATH."
    exit 1
fi
echo "  chainlink: $(command -v chainlink)"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

if ! grep -q "chainlink_issue_close" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
    echo "ERROR: chainlink_issue_close not found in WASM binary."
    exit 1
fi
echo "  chainlink_issue_close: FOUND in WASM"
if grep -q "chainlink_agent_init" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
    echo "ERROR: chainlink_agent_init should not be compiled into the devswarm WASM binary."
    exit 1
fi

for cmd in tmux git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  tmux, git: OK"

# --- Phase 1: Create temp environment ---

echo ">>> [Phase 1] Creating temp environment..."

WORK_DIR="$(mktemp -d /tmp/exomonad-e2e-close.XXXXXXXX)"
echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t e2e-chainlink-close 2>/dev/null || true
    rm -rf "$WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q

REPO_DIR="$WORK_DIR/repo"
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git commit --allow-empty -m "initial commit" -q
git push -u origin main -q

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed."
    exit 1
fi

mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done

if ! chainlink init 2>&1 | sed 's/^/  /'; then
    echo "ERROR: chainlink init failed."
    exit 1
fi

# Simple config: Claude TL + testrunner (spawn_worker is built-in, no extra setup)
cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-chainlink-close"
root_agent_type = "claude"
yolo = true
model = "sonnet"

[[companions]]
name = "test-runner"
agent_type = "claude"
role = "testrunner"
model = "haiku"
command = "claude --dangerously-skip-permissions"
task = "Execute the test plan from your role context. Start immediately."
EOF

cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md
mkdir -p .claude/rules
cp "$SCRIPT_DIR/e2e-test.md" .claude/rules/e2e-test.md

echo "  Repo: $REPO_DIR"

# --- Phase 2: Run ---

export GITHUB_TOKEN="test-token-e2e"

echo ""
echo "============================================"
echo "  E2E Chainlink Issue Close Test"
echo "  Session: e2e-chainlink-close"
echo ""
echo "  Chain:"
echo "    Claude TL → chainlink_issue_create"
echo "    → spawn_worker (Gemini, worker role)"
echo "    → worker: session_start → session_work → session_end → notify_parent"
echo "    → TL: chainlink_issue_close after handoff"
echo "    → testrunner validates closed issue and no lock worktree"
echo "============================================"
echo ""

"$EXOMONAD_BIN" init --session e2e-chainlink-close
