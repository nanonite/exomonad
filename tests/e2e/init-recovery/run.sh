#!/usr/bin/env bash
set -euo pipefail

# E2E init-recovery test (chainlink #907).
#
# Drives the real `exomonad init` binary against a real tmux session and
# asserts the crash-recovery properties fixed in #903: a dead Server,
# Watcher, or TL window is replaced by exactly one healthy window instead
# of accumulating duplicates, and concurrent `exomonad init` invocations
# converge on the same single-window-per-role outcome instead of racing.
#
# Phases 1-6 do not exercise TL business logic — the seeded plan has no
# work, so the TL controller exits immediately after each `exomonad init`
# call there. That failure is expected and tolerated: those phases assert
# tmux window state and server socket health, not controller outcomes.
#
# Phase 7 exercises what phases 1-6 deliberately don't: automatic
# continuation of a real nonterminal checkpoint through the embedded
# controller, the real watcher poller, and the real ledger, ending in an
# observable next lifecycle action (recovered review/CI evidence durably
# written back to the checkpoint) -- not just tmux window repair. TL
# controller crash/resume *decision logic* is covered separately and
# thoroughly by tests/e2e/ordered-recursive at the Python-controller level;
# this phase covers what that harness cannot: the real embedded `tl_loop.pyz`
# archive, launched by the real binary, resuming a checkpoint that was
# already on disk when `exomonad init` started.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

SESSION="e2e-init-recovery"
CONTINUATION_SESSION="e2e-init-recovery-continuation"
MOCK_PID=""

e2e_preflight tmux git curl python3

cleanup() {
    local code=$?
    # Cleanup must always reach e2e_cleanup, including on a failing run.
    # Under `set -e`, `(exit "$code")` below would itself trigger errexit
    # for any nonzero code and abort this trap before e2e_cleanup runs,
    # leaking the tmux session and scratch work dir. Disable errexit for
    # the remainder of the trap so every step below always executes.
    set +e
    tmux kill-session -t "$SESSION" 2>/dev/null
    tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null
        wait "$MOCK_PID" 2>/dev/null
    fi
    trap - EXIT
    (exit "$code")
    e2e_cleanup
}
trap cleanup EXIT INT TERM

e2e_create_work_dir "init-recovery"

e2e_phase "Phase 1" "Creating scratch repository..."
REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q
e2e_init_repo "Exomonad init-recovery E2E" "init-recovery-e2e@example.invalid"
git remote add origin "$REMOTE_DIR"
git push -u origin main -q
e2e_run_exomonad_new
e2e_install_project_wasm_and_roles
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
mkdir -p .exo/tl-loop
printf '{"run_id":"root","plan":{"leaves":[]}}\n' > .exo/tl-loop/plan.json
e2e_write_basic_config "$SESSION"
export FORGEJO_TOKEN="test-token-e2e"
tmux kill-session -t "$SESSION" 2>/dev/null || true

window_line() {
    # Prints "<window_id> <pane_pid> <pane_dead>" for the first window
    # matching the given name, or nothing if no such window exists.
    tmux list-windows -t "$SESSION" -F '#{window_name} #{window_id} #{pane_pid} #{pane_dead}' 2>/dev/null \
        | awk -v name="$1" '$1 == name { print $2, $3, $4; exit }'
}

window_count() {
    tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -Fxc "$1" || true
}

run_init() {
    # exomonad init is expected to fail here: the seeded plan has no work,
    # so the TL controller exits during startup and the whole invocation
    # returns nonzero even though Server/Watcher recovery already
    # succeeded. That failure is intentional and tolerated everywhere this
    # helper is used — only tmux window state is asserted.
    "$EXOMONAD_BIN" init --session "$SESSION" > "$WORK_DIR/init-$1.log" 2>&1 || true
}

e2e_phase "Phase 2" "Running exomonad init to create Server, Watcher, and TL windows..."
run_init "first"
for name in Server Watcher TL; do
    [[ "$(window_count "$name")" == "1" ]] || {
        cat "$WORK_DIR/init-first.log"
        e2e_fail "expected exactly one $name window after first init, got $(window_count "$name")"
    }
done
e2e_log "PASS: first init created exactly one Server, Watcher, and TL window"

e2e_phase "Phase 3" "Killing the Watcher process and verifying dead-window replacement..."
read -r watcher_id watcher_pid _ < <(window_line Watcher)
kill -9 "$watcher_pid"
for _ in $(seq 1 50); do
    read -r _ _ dead < <(window_line Watcher)
    [[ "$dead" == "1" ]] && break
    sleep 0.1
done
read -r _ _ dead < <(window_line Watcher)
[[ "$dead" == "1" ]] || e2e_fail "Watcher window did not persist dead after its process was killed (remain-on-exit regression)"
e2e_log "Watcher window $watcher_id confirmed dead and still present"

run_init "watcher-recovery"
[[ "$(window_count Watcher)" == "1" ]] || {
    cat "$WORK_DIR/init-watcher-recovery.log"
    e2e_fail "expected exactly one Watcher window after recovery, got $(window_count Watcher) (duplicate-window regression)"
}
read -r new_watcher_id _ new_dead < <(window_line Watcher)
[[ "$new_watcher_id" != "$watcher_id" ]] || e2e_fail "Watcher window was not replaced (still $watcher_id)"
[[ "$new_dead" == "0" ]] || e2e_fail "recovered Watcher window is not alive"
e2e_log "PASS: dead Watcher window $watcher_id was replaced by live window $new_watcher_id, no duplicate"

e2e_phase "Phase 4" "Killing the Server process and verifying single-window recovery..."
read -r server_id server_pid _ < <(window_line Server)
kill -9 "$server_pid"
sleep 1
run_init "server-recovery"
[[ "$(window_count Server)" == "1" ]] || {
    cat "$WORK_DIR/init-server-recovery.log"
    e2e_fail "expected exactly one Server window after recovery, got $(window_count Server) (duplicate-window regression)"
}
for _ in $(seq 1 50); do
    [[ -S "$REPO_DIR/.exo/server.sock" ]] && curl -fsS --unix-socket "$REPO_DIR/.exo/server.sock" http://localhost/health >/dev/null 2>&1 && break
    sleep 0.2
done
curl -fsS --unix-socket "$REPO_DIR/.exo/server.sock" http://localhost/health >/dev/null \
    || e2e_fail "recovered Server socket is not healthy"
e2e_log "PASS: Server recovered to exactly one window with a healthy socket"

e2e_phase "Phase 5" "Running two concurrent init invocations and verifying no duplicate windows..."
read -r pre_watcher_id pre_watcher_pid _ < <(window_line Watcher)
kill -9 "$pre_watcher_pid"
read -r pre_server_id pre_server_pid _ < <(window_line Server)
kill -9 "$pre_server_pid"
sleep 1
( run_init "race-a" ) &
race_a=$!
( run_init "race-b" ) &
race_b=$!
wait "$race_a"
wait "$race_b"
for name in Server Watcher TL; do
    [[ "$(window_count "$name")" == "1" ]] || {
        cat "$WORK_DIR/init-race-a.log" "$WORK_DIR/init-race-b.log"
        e2e_fail "expected exactly one $name window after concurrent init, got $(window_count "$name") (concurrent-init lock regression)"
    }
done
e2e_log "PASS: two concurrent exomonad init invocations converged on exactly one window per role"

e2e_phase "Phase 6" "Confirming TL dead-window replacement never accumulated duplicates..."
[[ "$(window_count TL)" == "1" ]] || e2e_fail "TL window duplicated across repeated crash-and-reinit cycles: $(window_count TL)"
e2e_log "PASS: exactly one TL window survived five exomonad init invocations across three crash scenarios"

tmux kill-session -t "$SESSION" 2>/dev/null || true

e2e_phase "Phase 7" "Seeding a real nonterminal checkpoint and confirming automatic continuation..."

CONT_DIR="$WORK_DIR/continuation"
CONT_REPO="$CONT_DIR/repo"
CONT_REMOTE="$CONT_DIR/remote/owner/repo.git"
mkdir -p "$CONT_DIR"

# Bare remote lives at .../owner/repo.git: exomonad's owner/repo parser reads
# the last two path segments of the git remote URL, independent of what
# forgejo_url points at. origin uses file:// so `git fetch` genuinely works;
# forgejo_url (below) is queried over real HTTP for PR/review/CI state.
mkdir -p "$(dirname "$CONT_REMOTE")"
git init --bare -q "$CONT_REMOTE"
mkdir -p "$CONT_REPO"
git -C "$CONT_REPO" init -q -b main
git -C "$CONT_REPO" remote add origin "file://$CONT_REMOTE"
git -C "$CONT_REPO" config user.name "Exomonad init-recovery E2E"
git -C "$CONT_REPO" config user.email "init-recovery-e2e@example.invalid"
git -C "$CONT_REPO" commit --allow-empty -q -m init
git -C "$CONT_REPO" push -u origin main -q
git -C "$CONT_REPO" checkout -q -b main.leaf-a
echo "leaf change" > "$CONT_REPO/leaf.txt"
git -C "$CONT_REPO" add leaf.txt
git -C "$CONT_REPO" commit -q -m "leaf-a change"
git -C "$CONT_REPO" push -q origin main.leaf-a
git -C "$CONT_REPO" checkout -q main
LEAF_SHA="$(git -C "$CONT_REPO" rev-parse main.leaf-a)"

MOCK_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
REMOTE_DIR="$CONT_REMOTE" MOCK_LOG="$CONT_DIR/mock.log" \
    python3 "$PROJECT_ROOT/tests/e2e/mock_github.py" --port "$MOCK_PORT" \
    > "$CONT_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null \
    || { cat "$CONT_DIR/mock.stderr"; e2e_fail "mock Forgejo API did not become ready"; }

cd "$CONT_REPO"
"$EXOMONAD_BIN" new > "$CONT_DIR/new.log" 2>&1 || { cat "$CONT_DIR/new.log"; e2e_fail "'exomonad new' failed for the continuation fixture"; }
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
mkdir -p .exo/tl-loop
printf '{"run_id":"root","plan":{"leaves":[{"name":"leaf-a","task":"leaf-a change"}]}}\n' > .exo/tl-loop/plan.json
cat > .exo/config.toml <<EOF
shell_command = "bash"
tmux_session = "$CONTINUATION_SESSION"
yolo = true
poll_interval = 1
spawn_agent_type = "codex"
forgejo_url = "$MOCK_URL"
forgejo_token = "test-token-e2e"
EOF

read -r PR_NUMBER HEAD_SHA < <(
    python3 "$SCRIPT_DIR/seed_publication.py" \
        --mock-url "$MOCK_URL" --repo "$CONT_REPO" \
        --head-branch "main.leaf-a" --head-sha "$LEAF_SHA" --slice-id "leaf-a"
)
[[ "$HEAD_SHA" == "$LEAF_SHA" ]] || e2e_fail "seed_publication.py returned an unexpected head SHA"
e2e_log "Filed and approved PR #$PR_NUMBER against the mock Forgejo (head=$HEAD_SHA)"

PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/seed_checkpoint.py" \
    --repo "$CONT_REPO" --pr-number "$PR_NUMBER" --branch "main.leaf-a" --slice-id "leaf-a"

BEFORE_STATUS="$(python3 -c "
import json
d = json.load(open('.exo/tl-loop/root/run.json'))
s = d['slices']['leaf-a']
print(s['reviewed_head'], s['verdict'], d['events']['last_consumed_offset'])
")"
read -r before_reviewed_head before_verdict before_offset <<< "$BEFORE_STATUS"
[[ "$before_reviewed_head" == "None" && "$before_verdict" == "None" && "$before_offset" == "0" ]] \
    || e2e_fail "seeded checkpoint was not in the expected pre-recovery state: $BEFORE_STATUS"
e2e_log "Seeded nonterminal checkpoint: pr_number=$PR_NUMBER reviewed_head=None verdict=None (awaiting recovery)"

tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null || true
"$EXOMONAD_BIN" init --session "$CONTINUATION_SESSION" > "$CONT_DIR/init.log" 2>&1 || true

AFTER_STATUS=""
for _ in $(seq 1 50); do
    AFTER_STATUS="$(python3 -c "
import json
d = json.load(open('.exo/tl-loop/root/run.json'))
s = d['slices']['leaf-a']
print(s['reviewed_head'], s['verdict'], d['events']['last_consumed_offset'])
" 2>/dev/null)" || AFTER_STATUS=""
    [[ -n "$AFTER_STATUS" ]] && [[ "$AFTER_STATUS" != "None None 0" ]] && break
    sleep 0.2
done
read -r after_reviewed_head after_verdict after_offset <<< "$AFTER_STATUS"

if [[ "$after_reviewed_head" != "$HEAD_SHA" || "$after_verdict" != "GO" ]]; then
    cat "$CONT_DIR/init.log"
    e2e_fail "embedded controller did not recover review evidence after restart: got '$AFTER_STATUS', expected reviewed_head=$HEAD_SHA verdict=GO"
fi
[[ "$after_offset" -gt 0 ]] || e2e_fail "embedded controller did not consume any ledger events during recovery"
e2e_log "PASS: embedded controller resumed the nonterminal checkpoint, the real watcher observed the approved+green PR through the real ledger (offset 0 -> $after_offset), and recovered reviewed_head=$after_reviewed_head verdict=$after_verdict as its next lifecycle action"

echo ""
echo "============================================"
echo "  init-recovery E2E: all phases passed"
echo "============================================"
