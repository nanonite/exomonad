#!/usr/bin/env bash
set -euo pipefail

# E2E Messaging Test
# Validates Teams inbox message delivery through the full MCP stack.
# Simpler than the full E2E test — no mock GitHub, no spawn/merge pipeline.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

# --- Phase 0: Preconditions ---

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

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/msg.XXXXXXXX")"
echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t e2e-messaging 2>/dev/null || true
    echo "  Killed tmux session"
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

# Create bare remote for push/fetch
REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q

# Create working repo
REPO_DIR="$WORK_DIR/repo"
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"

# Configure local Git identity
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"

# Initial commit + push
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

# Patch config: use bash instead of nix develop
if [[ -f .exo/config.toml ]]; then
    if grep -q 'shell_command' .exo/config.toml; then
        sed -i 's|^shell_command.*|shell_command = "bash"|' .exo/config.toml
    else
        echo 'shell_command = "bash"' >> .exo/config.toml
    fi
else
    cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
EOF
fi

# Messaging test config — no mock GitHub, no poll_interval needed
cat >> .exo/config.toml <<EOF
tmux_session = "e2e-messaging"
model = "haiku"
yolo = true

[[companions]]
name = "test-runner"
agent_type = "claude"
role = "testrunner"
model = "haiku"
command = "claude --dangerously-skip-permissions"
task = "Execute the test plan from your role context. Start immediately."
EOF

# Copy messaging-specific testrunner context into the role
# The companion gets its role context from .exo/roles/devswarm/context/testrunner.md
# We override it with the messaging-specific plan
mkdir -p .exo/roles/devswarm/context
cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md

# Create messaging-specific TL rule
mkdir -p .claude/rules
cp "$SCRIPT_DIR/e2e-test.md" .claude/rules/e2e-test.md

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"

# --- Phase 2: Set environment ---

echo ">>> [Phase 2] Configuring environment..."

# Messaging test doesn't need mock GitHub, but set token to avoid auth errors
export GITHUB_TOKEN="test-token-e2e"

echo "  GITHUB_TOKEN=test-token-e2e"

# --- Phase 3: Run exomonad init ---

echo ">>> [Phase 3] Launching exomonad init..."

echo ""
echo "============================================"
echo "  E2E Messaging Test Ready"
echo "  Session: e2e-messaging"
echo "  Work dir: $WORK_DIR/repo"
echo ""
echo "  Test runner will send 4 messages via MCP"
echo "  and validate Teams inbox delivery."
echo "============================================"
echo ""

# Launch exomonad init — creates tmux session and attaches.
"$EXOMONAD_BIN" init --session e2e-messaging
