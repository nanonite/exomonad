#!/usr/bin/env bash
set -euo pipefail

# E2E Tangled VM PR Integration Test
# Drives Codex root/TL/dev/reviewer flow against a pre-provisioned Tangled VM.
# This is intentionally separate from tangled-pr-codex, which owns the local
# container/knot relay path.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

required_env() {
    local name="$1"
    if [[ -z "${!name:-}" ]]; then
        echo "ERROR: $name is required" >&2
        exit 1
    fi
}

echo ">>> [Phase 0] Checking VM configuration..."
required_env TANGLED_VM_GIT_REMOTE
required_env TANGLED_VM_KNOT_WS_URL
required_env TANGLED_VM_SPINDLE_WS_URL
required_env TANGLED_VM_OWNER_DID

TANGLED_VM_APPVIEW_URL="${TANGLED_VM_APPVIEW_URL:-}"
TANGLED_VM_REPO_NAME="${TANGLED_VM_REPO_NAME:-exomonad-vm-pr-e2e-$(date +%s)-$$}"
TANGLED_VM_CLEANUP_REMOTE="${TANGLED_VM_CLEANUP_REMOTE:-1}"

EXOMONAD_BIN=""
if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
    export PATH="$PROJECT_ROOT/target/debug:$PATH"
elif command -v exomonad >/dev/null 2>&1; then
    EXOMONAD_BIN="$(command -v exomonad)"
else
    echo "ERROR: exomonad binary not found. Run 'just build'." >&2
    exit 1
fi

for cmd in codex tmux git python3 curl jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: $cmd not found in PATH." >&2
        exit 1
    fi
done

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm >/dev/null 2>&1; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'." >&2
    exit 1
fi

echo "  exomonad: $EXOMONAD_BIN"
echo "  git remote: $TANGLED_VM_GIT_REMOTE"
echo "  knot events: $TANGLED_VM_KNOT_WS_URL"
echo "  spindle events: $TANGLED_VM_SPINDLE_WS_URL"
echo "  appview: ${TANGLED_VM_APPVIEW_URL:-disabled}"

echo ">>> [Phase 1] Creating isolated repo..."
mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/tangled-vm-pr.XXXXXXXX")"
SESSION="e2e-tangled-vm-pr"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REPO_DIR="$WORK_DIR/repo"
CODEX_HOME_DIR="$WORK_DIR/codex-home"
SERVER_LOG="$WORK_DIR/server.log"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [[ "$TANGLED_VM_CLEANUP_REMOTE" == "1" && -d "$REPO_DIR/.git" ]]; then
        if [[ -f "$REPO_DIR/.exo/prs.json" ]]; then
            branch="$(python3 "$SCRIPT_DIR/pr-field.py" "$REPO_DIR/.exo/prs.json" head_branch 2>/dev/null || true)"
            [[ -n "$branch" ]] && git -C "$REPO_DIR" push tangled ":refs/heads/$branch" >/dev/null 2>&1 || true
        fi
    fi
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if [[ -f "$SERVER_LOG" ]]; then
        echo "  Server log tail:"
        tail -n 80 "$SERVER_LOG" | sed 's/^/    /'
    fi
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
}
trap cleanup EXIT

tmux kill-session -t "$SESSION" 2>/dev/null || true
mkdir -p "$REPO_DIR" "$CODEX_HOME_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add tangled "$TANGLED_VM_GIT_REMOTE"
git config user.name "Exomonad Tangled VM E2E"
git config user.email "e2e@example.com"

mkdir -p .tangled/workflows src
cat > README.md <<EOF
# Tangled VM PR E2E Fixture

Repository: $TANGLED_VM_REPO_NAME
EOF
cat > src/hello.py <<'EOF'
def add(a, b):
    return a + b
EOF
cat > src/test_hello.py <<'EOF'
from hello import add

assert add(2, 3) == 5
print("vm pr e2e tests passed")
EOF
cat > .tangled/workflows/ci.yml <<'EOF'
engine: nixery
when:
  - event: [push, pull_request, manual]
    branch: ["*"]
clone:
  depth: 1
  submodules: false
dependencies:
  nixpkgs:
    - python3
steps:
  - name: "Run tests"
    command: "python3 src/test_hello.py"
EOF

git add README.md src/hello.py src/test_hello.py .tangled/workflows/ci.yml
git commit -m "initial Tangled VM PR E2E fixture" -q
GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o StrictHostKeyChecking=no}" git push -u tangled main -q

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: exomonad new failed" >&2
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

ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" <<'PROMPTPY'
import pathlib
import sys
print(pathlib.Path(sys.argv[1]).read_text().replace('"""', '\\"\\"\\"'))
PROMPTPY
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
tangled_knot_url = "$TANGLED_VM_KNOT_WS_URL"
tangled_spindle_url = "$TANGLED_VM_SPINDLE_WS_URL"
tangled_owner_did = "$TANGLED_VM_OWNER_DID"
tangled_appview_url = "$TANGLED_VM_APPVIEW_URL"
initial_prompt = """
$ROOT_PROMPT
"""

[reviewer]
agent_type = "codex"

[[companions]]
name = "tangled-vm-pr-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$SERVER_LOG' '$TANGLED_VM_APPVIEW_URL'"
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
EOF

unset GITHUB_TOKEN
unset GITHUB_API_URL
export CODEX_HOME="$CODEX_HOME_DIR"
export EXOMONAD_LOG_FORMAT=""
export EXOMONAD_SERVER_LOG_FILE="$SERVER_LOG"

echo ">>> [Phase 2] Launching exomonad init..."
echo "============================================"
echo "  E2E Tangled VM PR Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo "  Remote: $TANGLED_VM_GIT_REMOTE"
echo "============================================"

set +e
"$EXOMONAD_BIN" init --verbose --session "$SESSION" --reviewer codex
INIT_STATUS=$?
set -e

if [[ -f "$RESULT_FILE" ]]; then
    grep -Fxq "Failures: 0" "$RESULT_FILE"
    exit $?
fi

exit "$INIT_STATUS"
