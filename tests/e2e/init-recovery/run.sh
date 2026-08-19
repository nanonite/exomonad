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
# This does not exercise TL business logic — the seeded plan has no work,
# so the TL controller exits immediately after each `exomonad init` call.
# That failure is expected and tolerated: this test asserts tmux window
# state and server socket health, not controller outcomes. TL crash/restart
# *logic* is covered separately and thoroughly by tests/e2e/ordered-recursive
# at the Python-controller level; this test covers what that harness
# cannot: the real binary's tmux session reconciliation.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

SESSION="e2e-init-recovery"

e2e_preflight tmux git curl

cleanup() {
    local code=$?
    tmux kill-session -t "$SESSION" 2>/dev/null || true
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

echo ""
echo "============================================"
echo "  init-recovery E2E: all phases passed"
echo "============================================"
