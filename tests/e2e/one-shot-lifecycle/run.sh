#!/usr/bin/env bash
set -euo pipefail

# E2E one-shot lifecycle test.  The Codex executable is a deterministic fixture;
# ExoMonad still owns the real spawn, invocation, MCP, watcher, and routing paths.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

SESSION="e2e-one-shot-lifecycle"
MOCK_PORT="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
REMOTE_DIR=""
MOCK_PID=""

e2e_preflight curl git python3 tmux
e2e_create_work_dir "one-shot-lifecycle"

cleanup() {
    local code=$?
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
    fi
    MOCK_PID=""
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    trap - EXIT
    (exit "$code")
    e2e_cleanup
}
trap cleanup EXIT INT TERM

e2e_phase "Phase 1" "Creating the scratch repository and Forgejo-compatible mock..."
REMOTE_DIR="$WORK_DIR/remote.git"
MOCK_LOG="$WORK_DIR/mock-forgejo.jsonl"
FAKE_LOG="$WORK_DIR/fake-codex.log"
RESULT_FILE="$WORK_DIR/validation-result.txt"
git init --bare "$REMOTE_DIR" -q
e2e_init_repo "Exomonad one-shot E2E" "one-shot-e2e@example.invalid"
git remote add origin "$MOCK_URL/test-owner/one-shot.git"
git remote set-url --push origin "$REMOTE_DIR"
git push -u origin main -q
e2e_run_exomonad_new
e2e_install_project_wasm_and_roles
git add .gitignore .forgejo
git commit -m "Initialize one-shot lifecycle fixture" -q

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
git add .exo
git commit -m "Configure one-shot lifecycle fixture" -q

mkdir -p "$WORK_DIR/bin"
cp "$SCRIPT_DIR/fake-codex.sh" "$WORK_DIR/bin/codex"
chmod +x "$WORK_DIR/bin/codex"
export PATH="$WORK_DIR/bin:$PATH"
export E2E_FAKE_CODEX_LOG="$FAKE_LOG"
export E2E_MCP_SOCKET="$REPO_DIR/.exo/server.sock"
export E2E_FAKE_REMOTE_DIR="$REMOTE_DIR"
export E2E_FAKE_MOCK_URL="$MOCK_URL"

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n TL "bash --noprofile --norc"
tmux set-environment -t "$SESSION" PATH "$PATH"
tmux set-environment -t "$SESSION" E2E_FAKE_CODEX_LOG "$FAKE_LOG"
tmux set-environment -t "$SESSION" E2E_MCP_SOCKET "$REPO_DIR/.exo/server.sock"
tmux set-environment -t "$SESSION" E2E_FAKE_REMOTE_DIR "$REMOTE_DIR"
tmux set-environment -t "$SESSION" E2E_FAKE_MOCK_URL "$MOCK_URL"

e2e_phase "Phase 2" "Starting the local Forgejo mock and ExoMonad server..."
REMOTE_DIR="$REMOTE_DIR" MOCK_LOG="$MOCK_LOG" \
    python3 "$SCRIPT_DIR/mock_forgejo.py" --port "$MOCK_PORT" >"$WORK_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    if curl -fsS "$MOCK_URL/admin/actions/runners" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
curl -fsS "$MOCK_URL/admin/actions/runners" >/dev/null
e2e_start_server "EXOMONAD_TMUX_SESSION=$SESSION"

e2e_phase "Phase 3" "Running Codex one-shot, watcher, inbox, pane, and resume assertions..."
python3 "$SCRIPT_DIR/validate.py" \
    "$REPO_DIR" "$REPO_DIR/.exo/server.sock" "$MOCK_URL" "$FAKE_LOG" "$RESULT_FILE"

echo ">>> PASS: one-shot lifecycle E2E"
