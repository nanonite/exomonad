#!/usr/bin/env bash
set -euo pipefail

# E2E Reviewer Convergence Loop Test
#
# Verifies the dispatch fan-out introduced by chainlink #249/#250: when a
# leaf pushes fixes after a reviewer's ChangesRequested, the watcher must
# fan the resulting fixes_pushed event out to BOTH the leaf and the
# reviewer's plugin manager. Without that fan-out the reviewer never
# re-reviews and the convergence loop stalls indefinitely.
#
# Flow exercised by the harness:
#   1. Codex TL spawns a Codex dev-leaf with a trivial PR-able task.
#   2. Leaf opens a PR via file_pr (local PR registry, no GitHub).
#   3. Watcher auto-spawns a reviewer for the PR; PrEntry gets reviewer_*
#      fields populated (subissue #248).
#   4. Testrunner (Claude companion) mutates .exo/prs.json to inject a
#      LocalReviewState::ChangesRequested for the PR. On the next poll
#      cycle the watcher sees the state.
#   5. Testrunner instructs the leaf (via send_message) to push a trivial
#      fix commit, bumping the head SHA.
#   6. Watcher detects SHA change after ChangesRequested → fires
#      fixes_pushed → reviewer_fanout_decision returns DispatchTo →
#      call_handle_event is invoked for the reviewer.
#   7. Testrunner inspects the exomonad server log for the canonical
#      "Fanning out pr_review event to reviewer agent" info line
#      AND the reviewer's tmux pane for evidence of the FixesPushed
#      handler firing. Both must be present.
#   8. Testrunner notify_parent with status=success / status=failure.
#   9. validate.sh records the testrunner verdict into RESULT_FILE.

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
    echo "ERROR: exomonad binary not found. Run 'just install-all-dev' or 'just build'."
    exit 1
fi
echo "  exomonad: $EXOMONAD_BIN"

for cmd in codex claude chainlink tmux git python3 jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  codex / claude / chainlink / tmux / git / python3 / jq: OK"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

echo ">>> [Phase 1] Creating temp environment..."

WORK_DIR="$(mktemp -d /tmp/exomonad-e2e-reviewer-convergence.XXXXXXXX)"
SESSION="e2e-reviewer-convergence"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
CODEX_HOME_DIR="$WORK_DIR/codex-home"
SERVER_LOG="$WORK_DIR/server.log"

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
    if [[ -f "$SERVER_LOG" ]]; then
        echo "  Last 30 server log lines:"
        tail -n 30 "$SERVER_LOG" | sed 's/^/    /'
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

cat > README.md <<'EOF'
# Reviewer Convergence Loop E2E Fixture

Repository created by tests/e2e/reviewer-convergence-loop/run.sh.
EOF
git add README.md
git commit -m "initial commit" -q
git push -u origin main -q

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

if ! chainlink init 2>&1 | sed 's/^/  /'; then
    echo "ERROR: chainlink init failed during E2E setup."
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

ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).read_text().replace('"""', '\\"\\"\\"'))
PY
)"

TESTRUNNER_TASK="$(python3 - "$SCRIPT_DIR/testrunner.md" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).read_text().replace('"""', '\\"\\"\\"'))
PY
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
initial_prompt = """
$ROOT_PROMPT
"""

[reviewer]
agent_type = "codex"

[[companions]]
name = "convergence-testrunner"
agent_type = "claude"
role = "testrunner"
command = "claude --dangerously-skip-permissions"
task = """
$TESTRUNNER_TASK
"""
model = "haiku"

[[companions]]
name = "convergence-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$SERVER_LOG'"
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

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"
echo "  Result: $RESULT_FILE"
echo "  Server log: $SERVER_LOG"
echo "  CODEX_HOME: $CODEX_HOME_DIR"

echo ">>> [Phase 2] Configuring environment..."
unset GITHUB_TOKEN
unset GITHUB_API_URL
export CODEX_HOME="$CODEX_HOME_DIR"
export EXOMONAD_LOG_FORMAT=""
# Route exomonad server stderr to a known file so the testrunner + validator
# can grep it for the canonical fan-out info line.
export EXOMONAD_SERVER_LOG_FILE="$SERVER_LOG"
echo "  GitHub auth unset"
echo "  Codex config isolated to $CODEX_HOME"
echo "  Server log: $EXOMONAD_SERVER_LOG_FILE"

echo ">>> [Phase 3] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Reviewer Convergence Loop Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo ""
echo "  Chain under test:"
echo "    Codex root TL -> spawn_leaf(codex)"
echo "    Codex leaf    -> file_pr (local registry)"
echo "    watcher       -> spawn_reviewer_for_pr (Codex)"
echo "    testrunner    -> inject ChangesRequested into .exo/prs.json"
echo "    leaf          -> push fix commit (SHA bump)"
echo "    watcher       -> fixes_pushed -> reviewer_fanout_decision"
echo "                  -> call_handle_event(reviewer_branch, ...)"
echo "    testrunner    -> assert server log + reviewer pane evidence"
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
