#!/usr/bin/env bash
set -euo pipefail

EXOMONAD_BIN="${1:?binary path required}"
FAST_DIR="${2:?fixture directory required}"
FAST_HOME="${3:?isolated home required}"
CODEX_HOME="${4:?Codex home required}"
SOURCE_ROOT="${5:?source workspace required}"
WASM_DIR="${6:?fresh WASM directory required}"
POLICY_REPO="${7:?policy fixture workspace required}"
MOCK_URL="${8:?mock Forgejo URL required}"
FAST_EXEC_DIR=""
FAST_FAKE_BIN=""
FAST_SHELL=""
FAST_TEST_PATH=""
REMOTE_DIR="$FAST_DIR/remote/owner/repo.git"
FAST_REPO="$FAST_DIR/repo"
FAST_SESSION="e2e-init-fast-handoff"
FAST_BOOTSTRAP_SESSION="exo1290-fast-bootstrap"
FAST_AGENT_LOG="$FAST_DIR/agent-events.jsonl"
FAST_SERVER_PID=""

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
    local status=$?
    set +e
    if (( status != 0 )); then
        mkdir -p "$FAST_DIR/tmux-failure-evidence"
        tmux list-panes -a -F '#{session_name} #{window_name} #{pane_id} #{pane_pid} #{pane_current_command} #{pane_dead}' \
            > "$FAST_DIR/tmux-failure-evidence/panes.txt" 2>&1
        while IFS= read -r pane_id; do
            [[ -n "$pane_id" ]] || continue
            tmux capture-pane -p -t "$pane_id" -S -120 \
                > "$FAST_DIR/tmux-failure-evidence/pane-${pane_id//%/}.txt" 2>&1
        done < <(tmux list-panes -a -F '#{pane_id}' 2>/dev/null)
    fi
    tmux kill-server 2>/dev/null
    if [[ -n "$FAST_SERVER_PID" ]] && kill -0 "$FAST_SERVER_PID" 2>/dev/null; then
        kill "$FAST_SERVER_PID" 2>/dev/null
        wait "$FAST_SERVER_PID" 2>/dev/null
    fi
    case "$FAST_EXEC_DIR" in
        "$SOURCE_ROOT"/target/exomonad-fast-start.*) rm -rf -- "$FAST_EXEC_DIR" ;;
    esac
    exit "$status"
}
trap cleanup EXIT INT TERM

mkdir -p "$FAST_REPO" "$FAST_HOME/.exo" "$(dirname "$REMOTE_DIR")" "$SOURCE_ROOT/target"
FAST_EXEC_DIR="$(mktemp -d "$SOURCE_ROOT/target/exomonad-fast-start.XXXXXX")"
FAST_FAKE_BIN="$FAST_EXEC_DIR/bin"
FAST_SHELL="$FAST_FAKE_BIN/fast-start-shell"
FAST_TMUX_BIN="$(command -v tmux || true)"
[[ -n "$FAST_TMUX_BIN" ]] || fail "tmux is required for the fast-start acceptance"
# The fixture deliberately narrows PATH for spawned agents. Keep the real
# tmux next to the fake harness binaries: under `nix develop` tmux is not in
# /usr/bin, and the acceptance still drives its own isolated server.
FAST_TEST_PATH="$FAST_FAKE_BIN:$(dirname "$FAST_TMUX_BIN"):/usr/bin:/bin"
mkdir -p "$FAST_FAKE_BIN"
cp "$SOURCE_ROOT/tests/e2e/init-recovery/fake-codex.sh" "$FAST_FAKE_BIN/codex"
cp "$SOURCE_ROOT/tests/e2e/init-recovery/fake-codex.sh" "$FAST_FAKE_BIN/opencode"
chmod 0755 "$FAST_FAKE_BIN/codex" "$FAST_FAKE_BIN/opencode"
python3 - "$FAST_FAKE_BIN/codex" "$FAST_FAKE_BIN/opencode" <<'PY'
import sys
from pathlib import Path

legacy_marker = 'if [[ "$role" == "dev" && "${E2E_FAST_START:-0}" == "1" ]]; then\n'
marker = 'if [[ ( "$role" == "dev" || "$role" == "worker" ) && "${E2E_FAST_START:-0}" == "1" ]]; then\n'
trace = '''printf '{"event":"fake_runtime_entered","runtime":"%s","role":"%s","agent_id":"%s","model":"%s","effort":"%s"}\\n' \\
    "$(basename "$0")" "$role" "$agent" "$model" "$effort" >> "$E2E_AGENT_LOG"
'''
for argument in sys.argv[1:]:
    path = Path(argument)
    contents = path.read_text(encoding="utf-8")
    if legacy_marker not in contents:
        raise SystemExit(f"fake runtime marker missing from {path}")
    contents = contents.replace(legacy_marker, marker, 1)
    path.write_text(contents.replace(marker, trace + marker, 1), encoding="utf-8")
PY
cat > "$FAST_SHELL" <<EOF
#!/bin/sh
export PATH="$FAST_TEST_PATH"
export HOME="$FAST_HOME"
export CODEX_HOME="$CODEX_HOME"
export E2E_FAST_START=1
export E2E_FAST_REPO="$FAST_REPO"
export E2E_FAST_WORKER_LIFETIME=120
export E2E_AGENT_LOG="$FAST_AGENT_LOG"
export E2E_MOCK_URL="$MOCK_URL"
export E2E_MCP_SOCKET="$FAST_REPO/.exo/server.sock"
printf 'agent_shell_started PATH=%s HOME=%s ROLE=%s AGENT=%s\n' \
    "\$PATH" "\$HOME" "\${EXOMONAD_ROLE:-}" "\${EXOMONAD_AGENT_ID:-}" \
    >> "$FAST_DIR/shell-invocations.log"
exec /bin/bash --noprofile "\$@"
EOF
chmod 0755 "$FAST_SHELL"
git init --bare -q "$REMOTE_DIR"
git -C "$FAST_REPO" init -q -b main
git -C "$FAST_REPO" config user.name "Exomonad fast startup E2E"
git -C "$FAST_REPO" config user.email "fast-startup-e2e@example.invalid"
printf 'fresh fast-start repository\n' > "$FAST_REPO/README.md"
git -C "$FAST_REPO" add README.md
git -C "$FAST_REPO" commit -q -m "fast startup base"
cat > "$FAST_DIR/git-ssh" <<'EOF'
#!/bin/sh
E2E_GIT_REMOTE_DIR="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$REMOTE_DIR|" "$FAST_DIR/git-ssh"
chmod 0755 "$FAST_DIR/git-ssh"
git -C "$FAST_REPO" remote add origin "git@forgejo-e2e:owner/repo.git"
git -C "$FAST_REPO" config core.sshCommand "$FAST_DIR/git-ssh"
GIT_SSH_COMMAND="$FAST_DIR/git-ssh" git -C "$FAST_REPO" push -u origin main -q
(cd "$FAST_REPO" && HOME="$FAST_HOME" "$EXOMONAD_BIN" new) \
    > "$FAST_DIR/new.log" 2>&1 \
    || { cat "$FAST_DIR/new.log"; fail "exomonad new failed for fast-start fixture"; }
cp "$POLICY_REPO/.exo/harness_policy.toml" "$FAST_REPO/.exo/harness_policy.toml"
cp "$POLICY_REPO/.exo/review-policy.toml" "$FAST_REPO/.exo/review-policy.toml"
cp "$POLICY_REPO/.exo/harness_capability.toml" "$FAST_REPO/.exo/harness_capability.toml"
if [[ -d "$SOURCE_ROOT/.exo/roles" ]]; then
    rm -rf "$FAST_REPO/.exo/roles"
    cp -r "$SOURCE_ROOT/.exo/roles" "$FAST_REPO/.exo/roles"
fi
mkdir -p "$FAST_REPO/.exo/wasm"
for artifact in "$WASM_DIR"/wasm-guest-*.wasm; do
    [[ -s "$artifact" ]] || fail "fresh WASM artifact is missing: $artifact"
    ln -sf "$artifact" "$FAST_REPO/.exo/wasm/$(basename "$artifact")"
done
mkdir -p "$FAST_REPO/.exo/tl-loop"
cat > "$FAST_REPO/.exo/tl-loop/plan.json" <<'EOF'
{"run_id":"root","plan":{"workers":[{"name":"fast-start-worker","task":"record immediate startup activity","agent_type":"codex"}]}}
EOF
FAST_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > "$FAST_REPO/.exo/config.toml" <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$FAST_SESSION"
yolo = true
poll_interval = 1
port = $FAST_PORT
spawn_agent_type = "codex"
forgejo_url = "$MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"
worker_effort_level = "xhigh"

[reviewer]
agent_type = "codex"
model = "gpt-6.1-sol"
effort_level = "medium"
EOF
printf '\n.mcp.json\n' >> "$FAST_REPO/.gitignore"
git -C "$FAST_REPO" add -A
git -C "$FAST_REPO" add -f \
    .exo/config.toml .exo/harness_policy.toml .exo/review-policy.toml \
    .exo/harness_capability.toml .exo/tl-loop/plan.json
git -C "$FAST_REPO" commit -q -m "configure immediate startup acceptance"
git -C "$FAST_REPO" push -u origin main -q

(
    cd "$FAST_REPO"
    env HOME="$FAST_HOME" CODEX_HOME="$CODEX_HOME" \
        PATH="$FAST_TEST_PATH" \
        SHELL="$FAST_SHELL" \
        EXOMONAD_TMUX_SESSION="$FAST_SESSION" \
        EXOMONAD_ROOT_AGENT_TYPE=codex EXOMONAD_SPAWN_AGENT_TYPE=codex \
        EXOMONAD_REVIEWER_AGENT_TYPE=codex EXOMONAD_CODEX_MODEL=gpt-6-luna \
        E2E_FAST_START=1 E2E_FAST_REPO="$FAST_REPO" \
        E2E_FAST_WORKER_LIFETIME=120 E2E_AGENT_LOG="$FAST_AGENT_LOG" \
        E2E_MOCK_URL="$MOCK_URL" E2E_MCP_SOCKET="$FAST_REPO/.exo/server.sock" \
        "$EXOMONAD_BIN" serve
) > "$FAST_DIR/server.log" 2>&1 &
FAST_SERVER_PID=$!
for _ in $(seq 1 300); do
    [[ -S "$FAST_REPO/.exo/server.sock" ]] && break
    kill -0 "$FAST_SERVER_PID" 2>/dev/null \
        || { cat "$FAST_DIR/server.log"; fail "fast-start server exited before socket creation"; }
    sleep 0.1
done
[[ -S "$FAST_REPO/.exo/server.sock" ]] \
    || { cat "$FAST_DIR/server.log"; fail "fast-start server socket did not become ready"; }
    env PATH="$FAST_TEST_PATH" HOME="$FAST_HOME" CODEX_HOME="$CODEX_HOME" \
    E2E_FAST_START=1 E2E_FAST_REPO="$FAST_REPO" \
    E2E_FAST_WORKER_LIFETIME=120 E2E_AGENT_LOG="$FAST_AGENT_LOG" \
    E2E_MOCK_URL="$MOCK_URL" E2E_MCP_SOCKET="$FAST_REPO/.exo/server.sock" \
    tmux new-session -d -s "$FAST_BOOTSTRAP_SESSION" -c "$FAST_REPO" 'sleep 300'
tmux set-window-option -g remain-on-exit on

for assignment in \
    "PATH=$FAST_TEST_PATH" \
    "HOME=$FAST_HOME" \
    "CODEX_HOME=$CODEX_HOME" \
    "EXOMONAD_CODEX_MODEL=gpt-6-luna" \
    "E2E_FAST_START=1" \
    "E2E_FAST_REPO=$FAST_REPO" \
    "E2E_FAST_WORKER_LIFETIME=120" \
    "E2E_AGENT_LOG=$FAST_AGENT_LOG" \
    "E2E_MOCK_URL=$MOCK_URL" \
    "E2E_MCP_SOCKET=$FAST_REPO/.exo/server.sock" \
    "GIT_SSH_COMMAND=$FAST_DIR/git-ssh"; do
    tmux set-environment -g "${assignment%%=*}" "${assignment#*=}"
done

run_init() {
    local mode="$1" status=0
    local args=(init --start --session "$FAST_SESSION")
    if [[ "$mode" == "continue" ]]; then
        args=(init --continue --session "$FAST_SESSION")
    fi
    (cd "$FAST_REPO" && HOME="$FAST_HOME" CODEX_HOME="$CODEX_HOME" \
        PATH="$FAST_TEST_PATH" \
        EXOMONAD_ROOT_AGENT_TYPE=codex EXOMONAD_SPAWN_AGENT_TYPE=codex \
        EXOMONAD_REVIEWER_AGENT_TYPE=codex EXOMONAD_CODEX_MODEL=gpt-6-luna \
        E2E_FAST_START=1 E2E_FAST_REPO="$FAST_REPO" E2E_AGENT_LOG="$FAST_AGENT_LOG" \
        GIT_SSH_COMMAND="$FAST_DIR/git-ssh" FORGEJO_TOKEN="test-token-e2e" \
        FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e" \
        "$EXOMONAD_BIN" "${args[@]}") \
        > "$FAST_DIR/init-$mode.log" 2>&1 || status=$?
    if [[ "$status" -ne 0 ]] && ! rg -Fq 'open terminal failed: not a terminal' "$FAST_DIR/init-$mode.log"; then
        cat "$FAST_DIR/init-$mode.log"
        fail "fast-start init $mode failed with status $status"
    fi
}

run_init first
for _ in $(seq 1 150); do
    if [[ ! -e "$FAST_REPO/.exo/tl-loop/root/controller-startup.json" ]] \
        && python3 - "$FAST_AGENT_LOG" "$FAST_REPO" <<'PY'
import json
import sys
from pathlib import Path

path, repo = Path(sys.argv[1]), Path(sys.argv[2])
if not path.is_file():
    raise SystemExit(1)
events = [json.loads(line).get("event") for line in path.read_text().splitlines()]
if not {"fast_start_worker_started", "fast_start_task_active"} <= set(events):
    raise SystemExit(1)
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
slice_state = root["slices"].get("fast-start-worker", {})
raise SystemExit(0 if slice_state.get("dispatch_invocation_id") else 1)
PY
    then
        break
    fi
    sleep 0.1
done
python3 - "$FAST_REPO" "$FAST_AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path

repo, log_path = Path(sys.argv[1]), Path(sys.argv[2])
events = [json.loads(line) for line in log_path.read_text().splitlines()]
starts = [event for event in events if event.get("event") == "fast_start_worker_started"]
activity = [event for event in events if event.get("event") == "fast_start_task_active"]
assert len(starts) == 1, f"expected one launched fast-start worker, found {len(starts)}"
assert len(activity) == 1, f"expected one active task marker, found {len(activity)}"
assert not (repo / ".exo/tl-loop/root/controller-startup.json").exists()
owner = json.loads((repo / ".exo/tl-loop/root/controller-target.json").read_text())
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
slice_state = root["slices"]["fast-start-worker"]
assert owner["run_id"] == "root" and owner["controller_epoch"] == root["controller_epoch"]
assert slice_state["dispatch_invocation_id"]
assert slice_state["dispatch_agent_id"] == starts[0]["agent_id"]
agent_dir = repo / ".exo/agents" / starts[0]["agent_id"]
invocation = json.loads((agent_dir / "invocation.json").read_text())
assert invocation["invocation_id"] == slice_state["dispatch_invocation_id"]
assert invocation["status"] == "running"
task_marker = Path(activity[0]["task_marker"])
assert task_marker.parent == agent_dir and task_marker.is_file()
journal = json.loads((repo / ".exo/tl-loop/root/action-journal.json").read_text())
spawns = [entry for entry in journal if entry.get("operation") == "spawn_worker" and entry.get("target") == "fast-start-worker"]
assert len(spawns) == 1 and spawns[0]["status"] == "confirmed"
print(json.dumps({"agent_id": starts[0]["agent_id"], "invocation_id": invocation["invocation_id"], "generation": invocation["generation"]}))
PY
FAST_OWNER_BEFORE_REPEAT="$(python3 - "$FAST_REPO" "$FAST_AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path
repo, log_path = Path(sys.argv[1]), Path(sys.argv[2])
events = [json.loads(line) for line in log_path.read_text().splitlines()]
agent = next(event["agent_id"] for event in events if event.get("event") == "fast_start_worker_started")
invocation = json.loads((repo / ".exo/agents" / agent / "invocation.json").read_text())
print(invocation["invocation_id"], invocation["generation"], agent)
PY
)"
run_init continue
[[ "$FAST_OWNER_BEFORE_REPEAT" == "$(python3 - "$FAST_REPO" "$FAST_AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path
repo, log_path = Path(sys.argv[1]), Path(sys.argv[2])
events = [json.loads(line) for line in log_path.read_text().splitlines()]
agent = next(event["agent_id"] for event in events if event.get("event") == "fast_start_worker_started")
invocation = json.loads((repo / ".exo/agents" / agent / "invocation.json").read_text())
print(invocation["invocation_id"], invocation["generation"], agent)
PY
)" ]] || fail "repeat init changed the active worker identity"
python3 - "$FAST_REPO" "$FAST_AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path
repo, log_path = Path(sys.argv[1]), Path(sys.argv[2])
events = [json.loads(line) for line in log_path.read_text().splitlines()]
assert sum(event.get("event") == "fast_start_worker_started" for event in events) == 1
assert sum(event.get("event") == "fast_start_task_active" for event in events) == 1
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
assert root["slices"]["fast-start-worker"]["dispatch_invocation_id"]
assert not (repo / ".exo/tl-loop/root/controller-startup.json").exists()
PY
printf 'PASS: immediate actual-init dispatch waited for committed ownership; repeat init kept one worker (repo=%s)\n' "$FAST_REPO"
