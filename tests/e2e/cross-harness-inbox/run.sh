#!/usr/bin/env bash
set -euo pipefail

# E2E Cross-Harness Inbox Test
# Validates the SQLite InboxStore path for non-Claude agents:
# - send_tmux_message records durable unread mail
# - successful MCP calls piggyback <unread-mail>
# - check_inbox drains messages for a Gemini-shaped agent identity
# - list_agents reports unread/read metadata
# - watcher timeout poke injects a notification into the routed tmux pane

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

SESSION="e2e-cross-harness-inbox"
AGENT_NAME="cross-reader-gemini"
SINK_NAME="cross-sink-gemini"
MAIL_CONTENT="[E2E-INBOX-1] durable SQLite inbox message for Gemini agent"
MAIL_SUMMARY="durable inbox e2e"
PIGGYBACK_TRIGGER="[E2E-INBOX-2] trigger piggyback response"
POKE_TEXT="You have 1 unread message(s). Call check_inbox."

e2e_preflight curl git python3 tmux

e2e_phase "Phase 1" "Creating temp environment..."
e2e_create_work_dir "cross-harness-inbox"
e2e_install_cleanup_trap
tmux kill-session -t "$SESSION" 2>/dev/null || true

e2e_init_repo "Exomonad E2E" "e2e@example.com"
git remote add origin "http://localhost:3000/e2e/repo.git"
e2e_run_exomonad_new
e2e_install_project_wasm_and_roles
e2e_write_basic_config "$SESSION"
cat >> .exo/config.toml <<'EOF'
poll_interval = 1
inbox_poke_interval = 1
spawn_agent_type = "gemini"
EOF

e2e_phase "Phase 2" "Seeding Gemini-shaped agent and tmux route..."
tmux new-session -d -s "$SESSION" -n "TL" "bash --noprofile --norc"
PANE_ID="$(tmux display-message -p -t "$SESSION:TL.0" "#{pane_id}")"

mkdir -p ".exo/agents/$AGENT_NAME" ".exo/agents/$SINK_NAME"
python3 - "$AGENT_NAME" "$PANE_ID" <<'PY'
import json
import sys
from pathlib import Path

agent, pane = sys.argv[1:3]
agent_dir = Path(".exo/agents") / agent
identity = {
    "agent_name": agent,
    "slug": "cross-reader",
    "agent_type": "gemini",
    "birth_branch": "main",
    "parent_branch": "main",
    "working_dir": ".",
    "display_name": "cross-reader",
    "topology": "shared_dir",
}
routing = {"pane_id": pane, "parent_tab": "TL"}
(agent_dir / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
(agent_dir / "routing.json").write_text(json.dumps(routing, indent=2) + "\n")
PY

e2e_log "Repo: $REPO_DIR"
e2e_log "Server log: $SERVER_LOG"
e2e_log "Agent pane: $PANE_ID"

mkdir -p "$WORK_DIR/bin"
cat > "$WORK_DIR/bin/fj" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "${2:-}" == "list" ]]; then
    printf '[]\n'
    exit 0
fi
if [[ "$1" == "auth" && "${2:-}" == "status" ]]; then
    exit 0
fi
printf '[]\n'
EOF
chmod +x "$WORK_DIR/bin/fj"

e2e_phase "Phase 3" "Starting exomonad serve..."
e2e_start_server "EXOMONAD_TMUX_SESSION=$SESSION" "PATH=$WORK_DIR/bin:$PATH"

call_tool() {
    local role="${1:?role required}"
    local agent="${2:?agent required}"
    local tool="${3:?tool required}"
    local args_json="${4:?arguments JSON required}"
    local output="${5:?output path required}"

    python3 - "$tool" "$args_json" >"$WORK_DIR/request.json" <<'PY'
import json
import sys

tool, raw_args = sys.argv[1:3]
print(json.dumps({"name": tool, "arguments": json.loads(raw_args)}))
PY

    curl -fsS \
        --unix-socket "$REPO_DIR/.exo/server.sock" \
        -H "Content-Type: application/json" \
        -d @"$WORK_DIR/request.json" \
        "http://localhost/agents/$role/$agent/tools/call" \
        >"$output"
}

assert_json() {
    local output="${1:?output path required}"
    local check="${2:?check name required}"
    python3 - "$output" "$check" "$AGENT_NAME" "$MAIL_CONTENT" "$MAIL_SUMMARY" "$POKE_TEXT" <<'PY'
import json
import sys

path, check, agent, mail_content, mail_summary, poke_text = sys.argv[1:7]
data = json.loads(open(path).read())

def fail(message):
    print(json.dumps(data, indent=2))
    raise SystemExit(f"{check}: {message}")

if not data.get("success"):
    fail("top-level tool call failed")

result = data.get("result")
if check == "send-recorded":
    text = json.dumps(result)
    if "delivery_method" not in text:
        fail("send response missing delivery_method")
elif check == "piggyback":
    text = json.dumps(result)
    if "<unread-mail>" not in text:
        fail("missing unread-mail block")
    if mail_content not in text or mail_summary not in text:
        fail("unread-mail block missing sent message content or summary")
elif check == "check-inbox":
    if not isinstance(result, dict):
        fail("check_inbox result is not an object")
    if result.get("count") != 1:
        fail(f"expected one drained message, got {result.get('count')!r}")
    messages = result.get("messages") or []
    if not messages or messages[0].get("content") != mail_content:
        fail("check_inbox did not return sent message")
    if messages[0].get("summary") != mail_summary:
        fail("check_inbox did not preserve summary")
elif check == "list-unread":
    agents = (result or {}).get("agents") or []
    matches = [item for item in agents if item.get("agent_id") == agent]
    if not matches:
        fail(f"list_agents missing {agent}")
    if matches[0].get("agent_type") != "gemini":
        fail("list_agents did not report gemini agent type")
    if matches[0].get("has_unread") is not True:
        fail("list_agents did not report unread mail")
elif check == "list-read":
    agents = (result or {}).get("agents") or []
    matches = [item for item in agents if item.get("agent_id") == agent]
    if not matches:
        fail(f"list_agents missing {agent}")
    if matches[0].get("has_unread") is not False:
        fail("list_agents still reports unread mail after check_inbox")
    if not matches[0].get("last_check_inbox_at"):
        fail("list_agents missing last_check_inbox_at after check_inbox")
else:
    fail(f"unknown check {check}")
PY
}

wait_for_pane_text() {
    local expected="${1:?expected text required}"
    for _ in $(seq 1 30); do
        if tmux capture-pane -p -t "$PANE_ID" | grep -Fq "$expected"; then
            return 0
        fi
        sleep 0.5
    done
    echo "ERROR: timed out waiting for pane text: $expected"
    tmux capture-pane -p -t "$PANE_ID" | sed 's/^/  | /'
    exit 1
}

e2e_phase "Phase 4" "Sending durable inbox message through MCP..."
ROOT_SEND_ARGS="$(python3 - "$AGENT_NAME" "$MAIL_CONTENT" "$MAIL_SUMMARY" <<'PY'
import json
import sys

recipient, content, summary = sys.argv[1:4]
print(json.dumps({"recipient": recipient, "content": content, "summary": summary}))
PY
)"
call_tool root root send_tmux_message "$ROOT_SEND_ARGS" "$WORK_DIR/root-send.json"
assert_json "$WORK_DIR/root-send.json" send-recorded

python3 - "$REPO_DIR/.exo/inbox.db" "$AGENT_NAME" "$MAIL_CONTENT" <<'PY'
import sqlite3
import sys

db, agent, content = sys.argv[1:4]
conn = sqlite3.connect(db)
row = conn.execute(
    "SELECT from_agent, to_agent, content, read_at FROM messages WHERE to_agent = ?",
    (agent,),
).fetchone()
if row is None:
    raise SystemExit("message was not recorded in InboxStore")
if row[1] != agent or row[2] != content or row[3] is not None:
    raise SystemExit(f"unexpected inbox row: {row!r}")
PY
e2e_log "InboxStore row recorded"

e2e_phase "Phase 5" "Verifying list_agents unread metadata..."
call_tool root root list_agents '{"filter_type":"gemini"}' "$WORK_DIR/list-unread.json"
assert_json "$WORK_DIR/list-unread.json" list-unread

e2e_phase "Phase 6" "Verifying piggyback unread mail on non-Claude MCP call..."
TRIGGER_ARGS="$(python3 - "$SINK_NAME" "$PIGGYBACK_TRIGGER" <<'PY'
import json
import sys

recipient, content = sys.argv[1:3]
print(json.dumps({"recipient": recipient, "content": content, "summary": "piggyback trigger"}))
PY
)"
call_tool worker "$AGENT_NAME" send_tmux_message "$TRIGGER_ARGS" "$WORK_DIR/piggyback.json"
assert_json "$WORK_DIR/piggyback.json" piggyback

e2e_phase "Phase 7" "Verifying explicit check_inbox drains unread mail..."
call_tool worker "$AGENT_NAME" check_inbox '{}' "$WORK_DIR/check-inbox.json"
assert_json "$WORK_DIR/check-inbox.json" check-inbox
call_tool root root list_agents '{"filter_type":"gemini"}' "$WORK_DIR/list-read.json"
assert_json "$WORK_DIR/list-read.json" list-read

e2e_phase "Phase 8" "Verifying watcher timeout poke reaches routed pane..."
SECOND_CONTENT="[E2E-INBOX-3] unread message for watcher poke"
SECOND_ARGS="$(python3 - "$AGENT_NAME" "$SECOND_CONTENT" <<'PY'
import json
import sys

recipient, content = sys.argv[1:3]
print(json.dumps({"recipient": recipient, "content": content, "summary": "watcher poke"}))
PY
)"
call_tool root root send_tmux_message "$SECOND_ARGS" "$WORK_DIR/root-send-second.json"
assert_json "$WORK_DIR/root-send-second.json" send-recorded
wait_for_pane_text "$POKE_TEXT"
e2e_log "Watcher poke observed in routed tmux pane"

echo ">>> PASS: Cross-harness InboxStore E2E"
