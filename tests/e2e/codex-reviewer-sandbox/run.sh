#!/usr/bin/env bash
set -euo pipefail

# Regression test for the Codex reviewer / sandbox mismatch: for a while,
# CODEX_REVIEWER_INSTRUCTIONS told Codex reviewers to submit their final
# verdict with `curl`/`fj` against Forgejo directly from their own shell,
# while the Codex reviewer's own sandbox profile (codex_config.rs,
# `permissions.reviewer`) sets `network_access = false`. Every Codex
# reviewer was structurally unable to submit a review. See
# docs/decisions/agent-sandbox-profiles.md and docs/decisions/codex-integration.md.
#
# This harness drives the real worktree event watcher (real `exomonad serve`,
# no mocked spawn logic) against a mock Forgejo API and lets it auto-spawn a
# real Codex reviewer worktree for a freshly observed PR. It then inspects the
# *actual* generated `.codex/config.toml` on disk and asserts the sandbox
# profile and the developer instructions are mutually consistent: if the
# sandbox has no network access, the instructions must not tell the reviewer
# to reach Forgejo over the network from its own shell. It also asserts the
# `.exo/server.sock` symlink — whose supposed absence was the stated reason
# for the curl-based rewrite — is present, proving that justification never
# held for the normal spawn path.
#
# This does not require the `codex` binary: it only exercises ExoMonad's own
# spawn/config-generation code path, not a live Codex process.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
# shellcheck source=../lib/git-fixture.sh
source "$PROJECT_ROOT/tests/e2e/lib/git-fixture.sh"
SESSION="e2e-codex-reviewer-sandbox-$(date +%s)-$$"
BRANCH="main.codex-reviewer-sandbox-dev"
AUTHOR_AGENT="codex-reviewer-sandbox-dev-codex"
PR_NUMBER=1

log() {
    printf '[codex-reviewer-sandbox-e2e] %s\n' "$*"
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
        tail -n 150 "$SERVER_LOG" | sed 's/^/[server] /' || true
    fi
    if [[ -n "${MOCK_LOG:-}" && -f "$MOCK_LOG" ]]; then
        log "mock API log tail:"
        tail -n 60 "$MOCK_LOG" | sed 's/^/[mock] /' || true
    fi
    if [[ -n "${REPO_DIR:-}" ]]; then
        log "worktrees present:"
        find "$REPO_DIR/.exo/worktrees" -maxdepth 1 -mindepth 1 2>/dev/null | sed 's/^/[worktrees] /' || true
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

create_pr() {
    python3 - "$MOCK_URL" "$BRANCH" "$AUTHOR_AGENT" <<'PYJSON'
import json
import sys
import urllib.request
mock_url, branch, author_agent = sys.argv[1:4]
body = f"Authoring-Agent: {author_agent}\nAuthoring-Role: dev\nBirth-Branch: {branch}"
payload = json.dumps({
    "title": "Codex reviewer sandbox fixture",
    "head": branch,
    "base": "main",
    "body": body,
}).encode()
req = urllib.request.Request(
    f"{mock_url}/api/v1/repos/e2e/repo/pulls",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=5).read()
PYJSON
}

assert_reviewer_config_consistent() {
    local config_path="$1"
    python3 - "$config_path" <<'PYASSERT'
import sys
import tomllib
path = sys.argv[1]
with open(path, "rb") as f:
    config = tomllib.load(f)

instructions = config.get("developer_instructions", "")
lower = instructions.lower()

network_access = config["permissions"]["reviewer"]["network_access"]
if network_access is not False:
    print(f"expected permissions.reviewer.network_access = false, got {network_access!r}", file=sys.stderr)
    raise SystemExit(1)

# The regression: instructions told the reviewer to hit Forgejo directly from
# its own (network-disabled) shell. If network access is off, the verdict
# path must never require it. Check for actual shell invocations, not just
# the word "curl"/"fj" — instructions may mention them by name to explain
# why the reviewer must not invoke them directly.
for banned in ("curl -", "curl http", "fj pr review", "fj pr view", "fj pr files"):
    if banned in lower:
        print(f"reviewer instructions require sandboxed network access ({banned!r}) but network_access=false", file=sys.stderr)
        raise SystemExit(1)

for required in ("approve_pr", "request_changes"):
    if required not in instructions:
        print(f"reviewer instructions must submit verdicts through the {required} MCP tool", file=sys.stderr)
        raise SystemExit(1)
PYASSERT
}

case "${1:-}" in
    --assert-reviewer-config)
        assert_reviewer_config_consistent "${2:?config path required}"
        exit 0
        ;;
esac

log "checking preconditions"
find_exomonad
require_commands git python3 curl
[[ -d "$PROJECT_ROOT/.exo/wasm" ]] || fail "missing $PROJECT_ROOT/.exo/wasm; run just wasm-all"
ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm >/dev/null 2>&1 || fail "no WASM guests found in $PROJECT_ROOT/.exo/wasm"
python3 -c "import tomllib" 2>/dev/null || fail "python3 tomllib not available (need Python 3.11+)"

mkdir -p "${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}"
WORK_DIR="$(mktemp -d "${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}/codex-reviewer-sandbox.XXXXXXXX")"
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
echo '# Codex Reviewer Sandbox E2E Fixture' > README.md
git add README.md
git commit -m "initial commit" -q
git push bare main -q

"$EXOMONAD_BIN" new >"$WORK_DIR/new.log" 2>&1 || fail "exomonad new failed"
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

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

[reviewer]
agent_type = "codex"
EOF

log "starting mock Forgejo API at $MOCK_URL"
MOCK_LOG="$MOCK_LOG" REMOTE_DIR="$REMOTE_DIR" python3 "$E2E_DIR/mock_github.py" --port "$MOCK_PORT" >"$MOCK_SERVER_LOG" 2>&1 &
MOCK_PID=$!
wait_for "mock Forgejo API ready" "curl -fsS '$MOCK_URL/api/v1/repos/e2e/repo/pulls'" 20

# Push the PR's head branch so the reviewer can `git diff base..HEAD` locally,
# and create the author worktree fixture used by the review sandbox.
git checkout -q -b "$BRANCH"
echo 'reviewer sandbox fixture' > fixture.txt
git add fixture.txt
git commit -m "fixture change" -q
git push bare "$BRANCH" -q
mkdir -p ".exo/worktrees/$AUTHOR_AGENT"

create_pr

log "starting exomonad serve"
RUST_LOG="${RUST_LOG:-info}" EXOMONAD_LOG_FORMAT="" "$EXOMONAD_BIN" serve >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
wait_for "server socket ready" "test -S '$REPO_DIR/.exo/server.sock'" 30

REVIEWER_CONFIG="$REPO_DIR/.exo/worktrees/review-pr-$PR_NUMBER-codex/.codex/config.toml"
REVIEWER_SOCKET="$REPO_DIR/.exo/worktrees/review-pr-$PR_NUMBER-codex/.exo/server.sock"
wait_for "watcher auto-spawned a Codex reviewer worktree" "test -f '$REVIEWER_CONFIG'" 60
wait_for "reviewer worktree has .exo/server.sock symlinked (the socket the old curl rewrite claimed might be missing)" "test -L '$REVIEWER_SOCKET' && test -S '$REVIEWER_SOCKET'" 20

HARNESS="$SCRIPT_DIR/run.sh"
wait_for "generated Codex reviewer config is sandbox/instructions-consistent" "bash '$HARNESS' --assert-reviewer-config '$REVIEWER_CONFIG'" 10

log "PASS: Codex reviewer sandbox/instructions consistency E2E completed"
