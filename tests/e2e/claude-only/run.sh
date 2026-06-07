#!/usr/bin/env bash
set -euo pipefail

# E2E Claude-only bounded smoke test
# Validates a Claude root TL can start, register SessionStart, call TeamCreate,
# and register the Claude Teams metadata without asking the TL to implement.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
SESSION="e2e-claude-only-$(date +%s)-$$"

log() {
    printf '[claude-only-e2e] %s\n' "$*"
}

dump_debug() {
    if [[ -n "${WORK_DIR:-}" && -f "$WORK_DIR/init.log" ]]; then
        log "init log tail:"
        tail -n 80 "$WORK_DIR/init.log" | sed 's/^/[init] /' || true
    fi
    if [[ -n "${SESSION:-}" ]] && tmux has-session -t "$SESSION" 2>/dev/null; then
        log "tmux windows:"
        tmux list-windows -t "$SESSION" | sed 's/^/[tmux] /' || true
        log "server pane tail:"
        capture_window Server | tail -n 80 | sed 's/^/[server] /' || true
        log "TL pane tail:"
        capture_window TL | tail -n 80 | sed 's/^/[tl] /' || true
    fi
}

fail() {
    log "FAIL: $*"
    dump_debug
    exit 1
}

wait_for() {
    local label="$1"
    local timeout_secs="$2"
    local command="$3"
    local elapsed=0
    while (( elapsed < timeout_secs )); do
        if bash -lc "$command" >/dev/null 2>&1; then
            log "PASS: $label"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    fail "$label timed out after ${timeout_secs}s"
}

capture_window() {
    local window="$1"
    tmux capture-pane -p -t "${SESSION}:${window}" 2>/dev/null || true
}

trust_claude_project() {
    local project_path="$1"
    python3 - "$project_path" <<'INNER_PY'
import json
import sys
from pathlib import Path

project = str(Path(sys.argv[1]).resolve())
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
entry.setdefault("mcpServers", {})
entry.setdefault("projectOnboardingSeenCount", 0)
entry["hasTrustDialogAccepted"] = True
entry["hasClaudeMdExternalIncludesApproved"] = False
entry["hasClaudeMdExternalIncludesWarningShown"] = False
entry["hasCompletedProjectOnboarding"] = True
claude_json.parent.mkdir(parents=True, exist_ok=True)
tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
tmp.replace(claude_json)
INNER_PY
}

record_team_baseline() {
    local baseline_file="$1"
    if [[ -d "$HOME/.claude/teams" ]]; then
        find "$HOME/.claude/teams" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort > "$baseline_file"
    else
        : > "$baseline_file"
    fi
}

new_teams() {
    local baseline_file="$1"
    local current
    current="$(mktemp)"
    find "$HOME/.claude/teams" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort > "$current"
    comm -13 "$baseline_file" "$current" 2>/dev/null || true
    rm -f "$current"
}

assert_exomonad_mcp_not_disabled() {
    local settings_path="$REPO_DIR/.claude/settings.local.json"
    [[ -f "$settings_path" ]] || fail "missing generated Claude settings at $settings_path"
    python3 - "$settings_path" <<'INNER_PY' || fail "generated Claude settings disables exomonad MCP server"
import json
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text())
if settings.get("_exomonad_generated") is not True:
    print("settings.local.json is missing _exomonad_generated marker", file=sys.stderr)
    sys.exit(1)
disabled = settings.get("disabledMcpjsonServers", [])
if isinstance(disabled, list) and "exomonad" in disabled:
    print("disabledMcpjsonServers contains exomonad", file=sys.stderr)
    sys.exit(1)
INNER_PY
}

cleanup() {
    local code=$?
    log "cleanup: killing tmux session ${SESSION}"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
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
for cmd in tmux git python3; do
    command -v "$cmd" >/dev/null 2>&1 || fail "$cmd not found in PATH"
done
[[ -d "$PROJECT_ROOT/.exo/wasm" ]] || fail "missing $PROJECT_ROOT/.exo/wasm; run just wasm-all"
ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm >/dev/null 2>&1 || fail "no WASM guests found in $PROJECT_ROOT/.exo/wasm"

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/claude-only.XXXXXXXX")"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
log "work dir: $WORK_DIR"

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git commit --allow-empty -m "initial commit" -q
git push -u origin main -q

"$EXOMONAD_BIN" new >/tmp/exomonad-claude-only-new.log 2>&1 || {
    sed 's/^/[exomonad-new] /' /tmp/exomonad-claude-only-new.log
    fail "exomonad new failed"
}
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done

BASELINE_FILE="$REPO_DIR/.exo/e2e-team-baseline.txt"
record_team_baseline "$BASELINE_FILE"
trust_claude_project "$REPO_DIR"

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
port = 0
root_agent_type = "claude"
model = "claude-haiku-4-5-20251001"
yolo = true
initial_prompt = """Automated bounded ExoMonad Claude-only smoke test. Do exactly one MCP action: call TeamCreate to create a team for this session. After TeamCreate returns, stop immediately. Do not write files. Do not spawn agents. Do not run commands. Do not inspect files. Do not explain."""
EOF

log "launching exomonad init in tmux session $SESSION"
set +e
FORGEJO_TOKEN="test-token-e2e" "$EXOMONAD_BIN" init --session "$SESSION" >"$WORK_DIR/init.log" 2>&1 &
INIT_PID=$!
set -e

wait_for "tmux session created" 30 "tmux has-session -t '$SESSION'"
assert_exomonad_mcp_not_disabled
wait_for "server accepted connections" 45 "tmux capture-pane -p -t '$SESSION:Server' | grep -q 'Plugins ready, accepting connections'"
wait_for "Claude root session registered" 45 "tmux capture-pane -p -t '$SESSION:Server' | grep -q 'Registering Claude session'"
wait_for "TeamCreate registered team" 90 "tmux capture-pane -p -t '$SESSION:Server' | grep -q 'Registered team:'"
wait_for "new Claude team directory created" 10 "test -n \"\$(comm -13 '$BASELINE_FILE' <(find '$HOME/.claude/teams' -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null | sort) 2>/dev/null)\""

if capture_window TL | grep -q 'TL agents cannot use Write'; then
    fail "TL attempted a direct Write; harness prompt is not role-safe"
fi
if capture_window TL | grep -Eq 'spawn_leaf|spawn_worker|fork_wave'; then
    fail "TL appeared to discuss spawning in bounded smoke; harness prompt is too broad"
fi

TEAM_NAME="$(new_teams "$BASELINE_FILE" | head -n 1)"
log "PASS: Claude-only bounded smoke completed"
log "team: ${TEAM_NAME:-unknown}"
log "session: $SESSION"
log "server log tail:"
capture_window Server | tail -n 40 | sed 's/^/[server] /'

# exomonad init exits with non-zero in non-TTY after creating the session because tmux attach fails.
# That is expected for this noninteractive harness; the assertions above validate the live session.
kill "$INIT_PID" 2>/dev/null || true
wait "$INIT_PID" 2>/dev/null || true
