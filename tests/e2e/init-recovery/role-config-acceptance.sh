#!/usr/bin/env bash
# Role-configuration acceptance for Chainlink #1433 R2-R4.
#
# Proves, through the compiled production path:
#   * a standalone `exomonad serve` loaded from project configuration and an
#     `init` repeating identical harness/model/effort values agree on one v2
#     effective-value identity and release exactly one routed worker;
#   * repeat init adds no duplicate launch, worker, controller, branch or
#     dispatch after the dispatch reaches its terminal boundary;
#   * genuine model/effort/harness overrides hold before dispatch with an
#     actionable role_configuration_not_accepted error and leave an
#     independent worker's pid, pane and process start time untouched;
#   * a server that reports no role identity (old binary) holds init with
#     role_configuration_unavailable.
#
# Selected by E2E_ROLE_CONFIG_ONLY=1 in run.sh, which supplies the same
# Phase 0 build, fresh WASM, isolated HOME/Codex home and run-scoped tmux
# server the composed acceptance uses. Harnesses are disposable shims.
set -euo pipefail

export NO_COLOR=1

EXOMONAD_BIN="${1:?binary path required}"
ROLE_DIR="${2:?fixture directory required}"
ROLE_HOME="${3:?isolated home required}"
CODEX_HOME="${4:?Codex home required}"
SOURCE_ROOT="${5:?source workspace required}"
WASM_DIR="${6:?fresh WASM directory required}"

SESSION="e2e-role-config"
REPO="$ROLE_DIR/repo"
REMOTE="$ROLE_DIR/remote/owner/repo.git"
SHIM_DIR="$ROLE_DIR/bin"
AGENT_LOG="$ROLE_DIR/agent-events.jsonl"
MOCK_STUB_PID=""
MOCK_PID=""
SERVER_PID=""
STUB_PID=""
INIT_PID=""
WORKER_LIFETIME="${E2E_ROLE_CONFIG_WORKER_LIFETIME:-120}"
WORKER_EXIT_FILE="$ROLE_DIR/worker-exit-requested"
BASE_PATH="${PATH:-/usr/bin:/bin}"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

tmux_iso() {
    env -u TMUX -u TMUX_PANE TMUX_TMPDIR="${TMUX_TMPDIR:?run-scoped TMUX_TMPDIR required}" tmux "$@"
}

cleanup() {
    local status=$?
    set +e
    if [[ -n "$INIT_PID" ]] && kill -0 "$INIT_PID" 2>/dev/null; then
        kill "$INIT_PID" 2>/dev/null
    fi
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    if [[ -n "$STUB_PID" ]] && kill -0 "$STUB_PID" 2>/dev/null; then
        kill "$STUB_PID" 2>/dev/null
        wait "$STUB_PID" 2>/dev/null
    fi
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null
        wait "$MOCK_PID" 2>/dev/null
    fi
    if (( status != 0 )); then
        mkdir -p "$ROLE_DIR/tmux-failure-evidence"
        tmux_iso list-panes -a -F '#{session_name} #{window_name} #{pane_id} #{pane_pid} #{pane_current_command} #{pane_dead}' \
            > "$ROLE_DIR/tmux-failure-evidence/panes.txt" 2>&1
        while IFS= read -r pane_id; do
            [[ -n "$pane_id" ]] || continue
            tmux_iso capture-pane -p -t "$pane_id" -S -120 \
                > "$ROLE_DIR/tmux-failure-evidence/pane-${pane_id//%/}.txt" 2>&1
        done < <(tmux_iso list-panes -a -F '#{pane_id}' 2>/dev/null)
    fi
    # The tmux server is this run's own: TMUX_TMPDIR is the run-scoped
    # directory supplied by run.sh, and TMUX/TMUX_PANE are scrubbed.
    tmux_iso kill-server 2>/dev/null
    if tmux_iso list-sessions >/dev/null 2>&1; then
        printf 'ERROR: role-config tmux server survived teardown\n' >&2
        status=1
    fi
    if (( status != 0 )); then
        printf 'ERROR: role-config acceptance failed; fixture kept at %s\n' "$ROLE_DIR" >&2
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM

mkdir -p "$ROLE_DIR" "$REPO" "$ROLE_HOME/.exo" "$(dirname "$REMOTE")" "$SHIM_DIR"
cd "$REPO"

python3 "$SOURCE_ROOT/scripts/build_tl_loop_archive.py" \
    --source "$SOURCE_ROOT/tl_loop" --output "$ROLE_HOME/.exo/tl_loop.pyz" >/dev/null
python3 "$SOURCE_ROOT/scripts/check_tl_loop_archive.py" \
    "$ROLE_HOME/.exo/tl_loop.pyz" --source "$SOURCE_ROOT/tl_loop" >/dev/null

cp "$SOURCE_ROOT/tests/e2e/init-recovery/fake-codex.sh" "$SHIM_DIR/opencode"
cp "$SOURCE_ROOT/tests/e2e/init-recovery/fake-codex.sh" "$SHIM_DIR/codex"
chmod 0755 "$SHIM_DIR/opencode" "$SHIM_DIR/codex"
export E2E_ROLE_CONFIG_ACCEPTANCE=1
export E2E_AGENT_LOG="$AGENT_LOG"
export E2E_ROLE_CONFIG_WORKER_LIFETIME="$WORKER_LIFETIME"
export E2E_ROLE_CONFIG_WORKER_EXIT_FILE="$WORKER_EXIT_FILE"

git init --bare -q "$REMOTE"
git init -q -b main
git config user.name "Exomonad role-config acceptance"
git config user.email "role-config-e2e@example.invalid"
printf 'role configuration acceptance base\n' > README.md
git add README.md
git commit -q -m "role configuration acceptance base"
cat > "$ROLE_DIR/git-ssh" <<'EOF'
#!/bin/sh
REMOTE="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${REMOTE:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${REMOTE:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$REMOTE|" "$ROLE_DIR/git-ssh"
chmod 0755 "$ROLE_DIR/git-ssh"
git remote add origin "git@forgejo-e2e:owner/repo.git"
git config core.sshCommand "$ROLE_DIR/git-ssh"
GIT_SSH_COMMAND="$ROLE_DIR/git-ssh" git push -u origin main -q

(
    export HOME="$ROLE_HOME" CODEX_HOME="$CODEX_HOME"
    export PATH="$SHIM_DIR:$BASE_PATH"
    export EXOMONAD_CODEX_MODEL="${EXOMONAD_CODEX_MODEL:-e2e-unused-model}"
    "$EXOMONAD_BIN" new
) > "$ROLE_DIR/new.log" 2>&1 || { cat "$ROLE_DIR/new.log"; fail "exomonad new failed"; }

mkdir -p .exo/wasm .exo/tl-loop
for artifact in "$WASM_DIR"/wasm-guest-*.wasm; do
    [[ -s "$artifact" ]] || fail "fresh WASM artifact is missing: $artifact"
    ln -sf "$artifact" ".exo/wasm/$(basename "$artifact")"
done
if [[ -d "$SOURCE_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$SOURCE_ROOT/.exo/roles" .exo/roles
fi
cat > .exo/harness_policy.toml <<'EOF'
[roles.tl]
allow = ["opencode/opencode-go/glm-5.3-flash"]
cost_rank = { "opencode/opencode-go/glm-5.3-flash" = 1 }
token_budget = 120000
escalate_after_attempts = 1

[roles.worker]
allow = ["opencode/opencode-go/deepseek-v4.1-flash", "opencode/opencode-go/glm-5.3-flash", "claude", "shoal"]
cost_rank = { "opencode/opencode-go/deepseek-v4.1-flash" = 1, "opencode/opencode-go/glm-5.3-flash" = 2, "claude" = 3, "shoal" = 4 }
token_budget = 120000
escalate_after_attempts = 1

[roles.reviewer]
allow = ["opencode/opencode-go/glm-5.3-flash"]
cost_rank = { "opencode/opencode-go/glm-5.3-flash" = 1 }
token_budget = 60000
escalate_after_attempts = 1
EOF
cat > .exo/harness_capability.toml <<'EOF'
[capabilities]
"opencode/opencode-go/glm-5.3-flash" = "hard"
"opencode/opencode-go/deepseek-v4.1-flash" = "hard"
"claude" = "standard"
"shoal" = "standard"
EOF
cp "$SOURCE_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cat > .exo/tl-loop/plan.json <<'EOF'
{"run_id":"root","plan":{"workers":[{"name":"rc-worker","task":"prove role configuration acceptance","agent_type":"opencode"}]}}
EOF
cp .exo/tl-loop/plan.json .exo/tl-loop/plan.snapshot
python3 - .exo/tl-loop/plan.snapshot <<'PY'
import hashlib, sys
from pathlib import Path
snapshot = Path(sys.argv[1]).read_bytes()
Path(sys.argv[1] + ".sha256").write_text(hashlib.sha256(snapshot).hexdigest() + "\n")
PY

MOCK_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
REMOTE_DIR="$REMOTE" MOCK_LOG="$ROLE_DIR/mock.log" \
    python3 "$SOURCE_ROOT/tests/e2e/mock_github.py" --port "$MOCK_PORT" \
    > "$ROLE_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 100); do
    curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null \
    || { cat "$ROLE_DIR/mock.stderr"; fail "mock Forgejo did not become ready"; }

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
yolo = true
poll_interval = 1
port = $PORT
root_agent_type = "opencode"
model = "opencode-go/glm-5.3-flash"
tl_effort_level = "high"
spawn_agent_type = "opencode"
worker_effort_level = "high"
worker_model = "opencode-go/deepseek-v4.1-flash"
forgejo_url = "$MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"

[reviewer]
agent_type = "opencode"
model = "opencode-go/glm-5.3-flash"
effort_level = "high"
EOF
printf '\n.mcp.json\n' >> .gitignore
git add -A
git add -f .exo/config.toml .exo/harness_policy.toml .exo/harness_capability.toml \
    .exo/review-policy.toml .exo/tl-loop/plan.json
git commit -q -m "configure role-config acceptance fixture"
GIT_SSH_COMMAND="$ROLE_DIR/git-ssh" git push -u origin main -q

ROLE_FLAGS=(
    --worker opencode
    --worker-model opencode-go/deepseek-v4.1-flash
    --worker-effort-level high
    --reviewer opencode
    --reviewer-model opencode-go/glm-5.3-flash
    --reviewer-effort-level high
)

run_env() {
    env HOME="$ROLE_HOME" CODEX_HOME="$CODEX_HOME" \
        PATH="$SHIM_DIR:$BASE_PATH" \
        SHELL="/bin/bash" \
        E2E_ROLE_CONFIG_ACCEPTANCE=1 E2E_AGENT_LOG="$AGENT_LOG" \
        E2E_ROLE_CONFIG_WORKER_LIFETIME="$WORKER_LIFETIME" \
        E2E_ROLE_CONFIG_WORKER_EXIT_FILE="$WORKER_EXIT_FILE" \
        E2E_MOCK_URL="$MOCK_URL" E2E_MCP_SOCKET="$REPO/.exo/server.sock" \
        E2E_REPO="$REPO" \
        GIT_SSH_COMMAND="$ROLE_DIR/git-ssh" \
        FORGEJO_TOKEN="test-token-e2e" FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e" \
        CHAINLINK_DB="$REPO/.chainlink/issues.db" \
        EXOMONAD_TMUX_SESSION="$SESSION" \
        "$@"
}

start_server() {
    local log="$1"
    (
        cd "$REPO" || exit 1
        export HOME="$ROLE_HOME" CODEX_HOME="$CODEX_HOME"
        export PATH="$SHIM_DIR:$BASE_PATH"
        export SHELL="/bin/bash"
        export E2E_ROLE_CONFIG_ACCEPTANCE=1 E2E_AGENT_LOG="$AGENT_LOG"
        export E2E_ROLE_CONFIG_WORKER_LIFETIME="$WORKER_LIFETIME"
        export E2E_ROLE_CONFIG_WORKER_EXIT_FILE="$WORKER_EXIT_FILE"
        export E2E_MOCK_URL="$MOCK_URL" E2E_MCP_SOCKET="$REPO/.exo/server.sock"
        export E2E_REPO="$REPO"
        export GIT_SSH_COMMAND="$ROLE_DIR/git-ssh"
        export FORGEJO_TOKEN="test-token-e2e" FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e"
        export CHAINLINK_DB="$REPO/.chainlink/issues.db"
        export EXOMONAD_TMUX_SESSION="$SESSION"
        # exec keeps the background PID equal to the server process, so
        # cleanup cannot orphan a live serve behind a shell.
        exec "$EXOMONAD_BIN" serve
    ) > "$log" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 600); do
        if [[ -S "$REPO/.exo/server.sock" ]] \
            && curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health >/dev/null 2>&1; then
            return 0
        fi
        kill -0 "$SERVER_PID" 2>/dev/null || { cat "$log"; fail "standalone serve exited before becoming healthy"; }
        sleep 0.1
    done
    cat "$log"
    fail "standalone serve socket did not become healthy"
}

health_identity() {
    curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["role_route_configuration"]["identity"])'
}

wait_for_worker() {
    for _ in $(seq 1 900); do
        if [[ -f "$AGENT_LOG" ]] && grep -q role_config_worker_started "$AGENT_LOG"; then
            return 0
        fi
        sleep 0.1
    done
    tail -60 "$ROLE_DIR/init-start.log" 2>/dev/null || true
    tail -80 "$REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    fail "the accepted launch never started the routed worker"
}

wait_for_dispatch_accepted() {
    for _ in $(seq 1 900); do
        if python3 - "$REPO" <<'PY'
import json, sys
from pathlib import Path
repo = Path(sys.argv[1])
count = 0
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    for line in segment.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if (
            event.get("type") == "tl.spawn_request_accepted"
            and (event.get("data") or {}).get("slice_id") == "rc-worker"
        ):
            count += 1
raise SystemExit(0 if count == 1 else 1)
PY
        then
            return 0
        fi
        sleep 0.1
    done
    fail "the accepted launch never recorded its terminal spawn acceptance"
}

ledger_counts() {
    python3 - "$REPO" "$1" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path

repo, agent = Path(sys.argv[1]), sys.argv[2]
counts = Counter()
by_child = Counter()
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    for line in segment.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        kind = event.get("type")
        counts[kind] += 1
        data = event.get("data") or {}
        if kind == "agent.spawned" and data.get("child_agent") == agent:
            by_child[kind] += 1
        if kind in {"agent.invocation.started", "agent.invocation.finished"} and event.get("agent_id") == agent:
            by_child[kind] += 1
        if kind in {
            "tl.dispatch_intended",
            "tl.spawn_requested",
            "tl.spawn_request_accepted",
            "tl.dispatch_confirmed",
        } and data.get("slice_id") == "rc-worker":
            by_child[kind] += 1
print(json.dumps(dict(sorted(by_child.items()))))
PY
}

init_log_identity() {
    sed -n 's/.*identity=\(sha256:v2:[0-9a-f]*\).*/\1/p' "$1" | head -n 1
}

# ---------------------------------------------------------------------------
# Phase A: standalone serve + identical flags -> one accepted worker (R2)
# ---------------------------------------------------------------------------
say "starting standalone serve from project configuration (not created by init)"
start_server "$ROLE_DIR/server.log"
SERVER_IDENTITY="$(health_identity)"
[[ "$SERVER_IDENTITY" == sha256:v2:* ]] || fail "standalone serve did not report a v2 identity: $SERVER_IDENTITY"
curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health > "$ROLE_DIR/health.json"
python3 - "$ROLE_DIR/health.json" <<'PY'
import json, sys
from pathlib import Path
health = json.loads(Path(sys.argv[1]).read_text())
route = health["role_route_configuration"]
assert route["identity"].startswith("sha256:v2:")
for role in ("tl", "worker", "reviewer"):
    assert role in route["provenance"], f"provenance is missing {role}"
print("standalone server accepted identity:", route["identity"])
PY

# A fresh fixture has no RunStore yet, so the first invocation is the supported
# `--start` mode; `launch_tl_recovery` reads the root RunStore before a
# `--continue` can release a controller, and the compiled `--continue` path
# with these identical flags is exercised by the repeat-init and override
# phases below.
say "running init with identical worker/reviewer harness/model/effort flags"
(
    cd "$REPO"
    run_env "$EXOMONAD_BIN" init --start --session "$SESSION" "${ROLE_FLAGS[@]}"
) > "$ROLE_DIR/init-start.log" 2>&1 &
INIT_PID=$!
wait_for_worker
wait_for_dispatch_accepted
wait "$INIT_PID" 2>/dev/null || true
INIT_PID=""
if grep -q 'ERROR.*exomonad init failed' "$ROLE_DIR/init-start.log"; then
    cat "$ROLE_DIR/init-start.log"
    fail "init failed before the accepted launch completed"
fi
INIT_IDENTITY="$(init_log_identity "$ROLE_DIR/init-start.log")"
[[ "$INIT_IDENTITY" == "$SERVER_IDENTITY" ]] \
    || fail "init did not log the server-accepted identity (init=$INIT_IDENTITY server=$SERVER_IDENTITY)"
grep -Fq 'server_provenance=' "$ROLE_DIR/init-start.log" \
    || fail "init did not audit the server provenance separately"

python3 - "$REPO" "$AGENT_LOG" "$SERVER_IDENTITY" <<'PY'
import json, sys
from pathlib import Path

repo, log_path, identity = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
starts = [event for event in events if event.get("event") == "role_config_worker_started"]
assert len(starts) == 1, f"expected exactly one launched worker, found {len(starts)}"
worker = starts[0]
assert worker["harness"] == "opencode", worker
assert worker["model"] == "opencode-go/deepseek-v4.1-flash", worker
assert worker["effort"] == "high", worker
assert worker["role"] in {"worker", "dev"}, worker
assert "--model" in worker["argv"], worker["argv"][:8]
assert "opencode-go/deepseek-v4.1-flash#high" in worker["argv"], worker["argv"][:8]
agent = worker["agent_id"]

# Durable dispatch authorization and spawn handshake, with the v2 identity.
segments = sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
events = [json.loads(line) for s in segments for line in s.read_text(encoding="utf-8").splitlines()]
for kind in ("tl.dispatch_intended", "tl.spawn_requested", "tl.spawn_request_accepted"):
    matching = [
        e for e in events
        if e.get("type") == kind
        and (e.get("data") or {}).get("slice_id") == "rc-worker"
    ]
    assert len(matching) == 1, f"expected one {kind}, found {len(matching)}"
    assert matching[0]["data"]["route_configuration_fingerprint"] == identity, matching[0]
spawned = [
    e for e in events
    if e.get("type") == "agent.spawned" and (e.get("data") or {}).get("child_agent") == agent
]
assert len(spawned) == 1, f"expected one agent.spawned for {agent}, found {len(spawned)}"
assert spawned[0]["data"]["model"] == "opencode-go/deepseek-v4.1-flash"
assert spawned[0]["data"]["effort"] == "high"
started = [
    e for e in events
    if e.get("type") == "agent.invocation.started" and e.get("agent_id") == agent
]
assert len(started) == 1, f"expected one invocation for {agent}, found {len(started)}"

invocation = json.loads((repo / ".exo/agents" / agent / "invocation.json").read_text())
route = invocation["role_route"]
assert route["configuration_fingerprint"] == identity, route
assert route["harness"] == "opencode/opencode-go/deepseek-v4.1-flash", route
assert route["model"] == "opencode-go/deepseek-v4.1-flash", route
assert route["effort"] == "high", route
assert route["role"] == "worker", route

root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
slice_state = root["slices"]["rc-worker"]
assert slice_state["dispatch_agent_id"] == agent, slice_state
assert slice_state["dispatch_invocation_id"] == invocation["invocation_id"], slice_state
assert slice_state["dispatch_route_configuration_fingerprint"] == identity, slice_state
journal = json.loads((repo / ".exo/tl-loop/root/action-journal.json").read_text())
spawns = [
    entry for entry in journal
    if entry.get("operation") == "spawn_worker" and entry.get("target") == "rc-worker"
]
assert len(spawns) == 1 and spawns[0].get("status") == "confirmed", spawns
target = json.loads((repo / ".exo/tl-loop/root/controller-target.json").read_text())
assert target["controller_epoch"] == root["controller_epoch"], (target, root["controller_epoch"])
print(f"accepted worker={agent} invocation={invocation['invocation_id']} identity={identity}")
PY
ROLE_PASS_1="standalone serve + identical flags -> one accepted correctly routed worker"
say "PASS: $ROLE_PASS_1"

# Capture the independent worker before the override probes (R3).  It is the
# worker that the accepted launch above started; the overrides must not touch
# it or release another controller.
python3 - "$REPO" "$AGENT_LOG" "$ROLE_DIR/worker-snapshot.json" <<'PY'
import json, sys
from pathlib import Path
repo, log_path, output = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
worker = next(e for e in events if e.get("event") == "role_config_worker_started")
invocation = json.loads((repo / ".exo/agents" / worker["agent_id"] / "invocation.json").read_text())
import hashlib
snapshot = {
    "agent_id": worker["agent_id"],
    "shim_pid": worker.get("shim_pid"),
    "recorder_pid": worker.get("recorder_pid"),
    "lifetime": worker.get("lifetime"),
    "pane_id": invocation["routing"]["pane_id"],
    "invocation_sha256": hashlib.sha256(
        (repo / ".exo/agents" / worker["agent_id"] / "invocation.json").read_bytes()
    ).hexdigest(),
    "controller_target": (repo / ".exo/tl-loop/root/controller-target.json").read_text(),
    "controller_epoch": (repo / ".exo/tl-loop/root.controller-epoch").read_text(),
    "gates": json.loads((repo / ".exo/tl-loop/root/run.json").read_text())["gates"],
    "dispatch_counts": {},
}
counts = {}
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    for line in segment.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        counts[event.get("type", "")] = counts.get(event.get("type", ""), 0) + 1
snapshot["dispatch_counts"] = counts
output.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n")
print(json.dumps(snapshot, sort_keys=True))
PY
WORKER_PANE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pane_id"])' "$ROLE_DIR/worker-snapshot.json")"
tmux_iso display-message -p -t "$WORKER_PANE" '#{window_id} #{pane_id} #{pane_pid} #{pane_dead}' > "$ROLE_DIR/worker-pane.txt"
read -r WORKER_WINDOW _ WORKER_PID WORKER_DEAD < "$ROLE_DIR/worker-pane.txt"
[[ -n "$WORKER_WINDOW" && -n "$WORKER_PID" ]] || fail "independent worker pane $WORKER_PANE has no tmux window/pid"
[[ "$WORKER_DEAD" == "0" ]] || fail "independent worker pane $WORKER_PANE is not alive"
WORKER_LSTART="$(ps -o lstart= -p "$WORKER_PID" 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
[[ -n "$WORKER_LSTART" ]] || fail "independent worker pane pid $WORKER_PID is not running"

# ---------------------------------------------------------------------------
# Phase B: genuine overrides hold before dispatch (R3)
# ---------------------------------------------------------------------------
check_override_hold() {
    local label="$1"
    local log="$ROLE_DIR/init-override-$label.log"
    shift
    local -a args=("$@")
    local status=0
    (
        cd "$REPO"
        run_env "$EXOMONAD_BIN" init --continue --session "$SESSION" "${args[@]}"
    ) > "$log" 2>&1 || status=$?
    [[ "$status" -ne 0 ]] || { cat "$log"; fail "$label override unexpectedly succeeded"; }
    grep -Fq "role_configuration_not_accepted" "$log" \
        || { cat "$log"; fail "$label override did not hold with role_configuration_not_accepted"; }
    grep -Fq "init resolved sha256:v2:" "$log" \
        || { cat "$log"; fail "$label hold did not name the expected identity"; }
    grep -Fq "but the server accepts $SERVER_IDENTITY" "$log" \
        || { cat "$log"; fail "$label hold did not name the server-accepted identity"; }
    grep -Fq "restart \`exomonad serve\`" "$log" \
        || { cat "$log"; fail "$label hold did not give the operator remedy"; }
    local expected
    expected="$(sed -n 's/.*init resolved \(sha256:v2:[0-9a-f]*\) but the server accepts.*/\1/p' "$log" | head -n 1)"
    [[ -n "$expected" && "$expected" != "$SERVER_IDENTITY" ]] \
        || fail "$label override did not change the resolved identity"
    say "PASS: $label override held before dispatch (expected=$expected received=$SERVER_IDENTITY)"
}

check_override_hold model-only \
    --worker opencode \
    --worker-model opencode-go/glm-5.3-flash \
    --worker-effort-level high \
    --reviewer opencode \
    --reviewer-model opencode-go/glm-5.3-flash \
    --reviewer-effort-level high
check_override_hold effort-only \
    --worker opencode \
    --worker-model opencode-go/deepseek-v4.1-flash \
    --worker-effort-level low \
    --reviewer opencode \
    --reviewer-model opencode-go/glm-5.3-flash \
    --reviewer-effort-level high
check_override_hold harness-only \
    --worker shoal \
    --worker-effort-level high \
    --reviewer opencode \
    --reviewer-model opencode-go/glm-5.3-flash \
    --reviewer-effort-level high

kill -0 "$WORKER_PID" 2>/dev/null || fail "independent worker $WORKER_PID was disturbed by an override"
[[ "$(ps -o lstart= -p "$WORKER_PID" 2>/dev/null | sed 's/^ *//;s/ *$//' || true)" == "$WORKER_LSTART" ]] \
    || fail "independent worker process start time changed"
tmux_iso display-message -p -t "$WORKER_PANE" '#{window_id} #{pane_id} #{pane_pid} #{pane_dead}' > "$ROLE_DIR/worker-pane-after.txt"
diff -u "$ROLE_DIR/worker-pane.txt" "$ROLE_DIR/worker-pane-after.txt" >/dev/null \
    || fail "independent worker tmux window/pane changed"
python3 - "$REPO" "$ROLE_DIR/worker-snapshot.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
repo, snapshot_path = Path(sys.argv[1]), Path(sys.argv[2])
snapshot = json.loads(snapshot_path.read_text())
agent_dir = repo / ".exo/agents" / snapshot["agent_id"]
digest = hashlib.sha256((agent_dir / "invocation.json").read_bytes()).hexdigest()
assert digest == snapshot["invocation_sha256"], "independent worker invocation changed"
assert (repo / ".exo/tl-loop/root/controller-target.json").read_text() == snapshot["controller_target"], \
    "an override released or retargeted a controller"
assert (repo / ".exo/tl-loop/root.controller-epoch").read_text() == snapshot["controller_epoch"], \
    "an override wrote a new controller epoch"
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
assert root["gates"] == snapshot["gates"], "an override changed durable gates"
counts = {}
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    for line in segment.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        counts[event.get("type", "")] = counts.get(event.get("type", ""), 0) + 1
for kind in (
    "tl.dispatch_intended",
    "tl.spawn_requested",
    "tl.spawn_request_accepted",
    "agent.spawned",
    "agent.invocation.started",
):
    assert counts.get(kind, 0) == snapshot["dispatch_counts"].get(kind, 0), f"override wrote {kind}"
# The existing dispatch may reach its own terminal confirmation while the
# probes run; that is the pre-existing worker finishing, never a new dispatch.
assert counts.get("tl.dispatch_confirmed", 0) <= 1, "an override confirmed a new dispatch"
assert not any(
    event.get("type") == "agent.invocation.started"
    and event.get("agent_id") != snapshot["agent_id"]
    for event in (
        json.loads(line)
        for segment in (repo / ".exo/ledger/segments").glob("segment-*.jsonl")
        for line in segment.read_text(encoding="utf-8").splitlines()
    )
), "an override started another invocation"
print("independent worker pid, pane, process start time, controller epoch and gates unchanged")
PY
ROLE_PASS_3="genuine override held before dispatch; independent worker undisturbed"
say "PASS: $ROLE_PASS_3"

# All independent-worker evidence is captured; let the shim finish promptly so
# the terminal-boundary wait is not a fixed wall-clock delay.
touch "$WORKER_EXIT_FILE"

# ---------------------------------------------------------------------------
# Phase C: repeat init after the terminal dispatch boundary (R4)
# ---------------------------------------------------------------------------
wait_for_terminal() {
    for _ in $(seq 1 900); do
        if python3 - "$REPO" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent_id"])' "$ROLE_DIR/worker-snapshot.json")" <<'PY'
import json, sys
from pathlib import Path
repo, agent = Path(sys.argv[1]), sys.argv[2]
events = [
    json.loads(line)
    for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
def count(kind, by_agent=False):
    return sum(
        event.get("type") == kind
        and (event.get("agent_id") == agent if by_agent else True)
        for event in events
    )
raise SystemExit(0 if (
    count("agent.spawned") == 2
    and count("agent.invocation.started", True) == 1
    and count("agent.invocation.finished", True) == 1
    and count("tl.spawn_requested") == 1
    and count("tl.spawn_request_accepted") == 1
    and count("tl.dispatch_confirmed") == 1
) else 1)
PY
        then
            return 0
        fi
        sleep 0.1
    done
    fail "the accepted launch never reached its terminal dispatch boundary"
}
wait_for_terminal

durable_snapshot() {
    python3 - "$REPO" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent_id"])' "$ROLE_DIR/worker-snapshot.json")" <<'PY'
import json, subprocess, sys
from pathlib import Path
repo, agent = Path(sys.argv[1]), sys.argv[2]
events = [
    json.loads(line)
    for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
]
def count(kind, by_agent=False):
    return sum(
        event.get("type") == kind
        and (event.get("agent_id") == agent if by_agent else True)
        for event in events
    )
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text())
journal = json.loads((repo / ".exo/tl-loop/root/action-journal.json").read_text())
snapshot = {
    "spawned": count("agent.spawned"),
    "started": count("agent.invocation.started", True),
    "finished": count("agent.invocation.finished", True),
    "intended": count("tl.dispatch_intended"),
    "requested": count("tl.spawn_requested"),
    "accepted": count("tl.spawn_request_accepted"),
    "confirmed": count("tl.dispatch_confirmed"),
    "spawn_worker_journal": sum(
        entry.get("operation") == "spawn_worker" and entry.get("target") == "rc-worker"
        for entry in journal
    ),
    "agent_dirs": sorted(path.name for path in (repo / ".exo/agents").iterdir() if path.is_dir()),
    "controller_epoch": (repo / ".exo/tl-loop/root.controller-epoch").read_text(),
    "controller_target": (repo / ".exo/tl-loop/root/controller-target.json").read_text(),
    "gates": root["gates"],
    "branches": subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        check=True, capture_output=True, text=True,
    ).stdout.split(),
    "head": subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip(),
}
print(json.dumps(snapshot, sort_keys=True, indent=2))
PY
}

durable_snapshot > "$ROLE_DIR/durable-before-repeat.json"
python3 - "$ROLE_DIR/durable-before-repeat.json" <<'PY'
import json, sys
snapshot = json.load(open(sys.argv[1]))
assert snapshot["spawned"] == 2, snapshot
assert snapshot["started"] == 1 and snapshot["finished"] == 1, snapshot
for key in ("intended", "requested", "accepted", "confirmed", "spawn_worker_journal"):
    assert snapshot[key] == 1, (key, snapshot)
PY

run_repeat() {
    local label="$1"
    local status=0
    (
        cd "$REPO"
        run_env "$EXOMONAD_BIN" init --continue --session "$SESSION" "${ROLE_FLAGS[@]}"
    ) > "$ROLE_DIR/init-repeat-$label.log" 2>&1 || status=$?
    if [[ "$status" -ne 0 ]] \
        && ! grep -Fq 'open terminal failed: not a terminal' "$ROLE_DIR/init-repeat-$label.log"; then
        cat "$ROLE_DIR/init-repeat-$label.log"
        fail "repeat init ($label) failed with status $status"
    fi
    if grep -q 'ERROR.*exomonad init failed' "$ROLE_DIR/init-repeat-$label.log"; then
        cat "$ROLE_DIR/init-repeat-$label.log"
        fail "repeat init ($label) reported a substantive failure"
    fi
    grep -Fq "identity=$SERVER_IDENTITY" "$ROLE_DIR/init-repeat-$label.log" \
        || { cat "$ROLE_DIR/init-repeat-$label.log"; fail "repeat init ($label) did not agree on the accepted identity"; }
    durable_snapshot > "$ROLE_DIR/durable-after-repeat-$label.json"
    diff -u "$ROLE_DIR/durable-before-repeat.json" "$ROLE_DIR/durable-after-repeat-$label.json" \
        > "$ROLE_DIR/duplicate-diff-$label.txt" \
        || { cat "$ROLE_DIR/duplicate-diff-$label.txt"; fail "repeat init ($label) duplicated an effect"; }
    say "PASS: repeat init ($label) added no duplicate launch, worker, controller, branch or dispatch"
}

run_repeat first
# Supported server-only restart: the same project configuration must converge
# on the same accepted snapshot, and the repeat must still add no effect.
say "restarting only the standalone server from the same project configuration"
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
for _ in $(seq 1 100); do
    [[ ! -S "$REPO/.exo/server.sock" ]] && break
    sleep 0.1
done
start_server "$ROLE_DIR/server-restarted.log"
[[ "$(health_identity)" == "$SERVER_IDENTITY" ]] \
    || fail "the restarted standalone server accepted a different identity"
run_repeat after-server-restart
ROLE_PASS_2="repeat init adds no duplicate effect"
say "PASS: $ROLE_PASS_2"

# ---------------------------------------------------------------------------
# Phase D: a server without a role identity must hold with unavailable (R4)
# ---------------------------------------------------------------------------
say "replacing the server with an old-binary-shaped /health that omits the role identity"
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
for _ in $(seq 1 100); do
    [[ ! -S "$REPO/.exo/server.sock" ]] && break
    sleep 0.1
done
python3 - "$REPO/.exo/server.sock" > "$ROLE_DIR/stub-server.log" 2>&1 <<'PY' &
import json, os, socket, sys
path = sys.argv[1]
try:
    os.unlink(path)
except FileNotFoundError:
    pass
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(8)
while True:
    connection, _ = server.accept()
    connection.recv(65536)
    body = json.dumps({"status": "ok", "version": "0.0.0", "wasm_hash": "old-binary"}).encode()
    connection.sendall(
        b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
        + f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
        + body
    )
    connection.close()
PY
STUB_PID=$!
for _ in $(seq 1 100); do
    [[ -S "$REPO/.exo/server.sock" ]] && curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health >/dev/null 2>&1 && break
    sleep 0.1
done
STALE_STATUS=0
durable_snapshot > "$ROLE_DIR/durable-before-stale.json"
(
    cd "$REPO"
    run_env "$EXOMONAD_BIN" init --continue --session "$SESSION" "${ROLE_FLAGS[@]}"
) > "$ROLE_DIR/init-stale-server.log" 2>&1 || STALE_STATUS=$?
[[ "$STALE_STATUS" -ne 0 ]] || { cat "$ROLE_DIR/init-stale-server.log"; fail "init accepted a server with no role identity"; }
grep -Fq "role_configuration_unavailable" "$ROLE_DIR/init-stale-server.log" \
    || { cat "$ROLE_DIR/init-stale-server.log"; fail "stale server did not hold with role_configuration_unavailable"; }
grep -Fq "did not expose its accepted role configuration" "$ROLE_DIR/init-stale-server.log" \
    || { cat "$ROLE_DIR/init-stale-server.log"; fail "stale-server hold was not actionable"; }
durable_snapshot > "$ROLE_DIR/durable-after-stale.json"
diff -u "$ROLE_DIR/durable-before-stale.json" "$ROLE_DIR/durable-after-stale.json" \
    > "$ROLE_DIR/stale-diff.txt" \
    || { cat "$ROLE_DIR/stale-diff.txt"; fail "the no-identity hold released or changed a controller, dispatch, gate or branch"; }
ROLE_PASS_4="server without an identity holds init with role_configuration_unavailable"
say "PASS: $ROLE_PASS_4"

# ---------------------------------------------------------------------------
# Phase E: the preflight regression itself (R1)
# ---------------------------------------------------------------------------
EVIDENCE_DIR="${E2E_ROLE_CONFIG_EVIDENCE_DIR:-}"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/check_role_config_preflight.py" \
    --source "$SOURCE_ROOT" ${EVIDENCE_DIR:+--evidence-dir "$EVIDENCE_DIR"} \
    > "$ROLE_DIR/preflight-guard.log" 2>&1 \
    || { cat "$ROLE_DIR/preflight-guard.log"; fail "R1 preflight guard rejected the production source"; }
cat "$ROLE_DIR/preflight-guard.log"
ROLE_PASS_5="preflight bypass is rejected (R1 proof guard and old-code-red evidence)"

# ---------------------------------------------------------------------------
# Fixed success contract: these lines, in this order, each also a PASS.
# ---------------------------------------------------------------------------
printf 'ROLECONFIG-1 standalone serve + identical flags -> one accepted correctly routed worker\n'
say "PASS: $ROLE_PASS_1"
printf 'ROLECONFIG-2 repeat init adds no duplicate effect\n'
say "PASS: $ROLE_PASS_2"
printf 'ROLECONFIG-3 genuine override held before dispatch; independent worker undisturbed\n'
say "PASS: $ROLE_PASS_3"
printf 'ROLECONFIG-4 server without an identity holds init with role_configuration_unavailable\n'
say "PASS: $ROLE_PASS_4"
printf 'ROLECONFIG-5 preflight bypass is rejected (cite the R1 evidence)\n'
say "PASS: $ROLE_PASS_5"
