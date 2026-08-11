#!/usr/bin/env bash
set -euo pipefail

# M5.6: run the programmatic TL against a real scratch repository.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SESSION="e2e-tl-loop-active"
CACHE_ROOT="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "$CACHE_ROOT/exomonad-tl-loop-active.XXXXXXXX")"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
ARTIFACTS="$WORK_DIR/artifacts.json"

cleanup() {
    local status=$?
    if tmux has-session -t "$SESSION" >/dev/null 2>&1; then
        echo "ERROR: active TL E2E left tmux session $SESSION running." >&2
        tmux kill-session -t "$SESSION" >/dev/null 2>&1 || true
        status=1
    fi
    rm -rf -- "$WORK_DIR"
    if [[ -e "$WORK_DIR" ]]; then
        echo "ERROR: active TL E2E scratch directory was not removed." >&2
        status=1
    fi
    return "$status"
}
trap cleanup EXIT

if tmux has-session -t "$SESSION" >/dev/null 2>&1; then
    echo "ERROR: refusing to reuse existing tmux session $SESSION." >&2
    exit 1
fi
command -v git >/dev/null || { echo "ERROR: git not found." >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not found." >&2; exit 1; }

printf '>>> [Phase 0] Creating scratch repository and bare remote...\n'
git init --bare "$REMOTE_DIR" -q
mkdir -p "$REPO_DIR/src" "$REPO_DIR/tests"
git -C "$REPO_DIR" init -q -b main
git -C "$REPO_DIR" remote add origin "$REMOTE_DIR"
git -C "$REPO_DIR" config user.name "ExoMonad active E2E"
git -C "$REPO_DIR" config user.email "active-e2e@example.com"
printf '# Active TL fixture\n' > "$REPO_DIR/README.md"
git -C "$REPO_DIR" add README.md
git -C "$REPO_DIR" commit -m "Create active TL fixture" -q
git -C "$REPO_DIR" push -u origin main -q

printf '>>> [Phase 1] Running bounded active controller without ExoMonad init or tmux...\n'
PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/active_run.py" \
    --repo "$REPO_DIR" \
    --remote "$REMOTE_DIR" \
    --artifacts "$ARTIFACTS"

printf '>>> [Phase 2] Exercising the default TL-window controller command without starting a session...\n'
mkdir -p "$REPO_DIR/.exo/tl-loop"
printf '%s\n' '{"run_id":"root","plan":{"leaves":[]}}' > "$REPO_DIR/.exo/tl-loop/plan.json"
PYTHONPATH="$PROJECT_ROOT" python3 -m tl_loop run \
    --project-root "$REPO_DIR" \
    --plan "$REPO_DIR/.exo/tl-loop/plan.json" \
    --run-id root
python3 - "$REPO_DIR/.exo/tl-loop/root/run.json" <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert state["fsm"]["phase"] == "tl_done"
print("PASS: default TL controller command reached tl_done without tmux")
PY

printf '>>> [Phase 3] Confirming captured assertions before cleanup...\n'
[[ -s "$ARTIFACTS" ]] || { echo "ERROR: active run artifact is missing." >&2; exit 1; }
python3 - "$ARTIFACTS" <<'PY'
import json
import sys
from pathlib import Path

artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert artifact["final_phase"] == "tl_done"
assert artifact["merged_prs"] == [1001, 1002]
assert artifact["ledger"]["sequence_status"] == "complete"
assert artifact["ledger"]["event_count"] == 7
assert artifact["ledger"]["last_consumed_offset"] == 7
assert artifact["ledger"]["charges_reconciled"] is True
assert artifact["ledger"]["reserved_tokens"] == {}
assert artifact["mutation_blocked"] == 0
assert artifact["tmux_session_started"] is False
assert artifact["manual_interventions"] == []
assert artifact["upward_pr"]["filed"] is True
print("PASS: active TL wave merged both slices, reconciled the ledger, and filed upward")
PY
