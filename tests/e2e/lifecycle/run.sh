#!/usr/bin/env bash
set -euo pipefail

# E2E agent lifecycle invariants test.
# Uses a process validator and real ExoMonad hook path, no LLM testrunner.

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
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/lifecycle.XXXXXXXX")"
REPO_DIR="$WORK_DIR/repo"
SERVER_LOG="$WORK_DIR/server.log"
RESULT_FILE="$WORK_DIR/validation-result.txt"

cleanup() {
    local code=$?
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        echo "  Stopped exomonad serve"
    fi
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if [[ -f "$SERVER_LOG" ]]; then
        echo "  Server log tail:"
        tail -n 20 "$SERVER_LOG" | sed 's/^/    /'
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

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git config user.name "Exomonad TL"
git config user.email "tl@example.com"
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
tmux_session = "e2e-lifecycle"
yolo = true
EOF

mkdir -p .exo/agents/lifecycle-worker .exo/agents/lifecycle-tl
python3 - <<'PY'
import json
from pathlib import Path

agents = [
    ("lifecycle-worker", "codex"),
    ("lifecycle-tl", "codex"),
]
for name, agent_type in agents:
    path = Path(".exo/agents") / name / "identity.json"
    path.write_text(json.dumps({
        "agent_name": name,
        "slug": name,
        "agent_type": agent_type,
        "birth_branch": "main",
        "parent_branch": "main",
        "working_dir": ".",
        "display_name": name,
        "topology": "shared_dir",
    }, indent=2) + "\n")
PY

git add .
git commit -q -m "initialize exomonad fixture"

echo "  Work dir: $WORK_DIR"
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

echo ">>> [Phase 3] Running validator..."
"$SCRIPT_DIR/validate.sh" "$REPO_DIR" "$EXOMONAD_BIN" "$RESULT_FILE" "$SERVER_LOG"
