#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
# shellcheck source=../lib/git-fixture.sh
source "$PROJECT_ROOT/tests/e2e/lib/git-fixture.sh"
SESSION="e2e-review-loop-stuck-$(date +%s)-$$"
BRANCH="main.review-loop-dev"
PR_NUMBER=1

log() {
    printf '[review-loop-stuck-e2e] %s\n' "$*"
}

fail() {
    log "FAIL: $*"
    dump_debug
    exit 1
}

pick_port() {
    python3 - <<'PYPORT'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PYPORT
}

dump_debug() {
    if [[ -n "${SERVER_LOG:-}" && -f "$SERVER_LOG" ]]; then
        log "server log tail:"
        tail -n 120 "$SERVER_LOG" | sed 's/^/[server] /' || true
    fi
    if [[ -n "${MOCK_LOG:-}" && -f "$MOCK_LOG" ]]; then
        log "mock API log tail:"
        tail -n 120 "$MOCK_LOG" | sed 's/^/[mock] /' || true
    fi
    if [[ -n "${REPO_DIR:-}" && -f "$REPO_DIR/.exo/watcher-state.json" ]]; then
        log "watcher state:"
        sed 's/^/[watcher-state] /' "$REPO_DIR/.exo/watcher-state.json" || true
    fi
    if [[ -n "${REPO_DIR:-}" && -d "$REPO_DIR/.chainlink" ]]; then
        log "review-stuck issues:"
        (cd "$REPO_DIR" && chainlink list --label review-stuck --status all) 2>&1 | sed 's/^/[chainlink] /' || true
    fi
}

cleanup() {
    local code=$?
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [[ -n "${MOCK_PID:-}" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
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

wait_for() {
    local label="$1"
    local command="$2"
    local timeout="${3:-60}"
    local deadline=$((SECONDS + timeout))
    while (( SECONDS < deadline )); do
        if bash -lc "$command" >/dev/null 2>&1; then
            log "OK: $label"
            return 0
        fi
        sleep 1
    done
    fail "$label timed out after ${timeout}s"
}

require_commands() {
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || fail "$cmd not found in PATH"
    done
}

find_exomonad() {
    if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
        EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
        export PATH="$PROJECT_ROOT/target/debug:$PATH"
    elif command -v exomonad >/dev/null 2>&1; then
        EXOMONAD_BIN="$(command -v exomonad)"
    else
        fail "exomonad binary not found. Run cargo build -p exomonad or just build."
    fi
}

json_payload() {
    python3 - "$@" <<'PYJSON'
import json
import sys
mode = sys.argv[1]
if mode == "pull":
    _, _, title, head, base, body = sys.argv
    print(json.dumps({"title": title, "head": head, "base": base, "body": body}))
elif mode == "review":
    _, _, pr_number, state, body, commit_id, login = sys.argv
    print(json.dumps({
        "pr_number": int(pr_number),
        "state": state,
        "body": body,
        "commit_id": commit_id,
        "login": login,
    }))
PYJSON
}

create_pr() {
    local body="$1"
    json_payload pull "Review loop stuck fixture" "$BRANCH" main "$body" \
        | curl -fsS -X POST "$MOCK_URL/api/v1/repos/e2e/repo/pulls" \
            -H 'Content-Type: application/json' \
            -d @- >/dev/null
}

post_review() {
    local state="$1"
    local body="$2"
    local commit_id="$3"
    local login="$4"
    json_payload review "$PR_NUMBER" "$state" "$body" "$commit_id" "$login" \
        | curl -fsS -X POST "$MOCK_URL/_control/reviews" \
            -H 'Content-Type: application/json' \
            -d @- >/dev/null
}

assert_watcher_state() {
    local repo_dir="$1"
    python3 - "$repo_dir/.exo/watcher-state.json" <<'PYASSERT'
import json
import sys
from pathlib import Path
state_path = Path(sys.argv[1])
if not state_path.exists():
    raise SystemExit(1)
data = json.loads(state_path.read_text())
pr = data.get("prs", {}).get("1")
if not pr:
    raise SystemExit(1)
if pr.get("rounds", 0) < 2:
    raise SystemExit(1)
if pr.get("stuck") is not True:
    raise SystemExit(1)
if pr.get("needs_human_review") is not True:
    raise SystemExit(1)
PYASSERT
}

assert_chainlink_escalation() {
    local repo_dir="$1"
    local expected_sha="$2"
    python3 - "$repo_dir" "$expected_sha" <<'PYASSERT'
import json
import subprocess
import sys
repo_dir, expected_sha = sys.argv[1:3]
base = ["chainlink", "--db", f"{repo_dir}/.chainlink"]
raw = subprocess.check_output(base + ["list", "--json", "--label", "review-stuck", "--priority", "high"], text=True)
data = json.loads(raw)
issues = data if isinstance(data, list) else data.get("issues", [])
for issue in issues:
    issue_id = issue.get("id") or issue.get("number")
    if issue_id is None:
        continue
    detail_raw = subprocess.check_output(base + ["show", str(issue_id), "--json"], text=True)
    detail = json.loads(detail_raw)
    text = json.dumps(detail)
    required = ["PR #1", "review-loop-dev", "rounds", expected_sha, "dev_not_pushing"]
    if all(value in text for value in required):
        raise SystemExit(0)
raise SystemExit(1)
PYASSERT
}

assert_pr_open() {
    local mock_url="$1"
    python3 - "$mock_url" <<'PYASSERT'
import json
import sys
import urllib.request
url = sys.argv[1] + "/api/v1/repos/e2e/repo/pulls"
with urllib.request.urlopen(url, timeout=5) as response:
    prs = json.loads(response.read().decode())
for pr in prs:
    if pr.get("number") == 1 and pr.get("state") == "open":
        raise SystemExit(0)
raise SystemExit(1)
PYASSERT
}

assert_no_tl_stuck_message() {
    local repo_dir="$1"
    local server_log="$2"
    if grep -R '\[STUCK:' "$repo_dir/.exo/logs" "$server_log" 2>/dev/null | grep -q .; then
        return 1
    fi
}

assert_no_auto_close_or_merge() {
    local mock_log="$1"
    python3 - "$mock_log" <<'PYASSERT'
import json
import re
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = entry.get("method")
    path = entry.get("path", "")
    if method in {"POST", "PUT"} and re.search(r"/pulls/1/merge$", path):
        raise SystemExit(1)
    if method == "PATCH" and re.search(r"/pulls/1$", path):
        raise SystemExit(1)
raise SystemExit(0)
PYASSERT
}

case "${1:-}" in
    --assert-watcher-state)
        assert_watcher_state "${2:?repo dir required}" "${3:?head sha required}"
        exit 0
        ;;
    --assert-chainlink-escalation)
        assert_chainlink_escalation "${2:?repo dir required}" "${3:?head sha required}"
        exit 0
        ;;
    --assert-pr-open)
        assert_pr_open "${2:?mock url required}"
        exit 0
        ;;
    --assert-no-tl-stuck-message)
        assert_no_tl_stuck_message "${2:?repo dir required}" "${3:?server log required}"
        exit 0
        ;;
    --assert-no-auto-close-or-merge)
        assert_no_auto_close_or_merge "${2:?mock log required}"
        exit 0
        ;;
esac

log "checking preconditions"
find_exomonad
require_commands git python3 curl chainlink
[[ -d "$PROJECT_ROOT/.exo/wasm" ]] || fail "missing $PROJECT_ROOT/.exo/wasm; run just wasm-all"
ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm >/dev/null 2>&1 || fail "no WASM guests found in $PROJECT_ROOT/.exo/wasm"

mkdir -p "${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}"
WORK_DIR="$(mktemp -d "${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}/review-loop-stuck.XXXXXXXX")"
e2e_git_use_fixture_root "$WORK_DIR"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
MOCK_LOG="$WORK_DIR/mock-forgejo.log"
MOCK_SERVER_LOG="$WORK_DIR/mock-forgejo-server.log"
SERVER_LOG="$WORK_DIR/exomonad-serve.log"
MOCK_PORT="$(pick_port)"
SERVER_PORT="$(pick_port)"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
log "work dir: $WORK_DIR"

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git remote add bare "$REMOTE_DIR"
git remote add origin "$MOCK_URL/e2e/repo"
echo '# Review Loop Stuck E2E Fixture' > README.md
git add README.md
git commit -m "initial commit" -q
git push bare main -q

"$EXOMONAD_BIN" new >"$WORK_DIR/new.log" 2>&1 || fail "exomonad new failed"
chainlink init >"$WORK_DIR/chainlink-init.log" 2>&1 || fail "chainlink init failed"
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

cat > .exo/review-policy.toml <<'EOF'
min_review_rounds = 1
reviewer_max_rounds = 2
reviewer_max_wait_seconds = 1200
review_freshness_window_secs = 1200
external_review_threshold = 300
external_review_paths = []
reviewer_max_rate_limit_retries = 2
require_second_reviewer_complexity = false
complexity_line_threshold = 500
max_leaf_session_seconds = 3600
max_reviewer_session_seconds = 600
EOF

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
yolo = true
port = $SERVER_PORT
poll_interval = 1
forgejo_url = "$MOCK_URL"
forgejo_token = "author-token"
forgejo_reviewer_token = "reviewer-token"
EOF

log "starting mock Forgejo API at $MOCK_URL"
MOCK_LOG="$MOCK_LOG" REMOTE_DIR="$REMOTE_DIR" python3 "$E2E_DIR/mock_github.py" --port "$MOCK_PORT" >"$MOCK_SERVER_LOG" 2>&1 &
MOCK_PID=$!
wait_for "mock Forgejo API ready" "curl -fsS '$MOCK_URL/api/v1/repos/e2e/repo/pulls'" 20

git checkout -q -b "$BRANCH"
echo 'round one' > review-loop.txt
git add review-loop.txt
git commit -m "round one" -q
SHA1="$(git rev-parse HEAD)"
git push bare "$BRANCH" -q
PR_BODY=$'Authoring-Agent: review-loop-dev-codex\nAuthoring-Role: dev\nBirth-Branch: main.review-loop-dev\nReviewer-Agent: review-pr-1-codex\nReviewer-Birth-Branch: review-pr-1\nChainlink-Issue: #503'
create_pr "$PR_BODY"
post_review CHANGES_REQUESTED "Round 1: request changes at $SHA1" "$SHA1" "reviewer-one"

echo 'round two' >> review-loop.txt
git add review-loop.txt
git commit -m "round two" -q
SHA2="$(git rev-parse HEAD)"
git push bare "$BRANCH" -q
post_review CHANGES_REQUESTED "Round 2: request changes at $SHA2" "$SHA2" "reviewer-two"

log "starting exomonad serve"
RUST_LOG="${RUST_LOG:-info}" EXOMONAD_LOG_FORMAT="" "$EXOMONAD_BIN" serve >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
wait_for "server socket ready" "test -S '$REPO_DIR/.exo/server.sock'" 30
HARNESS="$SCRIPT_DIR/run.sh"
wait_for "watcher marked PR stuck" "bash '$HARNESS' --assert-watcher-state '$REPO_DIR' '$SHA2'" 60
wait_for "Chainlink review-stuck escalation filed" "bash '$HARNESS' --assert-chainlink-escalation '$REPO_DIR' '$SHA2'" 60
wait_for "mock PR remains open" "bash '$HARNESS' --assert-pr-open '$MOCK_URL'" 10
wait_for "no TL [STUCK] message emitted" "bash '$HARNESS' --assert-no-tl-stuck-message '$REPO_DIR' '$SERVER_LOG'" 10
wait_for "watcher did not auto-close or merge the PR" "bash '$HARNESS' --assert-no-auto-close-or-merge '$MOCK_LOG'" 10

log "PASS: review-loop stuck escalation E2E completed"
