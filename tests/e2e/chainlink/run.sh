#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink Issue Create Test
# Validates chainlink_issue_create MCP tool:
#   TL agent calls chainlink_issue_create → shells out to `chainlink create` via ProcessRun
#   → issue created in chainlink DB → TL writes result → testrunner validates.

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
if grep -q "Chainlink Worker Protocol" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
    echo "  workerProfileText injection: VERIFIED (chainlink protocol found in WASM binary)"
else
    echo "WARNING: 'Chainlink Worker Protocol' not found in WASM binary. workerProfileText may be missing the chainlink protocol."
fi

# Check chainlink tool names are compiled into WASM binary (TL + worker tools)
MISSING_TOOLS=()
for tool in chainlink_issue_create chainlink_issue_list chainlink_issue_block chainlink_issue_close chainlink_session_end chainlink_milestone_create chainlink_sync; do
    if grep -q "$tool" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null; then
        echo "  chainlink tool '$tool': FOUND"
    else
        echo "  chainlink tool '$tool': MISSING"
        MISSING_TOOLS+=("$tool")
    fi
done
if [ ${#MISSING_TOOLS[@]} -gt 0 ]; then
    echo "ERROR: Missing chainlink tools in WASM binary: ${MISSING_TOOLS[*]}"
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

WORK_DIR="$(mktemp -d /tmp/exomonad-e2e-chainlink.XXXXXXXX)"
echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t e2e-chainlink 2>/dev/null || true
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

# Copy chainlink context files
mkdir -p .exo/roles/devswarm/context
cp "$PROJECT_ROOT/.exo/roles/devswarm/context/chainlink-tl.md" .exo/roles/devswarm/context/ 2>/dev/null || true
cp "$PROJECT_ROOT/.exo/roles/devswarm/context/chainlink-worker.md" .exo/roles/devswarm/context/ 2>/dev/null || true

# Write config: TL agent + testrunner companion
cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-chainlink"
root_agent_type = "claude"
yolo = true
initial_prompt = "You are an E2E test subject. Do these steps and nothing else: (1) Call the chainlink_issue_create MCP tool with title='E2E chainlink test issue' and priority='low'. (2) Write the returned issue ID to a file named chainlink-e2e-result.txt in the current directory. (3) Call the send_message MCP tool with target_name='test-runner' and message='[CHAINLINK-E2E-DONE] chainlink_issue_create returned issue ID: <replace with actual ID>'. Then stop."
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

# e2e-test.md for the testrunner companion
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
echo "  E2E Chainlink Issue Create Test"
echo "  Session: e2e-chainlink"
echo "  Work dir: $WORK_DIR/repo"
echo ""
echo "  Chain under test:"
echo "    exomonad init → TL agent starts"
echo "    → TL calls chainlink_issue_create(title='E2E chainlink test issue')"
echo "    → ProcessRun shells out to 'chainlink create ...'"
echo "    → TL writes result to chainlink-e2e-result.txt"
echo "    → send_message → Teams inbox → testrunner validates"
echo "============================================"
echo ""

"$EXOMONAD_BIN" init --session e2e-chainlink
