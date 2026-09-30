#!/usr/bin/env bash
set -euo pipefail

# E2E Chainlink Codex Test
# The Python TL controller consumes .exo/tl-loop/plan.json and dispatches one
# Codex worker. The worker runs the Chainlink session workflow that the worker
# role is granted (session_start, session_work, issue_comment, session_end) on
# an issue it does not own, then notifies the controller.
#
# There is no interactive Codex root TL and no TL prompt: root_agent_type is
# ignored by init and initial_prompt, if set, must be a JSON WorkPlan. The
# issue owner is the harness, because the controller holds no Chainlink
# authority and the worker role cannot create or close issues.

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

if ! command -v codex &>/dev/null; then
    echo "ERROR: codex binary not found in PATH."
    exit 1
fi
echo "  codex: $(command -v codex)"

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

for cmd in tmux git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  tmux, git, python3: OK"

python3 -c "import tomllib" 2>/dev/null || {
    echo "ERROR: python3 tomllib not available (need Python 3.11+)."
    exit 1
}

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/chainlink-codex.XXXXXXXX")"
e2e_git_use_fixture_root "$WORK_DIR"
SESSION="e2e-chainlink-codex"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
ISSUE_TITLE="E2E chainlink codex worker"
CHAINLINK_DB="$REPO_DIR/.chainlink"
export CHAINLINK_DB

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

# Own tmux server for this run, so the server's captured environment is this
# run's. This has to happen before the first `tmux` call below, and before
# `exomonad init` starts its server. See the helper for why CODEX_HOME
# depends on it.
e2e_python_tl_isolate_tmux_server "$WORK_DIR"

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
# Chainlink Codex E2E Fixture

This repository is created by tests/e2e/chainlink-codex/run.sh.
EOF
git add README.md
git commit -m "initial commit" -q
e2e_python_tl_configure_remote "$REPO_DIR" "$REMOTE_DIR" "chainlink-codex"

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

# Prove the account can run the model this fixture just provisioned, before the
# controller starts. A rejected model surfaces only inside the worker's rollout
# as a 400 on its first inference, after dispatch and provisioning, and nothing
# notices for the validator's whole budget -- so without this the run looks like
# a notify_parent stall. Chainlink #1149.
e2e_python_tl_assert_codex_model_runnable "$(e2e_python_tl_codex_model)" "$REPO_DIR"

# The harness is the issue owner. The controller holds no Chainlink authority
# and the worker role is granted neither create nor close, so the dispatched
# Codex child can only comment on and own a session for this issue.
# `issue create` prints the id on stdout rather than JSON, so parse that. Only
# one call, or the fixture would end up with a second stray issue.
ISSUE_ID="$(chainlink issue create "$ISSUE_TITLE" -p low -l e2e -l chainlink -l codex \
    | sed -n 's/.*#\([0-9][0-9]*\).*/\1/p' | head -n 1)"
if [[ -z "${ISSUE_ID:-}" ]]; then
    echo "ERROR: could not determine the Chainlink issue id."
    exit 1
fi
echo "  Chainlink issue: #$ISSUE_ID ($ISSUE_TITLE)"

# The controller's only input is plan.json, and the worker's task has to name
# the issue it is allowed to work on, so the single placeholder is rendered
# with the id the harness just created.
mkdir -p .exo/tl-loop
ISSUE_ID="$ISSUE_ID" python3 - "$SCRIPT_DIR/plan.json" .exo/tl-loop/plan.json <<'PY'
import os
import pathlib
import sys

placeholder = "{{CHAINLINK_ISSUE_ID}}"
plan = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
if plan.count(placeholder) != 1:
    raise SystemExit(
        f"plan.json must contain exactly one {placeholder} placeholder, "
        f"found {plan.count(placeholder)}"
    )
rendered = plan.replace(placeholder, os.environ["ISSUE_ID"])
if "{{" in rendered or "}}" in rendered:
    raise SystemExit("rendered plan.json still contains an unrendered placeholder")
pathlib.Path(sys.argv[2]).write_text(rendered, encoding="utf-8")
PY
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' .exo/tl-loop/plan.json \
    || { echo "ERROR: rendered plan.json is not valid JSON."; exit 1; }

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
spawn_agent_type = "codex"
yolo = true
poll_interval = 5

[[companions]]
name = "chainlink-codex-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$CODEX_HOME' '$ISSUE_ID'"
EOF

# `spawn_worker` refuses a dirty worktree, and `init` writes `.mcp.json` and
# `.claude/rules/exomonad.md` after this point. Ignore and commit them so the
# controller can actually dispatch its first worker.
e2e_python_tl_commit_scaffold "$REPO_DIR" "Configure Chainlink Codex fixture for the Python TL controller"

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
echo "  E2E Chainlink Codex Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo "  Issue: #$ISSUE_ID"
echo ""
echo "  Chain under test:"
echo "    harness (operator) owns the Chainlink issue"
echo "    Python TL controller -> Codex worker"
echo "    worker Chainlink session workflow on a foreign issue"
echo "    worker notify_parent -> controller"
echo "    the issue must still be open afterwards"
echo "============================================"
echo ""

set +e
"$EXOMONAD_BIN" init --verbose --session "$SESSION"
INIT_STATUS=$?
set -e

# The validator's verdict is the only thing that decides this scenario. `init`
# exits 0 as soon as it attaches the session, long before the controller reaches
# a terminal state, so falling back to INIT_STATUS when the result file is
# absent would report a pass for a run that proved nothing. A missing result
# file is itself a failure: the validator never reported.
if [[ ! -f "$RESULT_FILE" ]]; then
    printf 'ERROR: validator wrote no result file at %s (init exited %d)\n' \
        "$RESULT_FILE" "$INIT_STATUS" >&2
    exit 1
fi

if ! grep -Fxq "Failures: 0" "$RESULT_FILE"; then
    printf 'ERROR: validator reported failures:\n' >&2
    sed 's/^/  /' "$RESULT_FILE" >&2
    exit 1
fi

exit 0
