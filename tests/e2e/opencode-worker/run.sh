#!/usr/bin/env bash
set -euo pipefail

# E2E OpenCode Worker Test
# Validates fork_wave with agent_type="opencode": spawns OpenCode worker,
# model forwarding (worker_model → --model flag), ACP spawn lifecycle,
# and notify_parent delivery back to the Claude root TL.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

# --- Phase 0: Preconditions ---

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

if ! command -v opencode &>/dev/null; then
    echo "ERROR: opencode binary not found in PATH."
    exit 1
fi
echo "  opencode: $(command -v opencode)"

if [[ ! -d "$PROJECT_ROOT/.exo/wasm" ]] || ! ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm &>/dev/null; then
    echo "ERROR: No WASM plugins found in $PROJECT_ROOT/.exo/wasm/. Run 'just wasm-all'."
    exit 1
fi
echo "  WASM: $(ls "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm)"

for cmd in tmux git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found in PATH."
        exit 1
    fi
done
echo "  tmux, git: OK"

# --- Phase 1: Create temp environment ---

echo ">>> [Phase 1] Creating temp environment..."

mkdir -p "$HOME/.cache/exomonad-e2e"
WORK_DIR="$(mktemp -d "$HOME/.cache/exomonad-e2e/ocw.XXXXXXXX")"
echo "  Work dir: $WORK_DIR"

cleanup() {
    echo ""
    echo ">>> [Cleanup] Tearing down..."
    tmux kill-session -t e2e-opencode-worker 2>/dev/null || true
    echo "  Killed tmux session"
    rm -rf "$WORK_DIR"
    echo "  Removed $WORK_DIR"
    echo ">>> Done."
}
trap cleanup EXIT

# Create bare remote (OpenCode worker needs a pushable remote for its branch)
REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q

# Create working repo
REPO_DIR="$WORK_DIR/repo"
mkdir -p "$REPO_DIR"
cd "$REPO_DIR"
git init -q -b main
git remote add origin "$REMOTE_DIR"
git config user.name "Exomonad E2E"
git config user.email "e2e@example.com"
git commit --allow-empty -m "initial commit" -q
git push -u origin main -q

# Bootstrap via exomonad new
if ! "$EXOMONAD_BIN" new 2>&1 | sed 's/^/  /'; then
    echo "ERROR: 'exomonad new' failed during E2E setup."
    exit 1
fi

# Symlink WASM from project
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done

trust_claude_project() {
    local project_path="$1"
    python3 - "$project_path" <<'PY'
import json
import sys
from pathlib import Path

project = str(Path(sys.argv[1]).resolve())
claude_json = Path.home() / ".claude.json"

if claude_json.exists():
    try:
        data = json.loads(claude_json.read_text())
    except json.JSONDecodeError:
        data = {}
else:
    data = {}

projects = data.setdefault("projects", {})
entry = projects.setdefault(project, {})
entry.setdefault("allowedTools", [])
entry.setdefault("disabledMcpjsonServers", [])
entry.setdefault("enabledMcpjsonServers", [])
entry.setdefault("exampleFiles", [])
entry["hasClaudeMdExternalIncludesApproved"] = False
entry["hasClaudeMdExternalIncludesWarningShown"] = False
entry["hasTrustDialogAccepted"] = True
entry.setdefault("mcpContextUris", [])
entry.setdefault("mcpServers", {})
entry.setdefault("projectOnboardingSeenCount", 0)
entry["hasCompletedProjectOnboarding"] = True

claude_json.parent.mkdir(parents=True, exist_ok=True)
tmp_path = claude_json.with_suffix(claude_json.suffix + ".tmp")
tmp_path.write_text(json.dumps(data, indent=2) + "\n")
tmp_path.replace(claude_json)
PY
}

# Claude Code prompts for workspace trust per project path, including companion worktrees.
trust_claude_project "$REPO_DIR"
trust_claude_project "$REPO_DIR/.exo/companions/test-runner"

record_team_baseline() {
    local baseline_file="$REPO_DIR/.exo/e2e-team-baseline.txt"
    if [[ -d "$HOME/.claude/teams" ]]; then
        find "$HOME/.claude/teams" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort > "$baseline_file"
    else
        : > "$baseline_file"
    fi
}

# Scope testrunner team assertions to teams created by this test invocation.
record_team_baseline

# Write config: Claude haiku root TL + OpenCode workers + testrunner companion
cat > .exo/config.toml <<'EOF'
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "e2e-opencode-worker"
root_agent_type = "claude"
spawn_agent_type = "opencode"
model = "sonnet"
yolo = true
initial_prompt = """You are in automated E2E OpenCode worker test mode. Do exactly these steps on your first turn and nothing else:
1. Create a team via TeamCreate.
2. Call fork_wave with exactly one agent named oc-worker, agent_type='opencode', fork_session=false, and this task: You are an E2E test subject. Write oc-worker-output.txt containing the single line OpenCode worker test passed. Stage and commit it with git add oc-worker-output.txt && git commit -m 'e2e: add oc-worker-output.txt'. Push to your branch. Call notify_parent with status='success' and message='[OC-WORKER-DONE] OpenCode worker test complete. File written and committed.' Then stop.
3. Stop and idle. Do not edit files yourself, do not merge PRs, and wait for [OC-WORKER-DONE]."""

[opencode]
worker_model = "opencode-go/deepseek-v4-flash"
use_embedded_key = true

[[companions]]
name = "test-runner"
agent_type = "claude"
role = "testrunner"
model = "haiku"
command = "claude --dangerously-skip-permissions"
task = "Execute the test plan from your role context. Start immediately."
EOF

# Copy testrunner context into role
mkdir -p .exo/roles/devswarm/context
cp "$SCRIPT_DIR/testrunner.md" .exo/roles/devswarm/context/testrunner.md

# Root TL rules: create team, fork_wave one OpenCode worker, idle
mkdir -p .claude/rules
cp "$SCRIPT_DIR/e2e-test.md" .claude/rules/e2e-test.md

echo "  Repo: $REPO_DIR"
echo "  Remote: $REMOTE_DIR"

# --- Phase 2: Set environment ---

echo ">>> [Phase 2] Configuring environment..."
export FORGEJO_TOKEN="test-token-e2e"
echo "  FORGEJO_TOKEN=test-token-e2e"

# --- Phase 3: Run exomonad init ---

echo ">>> [Phase 3] Launching exomonad init..."

echo ""
echo "============================================"
echo "  E2E OpenCode Worker Test Ready"
echo "  Session: e2e-opencode-worker"
echo "  Work dir: $WORK_DIR/repo"
echo ""
echo "  Chain under test:"
echo "    Claude haiku root TL creates team"
echo "    → fork_wave agent_type=opencode"
echo "    → OpenCode spawns with --model haiku"
echo "    → worker writes oc-worker-output.txt"
echo "    → notify_parent → Teams inbox → root TL"
echo "  Testrunner validates independently."
echo "============================================"
echo ""

"$EXOMONAD_BIN" init --session e2e-opencode-worker
