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
REMOTE_DIR="$FAST_DIR/remote/owner/repo.git"
FAST_REPO="$FAST_DIR/repo"
FAST_SESSION="e2e-init-fast-handoff"
FAST_AGENT_LOG="$FAST_DIR/agent-events.jsonl"
FAST_SERVER_PID=""

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
    local status=$?
    set +e
    tmux kill-session -t "$FAST_SESSION" 2>/dev/null
    if [[ -n "$FAST_SERVER_PID" ]] && kill -0 "$FAST_SERVER_PID" 2>/dev/null; then
        kill "$FAST_SERVER_PID" 2>/dev/null
        wait "$FAST_SERVER_PID" 2>/dev/null
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM

mkdir -p "$FAST_REPO" "$FAST_HOME/.exo" "$(dirname "$REMOTE_DIR")"
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
poll_interval = 0.1
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

(
    cd "$FAST_REPO"
    env HOME="$FAST_HOME" CODEX_HOME="$CODEX_HOME" \
        EXOMONAD_TMUX_SESSION="$FAST_SESSION" \
        EXOMONAD_ROOT_AGENT_TYPE=codex EXOMONAD_SPAWN_AGENT_TYPE=codex \
        EXOMONAD_REVIEWER_AGENT_TYPE=codex EXOMONAD_CODEX_MODEL=gpt-6-luna \
        "$EXOMONAD_BIN" serve
) > "$FAST_DIR/server.log" 2>&1 &
FAST_SERVER_PID=$!
for _ in $(seq 1 100); do
    [[ -S "$FAST_REPO/.exo/server.sock" ]] && break
    kill -0 "$FAST_SERVER_PID" 2>/dev/null \
        || { cat "$FAST_DIR/server.log"; fail "fast-start server exited before socket creation"; }
    sleep 0.1
done
[[ -S "$FAST_REPO/.exo/server.sock" ]] \
    || { cat "$FAST_DIR/server.log"; fail "fast-start server socket did not become ready"; }

for assignment in \
    "PATH=$PATH" \
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
    local args=(init --session "$FAST_SESSION")
    if [[ "$mode" == "continue" ]]; then
        args=(init --continue --session "$FAST_SESSION")
    fi
    (cd "$FAST_REPO" && HOME="$FAST_HOME" CODEX_HOME="$CODEX_HOME" \
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
    [[ -s "$FAST_AGENT_LOG" ]] && [[ ! -e "$FAST_REPO/.exo/tl-loop/root/controller-startup.json" ]] && break
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
identity = json.loads((agent_dir / "identity.json").read_text())
assert invocation["invocation_id"] == slice_state["dispatch_invocation_id"]
assert invocation["status"] == "running"
assert (Path(identity["working_dir"]) / "fast-start-task.txt").is_file()
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
