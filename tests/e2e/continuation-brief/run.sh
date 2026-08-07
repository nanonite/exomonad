#!/usr/bin/env bash
set -euo pipefail

# Live continuation-brief E2E.
# Exercises the installed root SessionStart hook against an isolated project,
# Chainlink database, ExoMonad server, and current WASM/role bundle.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../lib/harness.sh
source "$PROJECT_ROOT/tests/e2e/lib/harness.sh"

TMUX_SESSION="e2e-continuation-brief"
SEEDED_CONTENT="E2E continuation brief seeded issue"
SEEDED_SESSION_FACT="E2E continuation brief seeded session fact"

if [[ -n "${E2E_SERVER_PORT:-}" ]]; then
    SERVER_PORT="$E2E_SERVER_PORT"
else
    SERVER_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi

cleanup() {
    local code=$?
    trap - EXIT

    if command -v tmux &>/dev/null; then
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    fi
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        e2e_log "Stopped exomonad serve"
    fi
    if [[ -n "${SERVER_LOG:-}" && -f "$SERVER_LOG" ]]; then
        e2e_log "Server log tail:"
        tail -n 20 "$SERVER_LOG" | sed 's/^/    /'
    fi
    if [[ "${KEEP_E2E_WORKDIR:-0}" == "1" ]]; then
        e2e_log "Keeping work dir: ${WORK_DIR:-unset}"
    elif [[ -n "${WORK_DIR:-}" ]]; then
        rm -rf "$WORK_DIR"
        e2e_log "Removed $WORK_DIR"
    fi
    echo ">>> Done."
    exit "$code"
}
trap cleanup EXIT

e2e_preflight git python3 chainlink tmux
e2e_create_work_dir continuation-brief
e2e_init_repo "Exomonad Continuation E2E" "continuation-e2e@example.com"
e2e_run_exomonad_new
e2e_chainlink_init
e2e_install_project_wasm_and_roles
e2e_write_basic_config "$TMUX_SESSION"
printf 'port = %s\n' "$SERVER_PORT" >> .exo/config.toml

export CHAINLINK_DB="$REPO_DIR/.chainlink"
export EXOMONAD_AGENT_ID=root
export EXOMONAD_ROLE=root
export EXOMONAD_SESSION_ID=main

e2e_phase "Phase 1" "Seeding isolated Chainlink state..."
chainlink session start >/dev/null
chainlink create "$SEEDED_CONTENT" -p low --label test --work >/dev/null
chainlink session action "$SEEDED_SESSION_FACT" >/dev/null

seeded_session_json="$(chainlink session status --json)"
python3 - "$seeded_session_json" "$SEEDED_CONTENT" "$SEEDED_SESSION_FACT" <<'PY'
import json
import sys

session, issue_title, session_fact = sys.argv[1:]
data = json.loads(session)
if data.get("active_issue", {}).get("title") != issue_title:
    raise SystemExit(f"seeded issue missing from session status: {data}")
if data.get("last_action") != session_fact:
    raise SystemExit(f"seeded session fact missing from session status: {data}")
PY
e2e_log "Seeded issue and session fact in $CHAINLINK_DB"

e2e_phase "Phase 2" "Starting isolated ExoMonad server..."
e2e_start_server

e2e_phase "Phase 3" "Invoking live root SessionStart hook..."
session_start_payload="$(python3 - "$REPO_DIR" <<'PY'
import json
import sys

repo = sys.argv[1]
print(json.dumps({
    "session_id": "continuation-brief-e2e-session",
    "hook_event_name": "SessionStart",
    "transcript_path": f"{repo}/transcript.jsonl",
    "cwd": repo,
    "permission_mode": "default",
    "source": "startup",
}))
PY
)"

hook_output="$(
    cd "$REPO_DIR"
    printf '%s' "$session_start_payload" \
        | "$EXOMONAD_BIN" hook session-start --runtime claude
)"

python3 - "$hook_output" "$SEEDED_CONTENT" <<'PY'
import json
import sys

raw, seeded_content = sys.argv[1:]
response = json.loads(raw)
specific = response.get("hookSpecificOutput") or {}
context = specific.get("additionalContext")
if not isinstance(context, str):
    raise SystemExit(f"SessionStart additionalContext missing: {raw}")

expected = (
    "Create a team using TeamCreate before proceeding.",
    "<exomonad-continuation-brief>",
    seeded_content,
)
missing = [value for value in expected if value not in context]
if missing:
    raise SystemExit(f"continuation brief missing {missing}: {context}")
if response.get("continue") is not True:
    raise SystemExit(f"SessionStart did not remain fail-open: {raw}")
print("  additionalContext contains TeamCreate, continuation marker, and seeded issue")
PY

echo ">>> PASS: live continuation-brief SessionStart E2E"
