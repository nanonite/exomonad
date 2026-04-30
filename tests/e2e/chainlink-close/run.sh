#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink Issue Close Test
# Validates chainlink_issue_close MCP tool:
#   TL agent creates an issue, claims it, calls chainlink_issue_close
#   → close atomically releases locks, closes issue, ends session, notifies parent
#   → testrunner validates chainlink state + notification

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

# Check chainlink_issue_close tool is compiled into WASM
if grep -q "chainlink_issue_close" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
    echo "  chainlink_issue_close: FOUND in WASM"
else
    echo "ERROR: chainlink_issue_close not found in WASM binary."
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

# Init chainlink in test repo
if ! chainlink init 2>&1 | sed 's/^/  /'; then
    echo "ERROR: chainlink init failed."
    exit 1
fi

# Write config: TL agent + testrunner companion
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

# Copy testrunner context into role
cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md

# Root TL rules
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
echo "  E2E Chainlink Issue Close Test"
echo "  Session: e2e-chainlink-close"
echo "  Work dir: $WORK_DIR/repo"
echo ""
echo "  Chain under test:"
echo "    exomonad init → TL agent starts"
echo "    → TL calls chainlink_issue_create(title='E2E close test')"
echo "    → TL claims issue (bash agent init + MCP session work)"
echo "    → TL calls chainlink_issue_close"
echo "    → TL writes result to chainlink-close-result.txt"
echo "    → send_message → Teams inbox → testrunner validates"
echo "============================================"
echo ""

"$EXOMONAD_BIN" init --session e2e-chainlink-close
