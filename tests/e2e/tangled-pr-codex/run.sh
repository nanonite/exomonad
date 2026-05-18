#!/usr/bin/env bash
set -euo pipefail

# E2E Tangled PR Codex Test
# Validates Codex root/TL/worker/dev/reviewer flow against a local Tangled
# remote and real spindle CI status ingestion.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

echo ">>> [Phase 0] Checking preconditions..."

EXOMONAD_BIN=""
if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
    export PATH="$PROJECT_ROOT/target/debug:$PATH"
elif command -v exomonad &>/dev/null; then
    EXOMONAD_BIN="$(command -v exomonad)"
else
    echo "ERROR: exomonad binary not found. Run 'just install-all-dev' or 'cargo build -p exomonad'."
    exit 1
fi
echo "  exomonad: $EXOMONAD_BIN"

for cmd in codex tmux git python3 docker sqlite3 curl awk; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  codex: $(command -v codex)"
echo "  tmux, git, python3, docker, sqlite3, curl: OK"

SPINDLE="$PROJECT_ROOT/tangled-core/cmd/spindle/spindle"
if [[ ! -x "$SPINDLE" ]]; then
    echo "ERROR: spindle binary not found at $SPINDLE"
    echo "Build it: cd tangled-core && go build -o cmd/spindle/spindle ./cmd/spindle"
    exit 1
fi
echo "  spindle: $SPINDLE"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

KNOT_CONTAINER="tangled-knot-knot-1"
KNOT_DB="$PROJECT_ROOT/tangled-knot/server/knotserver.db"
OWNER_DID="did:plc:localdev"

if ! docker ps --filter name="$KNOT_CONTAINER" --filter status=running --format '{{.Names}}' | grep -q "$KNOT_CONTAINER"; then
    echo "ERROR: knot container '$KNOT_CONTAINER' is not running"
    echo "Start it: docker compose up -d  (in tests/e2e/tangled-ci/)"
    exit 1
fi
echo "  knot container: running"

if ! sqlite3 "$KNOT_DB" "SELECT 1 FROM events LIMIT 1;" >/dev/null 2>&1; then
    echo "ERROR: knot DB not accessible at $KNOT_DB"
    exit 1
fi
echo "  knot DB: accessible"

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$PROJECT_ROOT/.e2e-work"
WORK_DIR="$(mktemp -d "$PROJECT_ROOT/.e2e-work/exomonad-e2e-tangled-pr-codex.XXXXXXXX")"
SESSION="e2e-tangled-pr-codex"
RESULT_FILE="$WORK_DIR/validation-result.txt"
REPO_NAME="tangled-pr-codex-$(date +%s)-$$"
REPO_DID="did:web:localhost%3A5555:repo:$REPO_NAME"
REMOTE_DIR="$WORK_DIR/$REPO_NAME.git"
REPO_DIR="$WORK_DIR/repo"
CODEX_HOME_DIR="$WORK_DIR/codex-home"
SPINDLE_DB="$WORK_DIR/spindle.db"
SPINDLE_LOG="$WORK_DIR/spindle.log"
EVENT_FILE="$WORK_DIR/knot-event.json"
RELAY_LOG="$WORK_DIR/knot-event-relay.log"
RELAY_PORT="${TANGLED_PR_CODEX_RELAY_PORT:-6566}"
RELAY_PID=""

echo "  Work dir: $WORK_DIR"
echo "  Repo name: $REPO_NAME"
echo "  Repo DID:  $REPO_DID"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    [[ -n "$RELAY_PID" ]] && kill "$RELAY_PID" 2>/dev/null || true
    pkill -f 'tangled-core/cmd/spindle/spindle' 2>/dev/null || true
    echo "  Killed tmux session and local relay/spindle processes"
    if [[ -f "$RESULT_FILE" ]]; then
        echo "  Validator result:"
        sed 's/^/    /' "$RESULT_FILE"
    fi
    if [[ -f "$SPINDLE_LOG" ]]; then
        echo "  Spindle log tail:"
        tail -n 80 "$SPINDLE_LOG" | sed 's/^/    /'
    fi
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -f 'tangled-core/cmd/spindle/spindle' 2>/dev/null || true
rm -rf /tmp/spindle-logs

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR" "$CODEX_HOME_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"

mkdir -p .tangled/workflows src
cat > README.md <<'EOF'
# Tangled PR Codex E2E Fixture

This repository is created by tests/e2e/tangled-pr-codex/run.sh.
EOF
cat > src/hello.py <<'EOF'
def add(a, b):
    return a + b
EOF
cat > src/test_hello.py <<'EOF'
from hello import add

assert add(2, 3) == 5
print("all tests passed")
EOF
cat > .tangled/workflows/ci.yml <<'EOF'
engine: nixery
when:
  - event: [push, manual]
    branch: ["*"]
clone:
  depth: 1
  submodules: false
dependencies:
  nixpkgs:
    - python3
steps:
  - name: "Run tests"
    command: "python3 src/test_hello.py"
EOF

git add README.md src/hello.py src/test_hello.py .tangled/workflows/ci.yml
git commit -m "initial commit" -q
git push -u origin main -q

echo ">>> [Phase 2] Preparing local Tangled repo..."
docker exec "$KNOT_CONTAINER" sh -c \
  "set -eu
   repo_path='/home/git/repositories/$REPO_DID'
   mkdir -p \"\$repo_path\"
   git init --bare --initial-branch=main \"\$repo_path\" >/dev/null 2>&1 || git init --bare \"\$repo_path\" >/dev/null 2>&1 || true
   git --git-dir \"\$repo_path\" symbolic-ref HEAD refs/heads/main >/dev/null 2>&1 || true
   mkdir -p \"\$repo_path/hooks/post-receive.d\"
   cat > \"\$repo_path/hooks/post-receive.d/40-notify.sh\" <<'HOOK'
#!/usr/bin/env bash
# AUTO GENERATED BY EXOMONAD TANGLED E2E
push_options=()
for ((i=0; i<GIT_PUSH_OPTION_COUNT; i++)); do
    option_var=\"GIT_PUSH_OPTION_\$i\"
    push_options+=(-push-option \"\${!option_var}\")
done
/usr/bin/knot hook -git-dir \"\$GIT_DIR\" -user-did \"\$GIT_USER_DID\" -user-handle \"\$GIT_USER_HANDLE\" -internal-api \"localhost:5444\" \"\${push_options[@]}\" post-receive
HOOK
   cat > \"\$repo_path/hooks/post-receive\" <<'HOOK'
#!/usr/bin/env bash
# AUTO GENERATED BY EXOMONAD TANGLED E2E
data=\$(cat)
exitcodes=\"\"
hookname=\$(basename \"\$0\")
GIT_DIR=\"\$PWD\"
for hook in \"\${GIT_DIR}/hooks/\${hookname}.d/\"*; do
  test -x \"\${hook}\" && test -f \"\${hook}\" || continue
  echo \"\${data}\" | \"\${hook}\"
  exitcodes=\"\${exitcodes} \$?\"
done
for i in \$exitcodes; do
  [ \"\$i\" -eq 0 ] || exit \"\$i\"
done
HOOK
   chmod 755 \"\$repo_path/hooks/post-receive\" \"\$repo_path/hooks/post-receive.d/40-notify.sh\"
   chown -R git:git \"\$repo_path\""

sqlite3 "$KNOT_DB" "
  INSERT OR IGNORE INTO repo_keys (repo_did, signing_key, owner_did, repo_name, at_uri, key_type)
    VALUES ('$REPO_DID', NULL, '$OWNER_DID', '$REPO_NAME', 'at://$OWNER_DID/sh.tangled.repo/$REPO_NAME', 'web');
  INSERT OR IGNORE INTO acl (p_type, v0, v1, v2, v3) VALUES
    ('p', '$OWNER_DID', 'thisserver', '$REPO_DID', 'repo:settings'),
    ('p', '$OWNER_DID', 'thisserver', '$REPO_DID', 'repo:push'),
    ('p', '$OWNER_DID', 'thisserver', '$REPO_DID', 'repo:owner'),
    ('p', '$OWNER_DID', 'thisserver', '$REPO_DID', 'repo:invite'),
    ('p', '$OWNER_DID', 'thisserver', '$REPO_DID', 'repo:delete'),
    ('p', 'server:owner', 'thisserver', '$REPO_DID', 'repo:delete');
"
echo "  seeded knot repo_keys/ACL: $KNOT_DB"

git remote add tangled "git@local-tangled:repositories/$REPO_DID"
GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git push tangled main --force -q
echo "  pushed main to tangled remote"

sqlite3 "$SPINDLE_DB" "
  CREATE TABLE IF NOT EXISTS repos (
    id integer primary key autoincrement,
    knot text not null,
    owner text not null,
    name text not null,
    addedAt text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    unique(owner, name)
  );
  INSERT OR IGNORE INTO repos (knot, owner, name) VALUES ('localhost:5555', '$OWNER_DID', '$REPO_NAME');
"
echo "  seeded spindle DB: $SPINDLE_DB"

if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

ROOT_PROMPT="$(python3 - "$SCRIPT_DIR/e2e-test.md" <<'PY'
import pathlib
import sys

value = pathlib.Path(sys.argv[1]).read_text()
print(value.replace('"""', '\\"\\"\\"'))
PY
)"

cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "codex"
spawn_agent_type = "codex"
yolo = true
poll_interval = 5
tangled_knot_url = "ws://127.0.0.1:$RELAY_PORT/events"
tangled_spindle_url = "ws://localhost:6555/events"
tangled_owner_did = "$OWNER_DID"
tangled_knot_container = "$KNOT_CONTAINER"
tangled_spindle_db = "$SPINDLE_DB"
initial_prompt = """
$ROOT_PROMPT
"""

[reviewer]
agent_type = "codex"

[[companions]]
name = "spindle"
agent_type = "process"
command = "$SCRIPT_DIR/deferred-spindle.sh"

[[companions]]
name = "tangled-pr-codex-validator"
agent_type = "process"
command = "$SCRIPT_DIR/validate.sh '$REPO_DIR' '$SESSION' '$RESULT_FILE' '$KNOT_DB' '$SPINDLE_DB' '$EVENT_FILE' '$SPINDLE_LOG' '$REPO_NAME' '$PROJECT_ROOT'"
EOF

if [[ -f "$HOME/.codex/auth.json" ]]; then
    cp -p "$HOME/.codex/auth.json" "$CODEX_HOME_DIR/auth.json"
fi
if [[ -f "$HOME/.codex/installation_id" ]]; then
    cp -p "$HOME/.codex/installation_id" "$CODEX_HOME_DIR/installation_id"
fi

cat > "$CODEX_HOME_DIR/config.toml" <<EOF
[projects."$REPO_DIR"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/tangled-pr-codex-tl-codex"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/tangled-pr-codex-dev-codex"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/agents/tangled-pr-codex-worker-codex"]
trust_level = "trusted"

[projects."$REPO_DIR/.exo/worktrees/review-pr-1-codex"]
trust_level = "trusted"
EOF

echo ">>> [Phase 3] Starting local knot event relay..."
python3 "$SCRIPT_DIR/knot-event-relay.py" "$RELAY_PORT" "$EVENT_FILE" > "$RELAY_LOG" 2>&1 &
RELAY_PID=$!
sleep 1
if ! kill -0 "$RELAY_PID" 2>/dev/null; then
    echo "ERROR: knot event relay failed to start"
    cat "$RELAY_LOG"
    exit 1
fi
echo "  relay pid: $RELAY_PID log: $RELAY_LOG"

echo ">>> [Phase 4] Configuring environment..."
unset GITHUB_TOKEN
unset GITHUB_API_URL
export CODEX_HOME="$CODEX_HOME_DIR"
export EXOMONAD_LOG_FORMAT=""
echo "  GitHub auth unset; file_pr should use local .exo/prs.json flow"
echo "  Codex config isolated to $CODEX_HOME"

echo ">>> [Phase 5] Launching exomonad init..."
echo ""
echo "============================================"
echo "  E2E Tangled PR Codex Test Ready"
echo "  Session: $SESSION"
echo "  Work dir: $REPO_DIR"
echo "  Tangled repo: $REPO_NAME"
echo ""
echo "  Chain under test:"
echo "    Codex root -> Codex TL"
echo "    Codex TL -> Codex worker tmux notify_parent"
echo "    Codex TL -> Codex dev leaf -> local PR"
echo "    file_pr pushes dev branch to local Tangled remote"
echo "    Tangled knot event -> spindle CI -> ExoMonad CI ingestion"
echo "    Codex reviewer approval + merge-ready parent messaging"
echo "============================================"
echo ""

set +e
"$EXOMONAD_BIN" init --verbose --session "$SESSION" --reviewer codex
INIT_STATUS=$?
set -e

if [[ -f "$RESULT_FILE" ]]; then
    if grep -Fxq "Failures: 0" "$RESULT_FILE"; then
        exit 0
    fi
    exit 1
fi

exit "$INIT_STATUS"
