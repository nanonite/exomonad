#!/usr/bin/env bash
set -euo pipefail

# E2E Recursive Fork Wave Test
# Validates root -> sub-TL -> worker recursion and notify_parent routing for one
# runtime. Use run-all.sh for the Claude/Codex/OpenCode matrix.

RUNTIME="${1:-codex}"
case "$RUNTIME" in
    claude|codex|opencode) ;;
    *) echo "ERROR: runtime must be claude, codex, or opencode"; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

SESSION="e2e-recursive-$RUNTIME"
FORK_SESSION="false"
if [[ "$RUNTIME" == "claude" ]]; then
    FORK_SESSION="true"
fi

echo ">>> [Phase 0] Checking preconditions for $RUNTIME..."

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

for cmd in tmux git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
if ! command -v "$RUNTIME" &>/dev/null; then
    echo "ERROR: $RUNTIME binary not found in PATH."
    exit 1
fi
echo "  $RUNTIME: $(command -v "$RUNTIME")"
echo "  tmux, git, python3: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/recursive-$RUNTIME.XXXXXXXX")"
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

cat > README.md <<EOF
# Recursive Fork Wave E2E Fixture

Runtime under test: $RUNTIME
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

ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" "$RUNTIME" "$FORK_SESSION" <<'PY'
import pathlib
import sys

template = pathlib.Path(sys.argv[1]).read_text()
runtime = sys.argv[2]
fork_session = sys.argv[3]
value = template.replace('__RUNTIME__', runtime).replace('__FORK_SESSION__', fork_session)
print(value.replace('"""', '\"\"\"'))
PY
)"

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "$RUNTIME"
spawn_agent_type = "$RUNTIME"
yolo = true
poll_interval = 5
initial_prompt = """
$ROOT_PROMPT
"""

[[companions]]
name = "recursive-fork-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$RUNTIME'"
EOF

if [[ "$RUNTIME" == "opencode" ]]; then
    cat >> .exo/config.toml <<'EOF'

[opencode]
worker_model = "opencode-go/deepseek-v4-flash"
use_embedded_key = true
EOF
fi

if [[ "$RUNTIME" == "codex" ]]; then
    if [[ -f "$HOME/.codex/auth.json" ]]; then
        cp -p "$HOME/.codex/auth.json" "$CODEX_HOME_DIR/auth.json"
    fi
    if [[ -f "$HOME/.codex/installation_id" ]]; then
        cp -p "$HOME/.codex/installation_id" "$CODEX_HOME_DIR/installation_id"
    fi
    cat > "$CODEX_HOME_DIR/config.toml" <<EOF
[projects."$REPO_DIR"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/recursive-subtl-codex"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/agents/recursive-worker-codex"]
trust_level = "trusted"
EOF
    export CODEX_HOME="$CODEX_HOME_DIR"
fi

trust_claude_project() {
    local project_path="$1"
    python3 - "$project_path" <<'PY'
import json
import sys
from pathlib import Path

project = str(Path(sys.argv[1]).resolve())
claude_json = Path.home() / ".claude.json"
try:
    data = json.loads(claude_json.read_text()) if claude_json.exists() else {}
except json.JSONDecodeError:
    data = {}
projects = data.setdefault("projects", {})
entry = projects.setdefault(project, {})
entry.setdefault("allowedTools", [])
entry.setdefault("disabledMcpjsonServers", [])
entry.setdefault("enabledMcpjsonServers", [])
entry.setdefault("exampleFiles", [])
entry["hasClaudeMdExternalIncludesApproved"] = False
entry["hasClaudeMdExternalIncludesWarningShown"] = False
entry["hasTrustDialogAccepted"] = True
entry.setdefault("mcpContextUris", [])
entry.setdefault("mcpServers", {})
entry.setdefault("projectOnboardingSeenCount", 0)
entry["hasCompletedProjectOnboarding"] = True
claude_json.parent.mkdir(parents=True, exist_ok=True)
tmp_path = claude_json.with_suffix(claude_json.suffix + ".tmp")
tmp_path.write_text(json.dumps(data, indent=2) + "
")
tmp_path.replace(claude_json)
PY
}

if [[ "$RUNTIME" == "claude" ]]; then
    trust_claude_project "$REPO_DIR"
    trust_claude_project "$REPO_DIR/.exo/worktrees/recursive-subtl-claude"
fi

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"

echo ">>> [Phase 2] Configuring environment..."
unset FORGEJO_TOKEN
unset FORGEJO_API_URL
export EXOMONAD_LOG_FORMAT=""
echo "  Forgejo auth unset"
if [[ "$RUNTIME" == "codex" ]]; then
    echo "  CODEX_HOME: $CODEX_HOME"
fi

echo ">>> [Phase 3] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Recursive Fork Wave Test Ready"
echo "  Runtime: $RUNTIME"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  Chain under test:"
echo "    $RUNTIME root -> $RUNTIME recursive sub-TL"
echo "    $RUNTIME recursive sub-TL -> $RUNTIME worker pane"
echo "    worker notify_parent -> sub-TL"
echo "    sub-TL notify_parent -> root"
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
