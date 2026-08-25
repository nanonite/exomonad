#!/usr/bin/env bash
set -euo pipefail

# Real-server, real-WASM acceptance for --continue identity preservation
# (chainlink #1019). The fake Codex is only the deterministic leaf command;
# ExoMonad owns the real spawn, MCP, invocation, publication, watcher, ledger,
# and init/restart paths.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

SESSION="e2e-init-continue"
MOCK_PORT="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
MOCK_PID=""

e2e_preflight curl git python3 tmux
e2e_create_work_dir "init-continue"
REMOTE_DIR="$WORK_DIR/remote.git"
MOCK_LOG="$WORK_DIR/mock-forgejo.jsonl"
RESULT_FILE="$WORK_DIR/evidence.json"
FAKE_LOG="$WORK_DIR/fake-codex.log"
git init --bare "$REMOTE_DIR" -q
e2e_init_repo "Exomonad continue E2E" "continue-e2e@example.invalid"
git remote add origin "$MOCK_URL/test-owner/one-shot.git"
git remote set-url --push origin "$REMOTE_DIR"
git push -u origin main -q
e2e_run_exomonad_new
e2e_install_project_wasm_and_roles
git add .gitignore .forgejo
git commit -m "Initialize continue identity fixture" -q

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
yolo = true
poll_interval = 1
inbox_poke_interval = 1
spawn_agent_type = "codex"
forgejo_url = "$MOCK_URL"
forgejo_token = "author-token"
forgejo_reviewer_token = "reviewer-token"

[reviewer]
agent_type = "codex"
EOF
git add .exo/config.toml
git commit -m "Configure continue identity fixture" -q

mkdir -p "$WORK_DIR/bin"
cp "$PROJECT_ROOT/tests/e2e/one-shot-lifecycle/fake-codex.sh" "$WORK_DIR/bin/codex"
chmod +x "$WORK_DIR/bin/codex"
export PATH="$WORK_DIR/bin:$PATH"
export E2E_FAKE_CODEX_LOG="$FAKE_LOG"
export E2E_FAKE_REMOTE_DIR="$REMOTE_DIR"
export E2E_FAKE_MOCK_URL="$MOCK_URL"

cleanup() {
    local code=$?
    set +e
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null
        wait "$MOCK_PID" 2>/dev/null
    fi
    trap - EXIT INT TERM
    (exit "$code")
    e2e_cleanup
}
trap cleanup EXIT INT TERM

e2e_phase "Phase 1" "Starting disposable Forgejo and the real ExoMonad init..."
REMOTE_DIR="$REMOTE_DIR" MOCK_LOG="$MOCK_LOG" \
    python3 "$PROJECT_ROOT/tests/e2e/one-shot-lifecycle/mock_forgejo.py" \
    --port "$MOCK_PORT" >"$WORK_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    curl -fsS "$MOCK_URL/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$MOCK_URL/admin/actions/runners" >/dev/null
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n TL "bash --noprofile --norc"
tmux set-environment -t "$SESSION" PATH "$PATH"
tmux set-environment -t "$SESSION" E2E_FAKE_CODEX_LOG "$FAKE_LOG"
tmux set-environment -t "$SESSION" E2E_MCP_SOCKET "$REPO_DIR/.exo/server.sock"
tmux set-environment -t "$SESSION" E2E_FAKE_REMOTE_DIR "$REMOTE_DIR"
tmux set-environment -t "$SESSION" E2E_FAKE_MOCK_URL "$MOCK_URL"

"$EXOMONAD_BIN" init --start --session "$SESSION" >"$WORK_DIR/init-start.log" 2>&1 || true
for _ in $(seq 1 60); do
    [[ -S "$REPO_DIR/.exo/server.sock" ]] && break
    sleep 0.25
done
[[ -S "$REPO_DIR/.exo/server.sock" ]] || { cat "$WORK_DIR/init-start.log"; e2e_fail "real server socket did not start"; }

e2e_phase "Phase 2" "Publishing a real one-shot PR and restarting with --continue..."
python3 "$SCRIPT_DIR/run.py" "$REPO_DIR" "$EXOMONAD_BIN" "$SESSION" \
    "$REPO_DIR/.exo/server.sock" "$RESULT_FILE" --mutant
test "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ids_preserved"])' "$RESULT_FILE")" = "True"
test "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_dir_archived"])' "$RESULT_FILE")" = "False"
e2e_log "PASS: observed invocation ownership and restart evidence are preserved"
