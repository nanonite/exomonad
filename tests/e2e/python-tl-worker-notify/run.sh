#!/usr/bin/env bash
set -euo pipefail

# E2E Worker Notify Test
# The Python TL controller consumes .exo/tl-loop/plan.json and dispatches one
# Codex worker into a pane of its own TL window. The worker's notify_parent must
# reach the controller's window, the child must get a role-correct Codex config
# with trusted hooks in the isolated CODEX_HOME, and the run must reach a durable
# terminal phase.
#
# There is no interactive Codex root TL and no TL prompt: root_agent_type is
# ignored by init and initial_prompt, if set, must be a JSON WorkPlan.
#
# Renamed from the sub-TL worker notify scenario: there is no sub-TL in the
# shipped architecture, the root controller is the Python process.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
# shellcheck source=../lib/git-fixture.sh
source "$PROJECT_ROOT/tests/e2e/lib/git-fixture.sh"
# shellcheck source=../lib/codex-home.sh
source "$PROJECT_ROOT/tests/e2e/lib/codex-home.sh"
# shellcheck source=../lib/python-tl.sh
source "$PROJECT_ROOT/tests/e2e/lib/python-tl.sh"

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

for cmd in codex tmux git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  codex: $(command -v codex)"
echo "  tmux, git, python3: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

python3 -c "import tomllib" 2>/dev/null || {
    echo "ERROR: python3 tomllib not available (need Python 3.11+)."
    exit 1
}

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/python-tl-worker-notify.XXXXXXXX")"
e2e_git_use_fixture_root "$WORK_DIR"
SESSION="e2e-python-tl-worker-notify"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"

echo "  Work dir: $WORK_DIR"

cleanup() {
    local code=$?
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "  Killed tmux session"
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if ! e2e_codex_assert_home_is_run_scoped; then
        code=1
    fi
    e2e_codex_remove_isolated_home
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

tmux kill-session -t "$SESSION" 2>/dev/null || true

# Isolate Codex before any ExoMonad process starts, and seed the two auth
# artifacts a live `codex` process needs to reach the model.
e2e_isolate_codex_home
e2e_copy_codex_auth

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
# `origin` is an HTTP URL because the controller resolves repository identity at
# startup and refuses a local-path remote; pushes still go to the local bare
# repository, so the scenario stays hermetic and local-only.
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"

cat > README.md <<'EOF'
# Python TL Worker Notify E2E Fixture

This repository is created by tests/e2e/python-tl-worker-notify/run.sh.
EOF
git add README.md
git commit -m "initial commit" -q
e2e_python_tl_configure_remote "$REPO_DIR" "$REMOTE_DIR" "python-tl-worker-notify"

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

# The controller opens the project's Chainlink database during startup, so it
# must exist before `init` starts the TL window.
e2e_python_tl_init_chainlink "$REPO_DIR"

# `exomonad new` scaffolds a 120000-token worker ceiling. The controller
# attributes a role's whole share of the run budget to that role until it has
# recorded per-role spend, so a plan whose run budget meets the scaffolded
# ceiling parks its only slice with `over_budget` before dispatching anything.
# Derive the ceilings from the plan instead.
e2e_python_tl_write_harness_policy "$REPO_DIR" "$SCRIPT_DIR/plan.json"

# The controller's only input is plan.json. Copy the scenario plan verbatim so
# the plan the validator reads is the plan in version control.
mkdir -p .exo/tl-loop
cp "$SCRIPT_DIR/plan.json" .exo/tl-loop/plan.json
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' .exo/tl-loop/plan.json \
    || { echo "ERROR: plan.json is not valid JSON."; exit 1; }

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
spawn_agent_type = "codex"
yolo = true
poll_interval = 5

[[companions]]
name = "python-tl-worker-notify-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$CODEX_HOME'"
EOF

# `spawn_worker` refuses a dirty worktree, and `init` writes `.mcp.json` and
# `.claude/rules/exomonad.md` after this point. Ignore and commit them so the
# controller can actually dispatch its first worker.
e2e_python_tl_commit_scaffold "$REPO_DIR" "Configure worker notify fixture for the Python TL controller"

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"
echo "  CODEX_HOME: $CODEX_HOME"

echo ">>> [Phase 2] Configuring environment..."
unset FORGEJO_TOKEN
unset FORGEJO_API_URL
e2e_codex_assert_home_is_run_scoped
export EXOMONAD_LOG_FORMAT=""
echo "  Forgejo auth unset"
echo "  Codex config isolated to $CODEX_HOME"

echo ">>> [Phase 3] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Worker Notify Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  Chain under test:"
echo "    Python TL controller (TL window, pane 0)"
echo "    controller -> Codex worker pane in the same window"
echo "    Codex worker notify_parent -> controller"
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
