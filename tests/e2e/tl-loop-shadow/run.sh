#!/usr/bin/env bash
set -euo pipefail

# M3.3: capture a real interactive TL trajectory beside the read-only shadow.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"
# shellcheck source=../lib/git-fixture.sh
source "$PROJECT_ROOT/tests/e2e/lib/git-fixture.sh"

TIMEOUT_SECONDS="${TL_LOOP_SHADOW_E2E_TIMEOUT_SECONDS:-480}"
SESSION="e2e-tl-loop-shadow"

pick_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

trust_claude_project() {
    local project_path="$1"
    python3 - "$project_path" <<'PY'
import json
import sys
from pathlib import Path

project = str(Path(sys.argv[1]).resolve())
claude_json = Path.home() / ".claude.json"
try:
    data = json.loads(claude_json.read_text(encoding="utf-8")) if claude_json.exists() else {}
except json.JSONDecodeError:
    data = {}
entry = data.setdefault("projects", {}).setdefault(project, {})
entry["hasTrustDialogAccepted"] = True
entry["hasCompletedProjectOnboarding"] = True
entry["hasClaudeMdExternalIncludesApproved"] = False
entry["hasClaudeMdExternalIncludesWarningShown"] = False
claude_json.parent.mkdir(parents=True, exist_ok=True)
temporary = claude_json.with_suffix(claude_json.suffix + ".tmp")
temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
temporary.replace(claude_json)
PY
}

echo ">>> [Phase 0] Checking preconditions..."
if [[ -x "$PROJECT_ROOT/target/debug/exomonad" ]]; then
    EXOMONAD_BIN="$PROJECT_ROOT/target/debug/exomonad"
else
    EXOMONAD_BIN="$(command -v exomonad 2>/dev/null || true)"
fi
[[ -n "$EXOMONAD_BIN" ]] || { echo "ERROR: exomonad binary not found."; exit 1; }
for command_name in chainlink claude curl git python3 tmux; do
    command -v "$command_name" >/dev/null || {
        echo "ERROR: $command_name not found in PATH."
        exit 1
    }
done
[[ -d "$PROJECT_ROOT/.exo/wasm" ]] || {
    echo "ERROR: missing $PROJECT_ROOT/.exo/wasm; run 'just wasm-all'."
    exit 1
}
ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm >/dev/null 2>&1 || {
    echo "ERROR: no WASM guests found in $PROJECT_ROOT/.exo/wasm."
    exit 1
}

# A previous interactive run may have been interrupted while attached. The
# session name is owned by this harness, so remove only that exact stale run.
tmux kill-session -t "$SESSION" 2>/dev/null || true

echo ">>> [Phase 1] Creating scratch repository..."
mkdir -p "${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}"
WORK_DIR="$(mktemp -d "${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}/tl-loop-shadow.XXXXXXXX")"
e2e_git_use_fixture_root "$WORK_DIR"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
ARTIFACT_DIR="$WORK_DIR/artifacts"
MOCK_LOG="$WORK_DIR/mock-forgejo.log"
MOCK_SERVER_LOG="$WORK_DIR/mock-forgejo-server.log"
MOCK_PORT="$(pick_port)"
SERVER_PORT="$(pick_port)"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
MOCK_PID=""

cleanup() {
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [[ -n "$MOCK_PID" ]]; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR" "$ARTIFACT_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
printf '# TL shadow E2E fixture\n' > README.md
git add README.md
git commit -m "initial commit" -q
git push -u origin main -q

"$EXOMONAD_BIN" new >"$WORK_DIR/new.log" 2>&1
chainlink init >"$WORK_DIR/chainlink-init.log" 2>&1
export CHAINLINK_DB="$REPO_DIR/.chainlink/issues.db"
E2E_ISSUE_ID="$(chainlink quick --quiet -p low -l test "Run live TL shadow trajectory")"
chainlink session start >"$WORK_DIR/chainlink-session.log" 2>&1
chainlink session work "$E2E_ISSUE_ID" >>"$WORK_DIR/chainlink-session.log" 2>&1
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
fi

ROOT_PROMPT="$(< "$SCRIPT_DIR/e2e-test.md")"
cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "claude"
spawn_agent_type = "claude"
model = "sonnet"
yolo = true
port = $SERVER_PORT
poll_interval = 1
forgejo_url = "$MOCK_URL"
forgejo_token = "author-token"
forgejo_reviewer_token = "reviewer-token"
initial_prompt = """
$ROOT_PROMPT
"""

[[companions]]
name = "tl-loop-shadow"
agent_type = "process"
command = "PYTHONPATH='$PROJECT_ROOT' python3 '$SCRIPT_DIR/shadow_companion.py' '$REPO_DIR' '$SESSION' '$ARTIFACT_DIR' --timeout $TIMEOUT_SECONDS"
EOF

mkdir -p .claude/rules
cp "$SCRIPT_DIR/e2e-test.md" .claude/rules/e2e-test.md
cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md
trust_claude_project "$REPO_DIR"
trust_claude_project "$REPO_DIR/.exo/worktrees/shadow-slice-a-claude"
trust_claude_project "$REPO_DIR/.exo/worktrees/shadow-slice-b-claude"
trust_claude_project "$REPO_DIR/.exo/worktrees/review-pr-1-claude"
trust_claude_project "$REPO_DIR/.exo/worktrees/review-pr-2-claude"

echo ">>> [Phase 2] Starting mock Forgejo..."
MOCK_LOG="$MOCK_LOG" REMOTE_DIR="$REMOTE_DIR" python3 "$E2E_DIR/mock_github.py" \
    --port "$MOCK_PORT" >"$MOCK_SERVER_LOG" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 40); do
    if curl -fsS "$MOCK_URL/api/v1/repos/e2e/repo/pulls" >/dev/null; then
        break
    fi
    sleep 0.25
done
curl -fsS "$MOCK_URL/api/v1/repos/e2e/repo/pulls" >/dev/null || {
    echo "ERROR: mock Forgejo did not become ready."
    exit 1
}

export FORGEJO_TOKEN="author-token"
export FORGEJO_API_URL="$MOCK_URL"
export TL_LOOP_SHADOW_E2E_TIMEOUT_SECONDS="$TIMEOUT_SECONDS"
# The scratch repo intentionally contains exomonad scaffolding that is
# untracked before the live TL starts; acknowledge only this E2E preflight.
export EXOMONAD_TL_PREFLIGHT_ACK=1

echo ">>> [Phase 3] Launching live TL and shadow companion..."
echo "  scratch repo: $REPO_DIR"
echo "  artifacts: $ARTIFACT_DIR"
"$EXOMONAD_BIN" init --session "$SESSION"

echo ">>> [Phase 4] Validating captured trajectory..."
[[ -s "$ARTIFACT_DIR/intended.jsonl" ]] || {
    echo "ERROR: shadow intended trajectory is empty or missing."
    exit 1
}
[[ -s "$ARTIFACT_DIR/actual.jsonl" ]] || {
    echo "ERROR: actual trajectory is empty or missing."
    exit 1
}
REPORT_PATH="$(< "$ARTIFACT_DIR/report.path")"
[[ -s "$REPORT_PATH" ]] || {
    echo "ERROR: shadow diff report is missing."
    exit 1
}
python3 - "$ARTIFACT_DIR/metadata.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    metadata = json.load(stream)
if metadata.get("shadow_mutation_calls") != 0:
    raise SystemExit("shadow companion recorded a mutation-attributable call")
PY
echo "PASS: live TL and read-only shadow trajectories captured"
