#!/usr/bin/env bash
set -euo pipefail

# E2E Claude Teams inbox review-chain test.
# Validates Claude TL -> Claude dev leaf -> Claude reviewer -> Teams inbox merge-ready delivery.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
SESSION="e2e-claude-teams-$(date +%s)-$$"

log() {
    printf '[claude-teams-e2e] %s\n' "$*"
}

fail() {
    log "FAIL: $*"
    dump_debug
    exit 1
}

capture_window() {
    local window="$1"
    tmux capture-pane -p -t "${SESSION}:${window}" 2>/dev/null || true
}

dump_debug() {
    if [[ -n "${WORK_DIR:-}" && -f "$WORK_DIR/init.log" ]]; then
        log "init log tail:"
        tail -n 80 "$WORK_DIR/init.log" | sed 's/^/[init] /' || true
    fi
    if [[ -n "${SERVER_LOG:-}" && -f "$SERVER_LOG" ]]; then
        log "server log tail:"
        tail -n 80 "$SERVER_LOG" | sed 's/^/[server-log] /' || true
    fi
    if [[ -n "${MOCK_LOG:-}" && -f "$MOCK_LOG" ]]; then
        log "mock API log:"
        tail -n 80 "$MOCK_LOG" | sed 's/^/[mock] /' || true
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        log "tmux windows:"
        tmux list-windows -t "$SESSION" | sed 's/^/[tmux] /' || true
        log "server pane tail:"
        capture_window Server | tail -n 80 | sed 's/^/[server] /' || true
        log "TL pane tail:"
        capture_window TL | tail -n 80 | sed 's/^/[tl] /' || true
    fi
}

trust_claude_project() {
    local project_path="$1"
    local mcp_role="${2:-root}"
    local mcp_name="${3:-root}"
    python3 - "$project_path" "$mcp_role" "$mcp_name" <<'PY'
import json
import sys
from pathlib import Path

project = str(Path(sys.argv[1]).resolve())
mcp_role = sys.argv[2]
mcp_name = sys.argv[3]
claude_json = Path.home() / ".claude.json"
try:
    data = json.loads(claude_json.read_text()) if claude_json.exists() else {}
except json.JSONDecodeError:
    data = {}
entry = data.setdefault("projects", {}).setdefault(project, {})
for key in [
    "allowedTools",
    "disabledMcpjsonServers",
    "enabledMcpjsonServers",
    "exampleFiles",
    "mcpContextUris",
]:
    entry.setdefault(key, [])
entry.setdefault("mcpServers", {})["exomonad"] = {
    "type": "stdio",
    "command": "bash",
    "args": [
        "-lc",
        "exec exomonad mcp-stdio --role ${EXOMONAD_ROLE:-root} --name ${EXOMONAD_AGENT_ID:-root}",
    ],
    "env": {},
}
entry.setdefault("projectOnboardingSeenCount", 0)
enabled = entry.setdefault("enabledMcpjsonServers", [])
if "exomonad" not in enabled:
    enabled.append("exomonad")
entry["disabledMcpjsonServers"] = [
    name for name in entry.get("disabledMcpjsonServers", []) if name != "exomonad"
]
entry["hasTrustDialogAccepted"] = True
entry["hasClaudeMdExternalIncludesApproved"] = False
entry["hasClaudeMdExternalIncludesWarningShown"] = False
entry["hasCompletedProjectOnboarding"] = True
claude_json.parent.mkdir(parents=True, exist_ok=True)
tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
tmp.replace(claude_json)
PY
}

record_team_baseline() {
    local baseline_file="$1"
    if [[ -d "$HOME/.claude/teams" ]]; then
        find "$HOME/.claude/teams" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort > "$baseline_file"
    else
        : > "$baseline_file"
    fi
}

pick_port() {
    python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

cleanup() {
    local code=$?
    log "cleanup: killing tmux session ${SESSION}"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [[ -n "${MOCK_PID:-}" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
    fi
    if [[ -f "${RESULT_FILE:-}" ]]; then
        log "validator result:"
        sed 's/^/[result] /' "$RESULT_FILE" || true
    fi
    if [[ "$code" != "0" ]]; then
        dump_debug
    fi
    if [[ "${KEEP_E2E_WORKDIR:-0}" == "1" ]]; then
        log "keeping work dir: ${WORK_DIR:-unset}"
    elif [[ -n "${WORK_DIR:-}" ]]; then
        rm -rf "$WORK_DIR"
    fi
    exit "$code"
}
trap cleanup EXIT

log "checking preconditions"
EXOMONAD_BIN=""
if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
    export PATH="$PROJECT_ROOT/target/debug:$PATH"
elif command -v exomonad >/dev/null 2>&1; then
    EXOMONAD_BIN="$(command -v exomonad)"
else
    fail "exomonad binary not found. Run cargo build -p exomonad or just install-all-dev."
fi
command -v claude >/dev/null 2>&1 || fail "claude binary not found in PATH"
for cmd in tmux git python3 curl chainlink; do
    command -v "$cmd" >/dev/null 2>&1 || fail "$cmd not found in PATH"
done
[[ -d "$PROJECT_ROOT/.exo/wasm" ]] || fail "missing $PROJECT_ROOT/.exo/wasm; run just wasm-all"
ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm >/dev/null 2>&1 || fail "no WASM guests found in $PROJECT_ROOT/.exo/wasm"

for tool in spawn_leaf file_pr; do
    grep -q "$tool" "$PROJECT_ROOT/.exo/wasm/wasm-guest-devswarm.wasm" 2>/dev/null || fail "MCP tool '$tool' missing from WASM binary"
done

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/claude-teams.XXXXXXXX")"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
MOCK_LOG="$WORK_DIR/mock-github.log"
MOCK_SERVER_LOG="$WORK_DIR/mock-github-server.log"
RESULT_FILE="$WORK_DIR/validation-result.txt"
TEAM_BASELINE="$WORK_DIR/team-baseline.txt"
MOCK_PORT="$(pick_port)"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
log "work dir: $WORK_DIR"

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
echo '# Claude Teams Inbox E2E Fixture' > README.md
git add README.md
git commit -m "initial commit" -q
git push -u origin main -q

"$EXOMONAD_BIN" new >"$WORK_DIR/new.log" 2>&1 || {
    sed 's/^/[exomonad-new] /' "$WORK_DIR/new.log"
    fail "exomonad new failed"
}
chainlink init >"$WORK_DIR/chainlink-init.log" 2>&1 || {
    sed 's/^/[chainlink-init] /' "$WORK_DIR/chainlink-init.log"
    fail "chainlink init failed"
}
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

record_team_baseline "$TEAM_BASELINE"
trust_claude_project "$REPO_DIR" root root
trust_claude_project "$REPO_DIR/.exo/worktrees/teams-inbox-dev-claude" dev teams-inbox-dev-claude
trust_claude_project "$REPO_DIR/.exo/worktrees/review-pr-1-claude" reviewer review-pr-1-claude

ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" <<'PY'
import pathlib
import sys
value = pathlib.Path(sys.argv[1]).read_text()
print(value.replace('"""', '\"\"\"'))
PY
)"

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
port = 0
root_agent_type = "claude"
spawn_agent_type = "claude"
model = "claude-haiku-4-5-20251001"
yolo = true
poll_interval = 5
forgejo_url = "$MOCK_URL"
forgejo_token = "author-token"
forgejo_reviewer_token = "reviewer-token"
initial_prompt = """$ROOT_PROMPT"""

[reviewer]
agent_type = "claude"
model = "claude-haiku-4-5-20251001"
EOF

log "starting mock Forgejo API at $MOCK_URL"
MOCK_LOG="$MOCK_LOG" REMOTE_DIR="$REMOTE_DIR" python3 "$E2E_DIR/mock_github.py" --port "$MOCK_PORT" >"$MOCK_SERVER_LOG" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 40); do
    if curl -fsS "$MOCK_URL/api/v1/repos/e2e/repo/pulls" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$MOCK_PID" 2>/dev/null; then
        cat "$MOCK_SERVER_LOG"
        fail "mock Forgejo API exited early"
    fi
    sleep 0.25
done
curl -fsS "$MOCK_URL/api/v1/repos/e2e/repo/pulls" >/dev/null || fail "mock Forgejo API did not become ready"

log "launching exomonad init in tmux session $SESSION"
set +e
FORGEJO_TOKEN="author-token" \
FORGEJO_REVIEWER_TOKEN="reviewer-token" \
FORGEJO_URL="$MOCK_URL" \
EXOMONAD_LOG_FORMAT="" \
"$EXOMONAD_BIN" init --verbose --session "$SESSION" >"$WORK_DIR/init.log" 2>&1 &
INIT_PID=$!
set -e

"$SCRIPT_DIR/validate.sh" "$REPO_DIR" "$SESSION" "$RESULT_FILE" "$MOCK_LOG" "$TEAM_BASELINE"

kill "$INIT_PID" 2>/dev/null || true
wait "$INIT_PID" 2>/dev/null || true
log "PASS: Claude Teams inbox review-chain E2E completed"
