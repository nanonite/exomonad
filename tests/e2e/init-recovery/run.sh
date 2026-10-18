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
# Phase 7 exercises what phases 1-6 deliberately don't: continuation of a
# reviewed, completed leaf through the embedded controller, real watcher,
# real ledger, and production merge tool. It verifies merge and post-merge
# completion, not just tmux repair. The checkpoint's review, handoff, and
# publication evidence are seeded from exact mock-Forgejo records so this
# phase tests the installed controller/tool boundary without simulating an
# agent or claiming a reviewer was recovered from the event stream.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/harness.sh
source "$SCRIPT_DIR/../lib/harness.sh"

# This recovery fixture never invokes a model. Keep project scaffolding
# deterministic when the isolated Codex home has no host model configuration.
export EXOMONAD_CODEX_MODEL="${EXOMONAD_CODEX_MODEL:-e2e-unused-model}"

SESSION="e2e-init-recovery"
CONTINUATION_SESSION="e2e-init-recovery-continuation"
AGGREGATE_SESSION="e2e-init-aggregate-recovery"
MOCK_PID=""
WASM_ARTIFACT_DIR=""

build_guest_wasm() {
    local role="$1"
    local package="wasm-guest-$role"
    local build_dir="$WORK_DIR/wasm-build"
    mkdir -p "$build_dir" "$WASM_ARTIFACT_DIR"
    (
        cd "$PROJECT_ROOT"
        nix develop .#wasm --command bash -c '
            set -euo pipefail
            export PATH="$PWD/.codex/tmp/bin:$PATH"
            build_dir="$1"
            package="$2"
            artifact_dir="$3"
            wasm32-wasi-cabal build --project-file=cabal.project.wasm \
                --builddir="$build_dir" "$package"
            mapfile -t artifacts < <(find "$build_dir" -type f -name "$package.wasm" -print)
            if (( ${#artifacts[@]} != 1 )); then
                printf "expected one fresh %s artifact under %s, found %s\\n" \
                    "$package" "$build_dir" "${#artifacts[@]}" >&2
                exit 1
            fi
            install -m 0644 "${artifacts[0]}" "$artifact_dir/$package.wasm"
            sha256sum "$artifact_dir/$package.wasm"
        ' e2e-wasm-build "$build_dir" "$package" "$WASM_ARTIFACT_DIR"
    )
}

install_verified_wasm_and_roles() {
    mkdir -p .exo/wasm
    for wasm_file in "$WASM_ARTIFACT_DIR"/wasm-guest-*.wasm; do
        [[ -s "$wasm_file" ]] || e2e_fail "verified scratch WASM artifact is missing: $wasm_file"
        ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
    done
    if [[ -d "$PROJECT_ROOT/.exo/roles" ]]; then
        rm -rf .exo/roles
        cp -r "$PROJECT_ROOT/.exo/roles" .exo/roles
    fi
}

count_pull_publications() {
    python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
count = 0
if log_path.is_file():
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = str(request.get("path", "")).split("?", 1)[0]
        if request.get("method") == "POST" and path.endswith("/api/v1/repos/owner/repo/pulls"):
            count += 1
print(count)
PY
}

assert_init_status() {
    local status="$1"
    local log_path="$2"
    local context="$3"
    if [[ "$status" -eq 0 ]]; then
        return
    fi
    if rg -Fq 'open terminal failed: not a terminal' "$log_path"; then
        e2e_log "$context returned the known detached-terminal status after init completed"
        return
    fi
    cat "$log_path"
    e2e_fail "$context exited with status $status"
}

assert_one_live_window() {
    local role="$1"
    local count state
    count="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc "$role" || true)"
    [[ "$count" == "1" ]] || e2e_fail "repeat init did not retain exactly one $role window"
    state="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{pane_dead}' | awk -v role="$role" '$1 == role { print $2, $3; exit }')"
    [[ "${state##* }" == "0" ]] || e2e_fail "repeat init returned without a live $role window"
}

run_managed_lock_fixture() {
    local test_name="$1"
    (
        cd "$PROJECT_ROOT"
        nix develop --command env \
            "EXOMONAD_INIT_LOCK_FIXTURE_PROJECT=$AGG_REPO" \
            cargo test -p exomonad --test init_git_lock_readiness "$test_name" \
                -- --ignored --exact --nocapture
    )
}

capture_local_edit_state() {
    python3 - "$AGG_REPO" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout

status = git("status", "--porcelain", "--untracked-files=all").decode()
assert "M  preserved-staged.txt" in status, status
assert " M preserved-unstaged.txt" in status, status
assert "?? preserved-untracked.txt" in status, status
print(json.dumps({
    "status": status,
    "staged_diff_sha256": hashlib.sha256(git("diff", "--cached", "--binary")).hexdigest(),
    "unstaged_diff_sha256": hashlib.sha256(git("diff", "--binary")).hexdigest(),
    "untracked_sha256": hashlib.sha256((repo / "preserved-untracked.txt").read_bytes()).hexdigest(),
}, sort_keys=True))
PY
}

e2e_phase "Phase 0" "Checking preconditions..."
e2e_require_commands nix tmux git curl python3

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
    tmux kill-session -t "$AGGREGATE_SESSION" 2>/dev/null
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null
        wait "$MOCK_PID" 2>/dev/null
    fi
    if (( code != 0 )); then
        KEEP_E2E_WORKDIR=1
    fi
    trap - EXIT
    (exit "$code")
    e2e_cleanup
}
e2e_create_work_dir "init-recovery"
trap cleanup EXIT INT TERM

e2e_phase "Runtime build" "Building the current ExoMonad binary before the disposable run..."
nix develop --command cargo build -p exomonad
e2e_find_exomonad

WASM_ARTIFACT_DIR="$WORK_DIR/wasm-artifacts"
e2e_phase "WASM build" "Building production guest WASM into isolated scratch output..."
build_guest_wasm devswarm
build_guest_wasm e2e-test
GUEST_WASM="$WASM_ARTIFACT_DIR/wasm-guest-devswarm.wasm"
E2E_WASM="$WASM_ARTIFACT_DIR/wasm-guest-e2e-test.wasm"
[[ -s "$GUEST_WASM" && -s "$E2E_WASM" ]] \
    || e2e_fail "isolated current-source guest WASM artifacts are missing"
GUEST_WASM_SHA256="$(sha256sum "$GUEST_WASM" | cut -d ' ' -f 1)"
E2E_WASM_SHA256="$(sha256sum "$E2E_WASM" | cut -d ' ' -f 1)"
e2e_log "Fresh devswarm WASM sha256=$GUEST_WASM_SHA256"
e2e_log "Fresh e2e-test WASM sha256=$E2E_WASM_SHA256"

if [[ "${E2E_AGGREGATE_ONLY:-0}" != "1" ]]; then
e2e_phase "Phase 1" "Creating scratch repository..."
REMOTE_DIR="$WORK_DIR/remote.git"
git init --bare "$REMOTE_DIR" -q
e2e_init_repo "Exomonad init-recovery E2E" "init-recovery-e2e@example.invalid"
git remote add origin "$REMOTE_DIR"
git push -u origin main -q
e2e_run_exomonad_new
install_verified_wasm_and_roles
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
mkdir -p .exo/tl-loop
printf '{"run_id":"root","plan":{"leaves":[]}}\n' > .exo/tl-loop/plan.json
e2e_write_basic_config "$SESSION"
FIRST_SERVER_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
printf 'port = %s\n' "$FIRST_SERVER_PORT" >> .exo/config.toml
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

e2e_phase "Phase 7" "Seeding a reviewed checkpoint and confirming production merge continuation..."

CONT_DIR="$WORK_DIR/continuation"
CONT_REPO="$CONT_DIR/repo"
CONT_REMOTE="$CONT_DIR/remote/owner/repo.git"
CONT_HOME="$CONT_DIR/home"
mkdir -p "$CONT_DIR"
mkdir -p "$CONT_HOME/.exo"
python3 "$PROJECT_ROOT/scripts/build_tl_loop_archive.py" \
    --source "$PROJECT_ROOT/tl_loop" --output "$CONT_HOME/.exo/tl_loop.pyz"
python3 "$PROJECT_ROOT/scripts/check_tl_loop_archive.py" \
    "$CONT_HOME/.exo/tl_loop.pyz" --source "$PROJECT_ROOT/tl_loop"
CONTROLLER_ARCHIVE_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$CONT_HOME/.exo/tl_loop.pyz")"
BUILD_ID="$(python3 -c 'import json,sys,zipfile; print(json.loads(zipfile.ZipFile(sys.argv[1]).read("tl_loop/_build_fingerprint.json"))["git_commit"])' \
    "$CONT_HOME/.exo/tl_loop.pyz")"
e2e_log "Built isolated controller archive sha256=$CONTROLLER_ARCHIVE_SHA256 build=$BUILD_ID"
cat > "$CONT_DIR/git-ssh" <<'EOF'
#!/bin/sh
E2E_GIT_REMOTE_DIR="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$CONT_REMOTE|" "$CONT_DIR/git-ssh"
chmod +x "$CONT_DIR/git-ssh"
export GIT_SSH_COMMAND="$CONT_DIR/git-ssh"
export E2E_GIT_REMOTE_DIR="$CONT_REMOTE"

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
REMOTE_DIR="$CONT_REMOTE" MOCK_LOG="$CONT_DIR/mock.log" MOCK_ADVANCE_BASE_ON_MERGE=1 \
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
# Keep Git operations local while exposing the canonical SSH remote as the
# repository identity consumed by the controller and merge host.
git remote set-url origin "git@forgejo-e2e:owner/repo.git"
git remote set-url --push origin "git@forgejo-e2e:owner/repo.git"
git config core.sshCommand "$CONT_DIR/git-ssh"
mkdir -p .exo/wasm
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    ln -sf "$wasm_file" ".exo/wasm/$(basename "$wasm_file")"
done
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
mkdir -p .exo/tl-loop
printf '{"run_id":"root","plan":{"leaves":[{"name":"leaf-a","task":"leaf-a change","chainlink_issue_id":null}]}}\n' > .exo/tl-loop/plan.json
cp .exo/tl-loop/plan.json .exo/tl-loop/plan.snapshot
python3 - <<'PY'
from hashlib import sha256
from pathlib import Path

snapshot = Path(".exo/tl-loop/plan.snapshot").read_bytes()
Path(".exo/tl-loop/plan.snapshot.sha256").write_text(sha256(snapshot).hexdigest() + "\n")
PY
CONT_SERVER_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > .exo/config.toml <<EOF
shell_command = "bash"
tmux_session = "$CONTINUATION_SESSION"
yolo = true
poll_interval = 1
port = $CONT_SERVER_PORT
spawn_agent_type = "codex"
forgejo_url = "$MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"
EOF

read -r PR_NUMBER HEAD_SHA < <(
    python3 "$SCRIPT_DIR/seed_publication.py" \
        --mock-url "$MOCK_URL" --repo "$CONT_REPO" \
        --head-branch "main.leaf-a" --head-sha "$LEAF_SHA" --slice-id "leaf-a" \
        --reviewer-token "reviewer-token-e2e"
)
[[ "$HEAD_SHA" == "$LEAF_SHA" ]] || e2e_fail "seed_publication.py returned an unexpected head SHA"
e2e_log "Filed and approved PR #$PR_NUMBER against the mock Forgejo (head=$HEAD_SHA)"

PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/seed_checkpoint.py" \
    --repo "$CONT_REPO" --mock-url "$MOCK_URL" --pr-number "$PR_NUMBER" \
    --head-sha "$HEAD_SHA" --branch "main.leaf-a" --slice-id "leaf-a"

BEFORE_STATUS="$(python3 -c "
import json
d = json.load(open('.exo/tl-loop/root/run.json'))
s = d['slices']['leaf-a']
print(s['reviewed_head'], s['verdict'], d['events']['last_consumed_offset'])
")"
read -r before_reviewed_head before_verdict before_offset <<< "$BEFORE_STATUS"
[[ "$before_reviewed_head" == "$HEAD_SHA" && "$before_verdict" == "GO" && "$before_offset" == "0" ]] \
    || e2e_fail "seeded checkpoint was not in the expected pre-recovery state: $BEFORE_STATUS"
e2e_log "Seeded reviewed checkpoint: pr_number=$PR_NUMBER reviewed_head=$HEAD_SHA verdict=GO (awaiting integration)"

tmux kill-session -t "$CONTINUATION_SESSION" 2>/dev/null || true
HOME="$CONT_HOME" "$EXOMONAD_BIN" init --continue --session "$CONTINUATION_SESSION" \
    > "$CONT_DIR/init.log" 2>&1 || true
rg -Fq "server_build=\"$BUILD_ID\" controller_build=\"$BUILD_ID\"" "$CONT_DIR/init.log" \
    || { cat "$CONT_DIR/init.log"; e2e_fail "continuation binary and controller compatibility fingerprints did not match"; }

AFTER_STATUS=""
for _ in $(seq 1 50); do
    AFTER_STATUS="$(python3 -c "
import json
d = json.load(open('.exo/tl-loop/root/run.json'))
s = d['slices']['leaf-a']
post = s.get('post_merge') or {}
phase = post.get('phase') if isinstance(post, dict) else None
journal = json.load(open('.exo/tl-loop/root/action-journal.json'))
merges = [entry for entry in journal if entry.get('operation') == 'merge_pr' and entry.get('target') == 'leaf-a' and entry.get('status') == 'confirmed']
print(s['reviewed_head'], s['verdict'], s['status'], phase, len(merges), d['events']['last_consumed_offset'])
" 2>/dev/null)" || AFTER_STATUS=""
    [[ "$AFTER_STATUS" == "$HEAD_SHA GO merged complete 1 "* ]] && break
    sleep 0.2
done
read -r after_reviewed_head after_verdict after_status after_post_merge after_merge_count after_offset <<< "$AFTER_STATUS"

if [[ "$after_reviewed_head" != "$HEAD_SHA" || "$after_verdict" != "GO" \
    || "$after_status" != "merged" || "$after_post_merge" != "complete" \
    || "$after_merge_count" != "1" ]]; then
    cat "$CONT_DIR/init.log"
    cat "$CONT_REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    e2e_fail "embedded controller did not merge and finish the reviewed leaf after restart: got '$AFTER_STATUS'"
fi
[[ "$after_offset" -gt 0 ]] || e2e_fail "embedded controller did not consume any ledger events during recovery"
[[ "$(rg -c 'pulls/1/merge' "$CONT_DIR/mock.log" || true)" == "1" ]] \
    || e2e_fail "production merge tool did not issue exactly one Forgejo merge request"
e2e_log "PASS: bundled controller resumed the reviewed checkpoint, used the production merge tool once, and completed post-merge bookkeeping (offset 0 -> $after_offset, head=$after_reviewed_head)"
fi

e2e_phase "Phase 8" "Recovering a failed root with a parked aggregate through actual init --continue..."

if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
fi

AGG_DIR="$WORK_DIR/aggregate-recovery"
AGG_REPO="$AGG_DIR/repo"
AGG_REMOTE="$AGG_DIR/remote/owner/repo.git"
AGG_HOME="$AGG_DIR/home"
mkdir -p "$AGG_DIR" "$AGG_HOME/.exo" "$(dirname "$AGG_REMOTE")" "$AGG_REPO"
python3 "$PROJECT_ROOT/scripts/build_tl_loop_archive.py" \
    --source "$PROJECT_ROOT/tl_loop" --output "$AGG_HOME/.exo/tl_loop.pyz"
python3 "$PROJECT_ROOT/scripts/check_tl_loop_archive.py" \
    "$AGG_HOME/.exo/tl_loop.pyz" --source "$PROJECT_ROOT/tl_loop"
AGGREGATE_ARCHIVE_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$AGG_HOME/.exo/tl_loop.pyz")"
AGGREGATE_BUILD_ID="$(python3 -c 'import json,sys,zipfile; print(json.loads(zipfile.ZipFile(sys.argv[1]).read("tl_loop/_build_fingerprint.json"))["git_commit"])' \
    "$AGG_HOME/.exo/tl_loop.pyz")"
e2e_log "Built aggregate recovery archive sha256=$AGGREGATE_ARCHIVE_SHA256 build=$AGGREGATE_BUILD_ID"

git init --bare -q "$AGG_REMOTE"
git -C "$AGG_REPO" init -q -b main
git -C "$AGG_REPO" remote add origin "file://$AGG_REMOTE"
git -C "$AGG_REPO" config user.name "Exomonad aggregate recovery E2E"
git -C "$AGG_REPO" config user.email "aggregate-recovery-e2e@example.invalid"
git -C "$AGG_REPO" commit --allow-empty -q -m "aggregate recovery base"
printf 'tracked staged baseline\n' > "$AGG_REPO/preserved-staged.txt"
printf 'tracked unstaged baseline\n' > "$AGG_REPO/preserved-unstaged.txt"
git -C "$AGG_REPO" add preserved-staged.txt preserved-unstaged.txt
git -C "$AGG_REPO" commit -q -m "add local edit preservation baselines"
git -C "$AGG_REPO" push -u origin main -q

AGG_MOCK_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
AGG_MOCK_URL="http://127.0.0.1:$AGG_MOCK_PORT"
REMOTE_DIR="$AGG_REMOTE" MOCK_LOG="$AGG_DIR/mock.log" MOCK_ADVANCE_BASE_ON_MERGE=1 \
    python3 "$PROJECT_ROOT/tests/e2e/mock_github.py" --port "$AGG_MOCK_PORT" \
    > "$AGG_DIR/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    curl -fsS "$AGG_MOCK_URL/api/v1/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$AGG_MOCK_URL/api/v1/admin/actions/runners" >/dev/null \
    || { cat "$AGG_DIR/mock.stderr"; e2e_fail "aggregate recovery mock Forgejo API did not become ready"; }

cd "$AGG_REPO"
HOME="$AGG_HOME" "$EXOMONAD_BIN" new > "$AGG_DIR/new.log" 2>&1 \
    || { cat "$AGG_DIR/new.log"; e2e_fail "'exomonad new' failed for aggregate recovery fixture"; }
install_verified_wasm_and_roles
cp "$PROJECT_ROOT/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$PROJECT_ROOT/.exo/review-policy.toml" .exo/review-policy.toml
cp "$PROJECT_ROOT/.exo/harness_capability.toml" .exo/harness_capability.toml
cat > "$AGG_DIR/git-ssh" <<'EOF'
#!/bin/sh
E2E_GIT_REMOTE_DIR="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${E2E_GIT_REMOTE_DIR:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$AGG_REMOTE|" "$AGG_DIR/git-ssh"
chmod +x "$AGG_DIR/git-ssh"
git remote set-url origin "git@forgejo-e2e:owner/repo.git"
git remote set-url --push origin "git@forgejo-e2e:owner/repo.git"
git config core.sshCommand "$AGG_DIR/git-ssh"
export GIT_SSH_COMMAND="$AGG_DIR/git-ssh"
export E2E_GIT_REMOTE_DIR="$AGG_REMOTE"

PYTHONPATH="$PROJECT_ROOT" python3 "$SCRIPT_DIR/seed_aggregate_checkpoint.py" \
    --repo "$AGG_REPO" --mock-url "$AGG_MOCK_URL" --output "$AGG_DIR/seed.json" \
    > "$AGG_DIR/seed.log" \
    || { cat "$AGG_DIR/seed.log"; cat "$AGG_DIR/mock.stderr"; e2e_fail "failed to seed failed-root aggregate recovery state"; }
AGGREGATE_PR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["aggregate_pr"])' "$AGG_DIR/seed.json")"
AGGREGATE_BASE_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_sha"])' "$AGG_DIR/seed.json")"
AGGREGATE_REMOTE_HEAD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["aggregate_remote_head"])' "$AGG_DIR/seed.json")"
LEAF_PR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["leaf_pr"])' "$AGG_DIR/seed.json")"
LEAF_RECEIPT_SHA256="$(python3 -c 'import hashlib,json,sys; d=json.load(open(sys.argv[1])); r=json.load(open(d["child"])); e=r["slices"]["issue-811"]["post_merge"]["evidence"]; print(hashlib.sha256(json.dumps(e,sort_keys=True,separators=(",", ":")).encode()).hexdigest())' "$AGG_DIR/seed.json")"
ARCHITECTURE_INVOCATION_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$AGG_REPO/.exo/agents/architecture/invocation.json")"
[[ "$(git -C "$AGG_REPO" rev-parse main)" == "$AGGREGATE_BASE_SHA" ]] \
    || e2e_fail "fixture did not leave the local parent branch behind the merged aggregate"
[[ "$(git -C "$AGG_REPO" rev-parse origin/main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "fixture remote parent branch does not point at the confirmed aggregate merge"
AGGREGATE_MERGE_REQUESTS_BEFORE="$(rg -c "pulls/$AGGREGATE_PR/merge" "$AGG_DIR/mock.log" || true)"
[[ "$AGGREGATE_MERGE_REQUESTS_BEFORE" == "1" ]] \
    || e2e_fail "fixture did not record exactly one authoritative remote aggregate merge"
PULL_PUBLICATIONS_BEFORE="$(count_pull_publications "$AGG_DIR/mock.log")"
[[ "$PULL_PUBLICATIONS_BEFORE" == "2" ]] \
    || e2e_fail "fixture expected only its leaf and aggregate PR publications, found $PULL_PUBLICATIONS_BEFORE"
e2e_log "Seeded merged aggregate PR #$AGGREGATE_PR at remote head $AGGREGATE_REMOTE_HEAD with local parent HEAD still at $AGGREGATE_BASE_SHA"
LOCAL_EDIT_STATE_BEFORE="$(capture_local_edit_state)"
printf '%s\n' "$LOCAL_EDIT_STATE_BEFORE" > "$AGG_DIR/local-edits-before.json"
e2e_log "Seeded unrelated staged, unstaged, and untracked edits"
run_managed_lock_fixture create_managed_orphan_lock_fixture_for_e2e
[[ -e "$AGG_REPO/.git/index.lock" && ! -s "$AGG_REPO/.git/index.lock" ]] \
    || e2e_fail "ProcessHandler did not leave the proven zero-byte native index lock"
e2e_log "Created a managed orphan lock through the production ProcessHandler timeout path"
AGGREGATE_SERVER_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > .exo/config.toml <<EOF
shell_command = "bash"
tmux_session = "$AGGREGATE_SESSION"
yolo = true
poll_interval = 1
port = $AGGREGATE_SERVER_PORT
spawn_agent_type = "codex"
forgejo_url = "$AGG_MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"
EOF

tmux kill-session -t "$AGGREGATE_SESSION" 2>/dev/null || true
tmux new-session -d -s "$AGGREGATE_SESSION" -n TL "sleep 0.2; exit 0"
tmux set-window-option -t "$AGGREGATE_SESSION:TL" remain-on-exit on
for _ in $(seq 1 30); do
    [[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{pane_dead}' | awk '$1 == "TL" { print $2; exit }')" == "1" ]] && break
    sleep 0.1
done
DEAD_TL_ID="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{window_id} #{pane_dead}' | awk '$1 == "TL" { print $2, $3; exit }')"
[[ "${DEAD_TL_ID##* }" == "1" ]] || e2e_fail "aggregate fixture did not start with an exited TL pane"
read -r DEAD_TL_WINDOW _ <<< "$DEAD_TL_ID"

AGG_INIT_STATUS=0
HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" \
    "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
    > "$AGG_DIR/init.log" 2>&1 || AGG_INIT_STATUS=$?
rg -Fq "server_build=\"$AGGREGATE_BUILD_ID\" controller_build=\"$AGGREGATE_BUILD_ID\"" "$AGG_DIR/init.log" \
    || { cat "$AGG_DIR/init.log"; e2e_fail "aggregate continuation binary/archive fingerprints did not match"; }
if rg -Fq 'cannot inspect managed process descriptors (permission denied)' "$AGG_DIR/init.log"; then
    [[ "$AGG_INIT_STATUS" -ne 0 ]] \
        || e2e_fail "limited-inspection refusal returned success instead of a nonzero init status"
    [[ -e "$AGG_REPO/.git/index.lock" && ! -s "$AGG_REPO/.git/index.lock" ]] \
        || e2e_fail "limited-inspection refusal changed or removed the zero-byte native lock"
    [[ "$(capture_local_edit_state)" == "$LOCAL_EDIT_STATE_BEFORE" ]] \
        || e2e_fail "limited-inspection refusal changed unrelated local edits"
    [[ "$(git -C "$AGG_REPO" rev-parse main)" == "$AGGREGATE_BASE_SHA" ]] \
        || e2e_fail "limited-inspection refusal changed the local parent branch"
    CURRENT_TL_ID="$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{window_id} #{pane_dead}' | awk '$1 == "TL" { print $2, $3; exit }')"
    [[ "$CURRENT_TL_ID" == "$DEAD_TL_WINDOW 1" ]] \
        || e2e_fail "limited-inspection refusal restarted or replaced the exited TL pane"
    [[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc Server || true)" == "0" ]] \
        || e2e_fail "limited-inspection refusal started a Server before Git readiness"
    [[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc Watcher || true)" == "0" ]] \
        || e2e_fail "limited-inspection refusal started a Watcher before Git readiness"
    python3 - "$AGG_REPO" <<'PY' || e2e_fail "limited-inspection refusal recorded a false quarantine"
import json
import sys
from pathlib import Path

segments = Path(sys.argv[1]) / ".exo/ledger/segments"
events = [
    json.loads(line)
    for path in segments.glob("segment-*.jsonl")
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
assert not any(
    event.get("type") == "git.native_lock.quarantined" for event in events
)
PY
    e2e_fail "positive init recovery is unavailable: this runtime cannot inspect /proc descriptors (permission denied); fail-closed refusal preserved the lock and local edits, and the complete fixture is retained at $WORK_DIR"
fi
assert_init_status "$AGG_INIT_STATUS" "$AGG_DIR/init.log" "first aggregate init --continue"
[[ "$(rg -F -c 'Recovered a proven orphan native Git lock' "$AGG_DIR/init.log" || true)" == "1" ]] \
    || { cat "$AGG_DIR/init.log"; e2e_fail "init did not report exactly one proven lock recovery"; }
[[ "$(count_pull_publications "$AGG_DIR/mock.log")" == "$PULL_PUBLICATIONS_BEFORE" ]] \
    || e2e_fail "first continuation published a new PR instead of adopting the existing aggregate"

AGGREGATE_RESULT=""
for _ in $(seq 1 240); do
    AGGREGATE_RESULT="$(python3 - "$AGG_REPO/.exo/tl-loop/root/run.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    print("missing")
    raise SystemExit(0)
state = json.loads(path.read_text())
fsm = state.get("fsm", {})
slices = state.get("slices", {})
architecture = slices.get("architecture", {})
kani = slices.get("kani", {})
post = architecture.get("post_merge") or {}
phase = post.get("phase") if isinstance(post, dict) else None
print(
    state.get("phase", fsm.get("phase", "unknown")),
    state.get("current_order", -1),
    architecture.get("status", "missing"),
    phase or "none",
    kani.get("status", "missing"),
    kani.get("dispatch_intent_id") or "none",
)
PY
    2>/dev/null)" || AGGREGATE_RESULT=""
    [[ "$AGGREGATE_RESULT" == "tl_done 2 merged complete merged "* ]] && break
    sleep 0.25
done
read -r final_phase final_order architecture_status architecture_post kani_status kani_intent <<< "$AGGREGATE_RESULT"
if [[ "$final_phase" != "tl_done" || "$final_order" != "2" \
    || "$architecture_status" != "merged" || "$architecture_post" != "complete" \
    || "$kani_status" != "merged" || "$kani_intent" == "none" ]]; then
    cat "$AGG_DIR/init.log"
    cat "$AGG_REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    e2e_fail "failed-root aggregate did not recover through integration and the next stage: '$AGGREGATE_RESULT'"
fi

python3 - "$AGG_REPO" "$AGGREGATE_PR" "$LEAF_PR" "$LEAF_RECEIPT_SHA256" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
aggregate_pr = int(sys.argv[2])
leaf_pr = int(sys.argv[3])
expected_receipt = sys.argv[4]
root_dir = repo / ".exo/tl-loop/root"
root = json.loads((root_dir / "run.json").read_text())
child = json.loads((root_dir / "architecture/run.json").read_text())
leaf_evidence = child["slices"]["issue-811"]["post_merge"]["evidence"]
receipt_hash = hashlib.sha256(
    json.dumps(leaf_evidence, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert receipt_hash == expected_receipt, "completed issue #811 receipt changed during recovery"
assert child["fsm"]["phase"] == "tl_pr_filed", "architecture handoff phase was changed"
assert leaf_evidence["pr_number"] == str(leaf_pr)
assert leaf_evidence["issue_id"] == "811"
assert leaf_evidence["issue_close_outcome"] == "closed"
assert not any(
    identity.get("slice_id") == "issue-811"
    for identity_path in (repo / ".exo/agents").glob("*/identity.json")
    for identity in [json.loads(identity_path.read_text())]
), "completed issue #811 was dispatched as a new worker"
assert not any(gate["name"] == "tl-ordered-child-recovery-ambiguous" for gate in root["gates"])
candidate = root["integration"]["candidates"]["architecture"]
assert candidate["lifecycle"] == "MERGED"
assert candidate["aggregate_pr_number"] == aggregate_pr
assert candidate["active_aggregate_approval_id"]
assert len(candidate["aggregate_approval_receipts"]) == 1
journal = json.loads((root_dir / "action-journal.json").read_text())
aggregate_merges = [
    entry
    for entry in journal
    if entry.get("operation") == "merge_pr" and entry.get("target") == "architecture"
]
assert sum(entry.get("status") == "rejected" for entry in aggregate_merges) == 1
assert sum(entry.get("status") == "confirmed" for entry in aggregate_merges) == 1
approved_merge = next(
    entry
    for entry in aggregate_merges
    if entry.get("status") == "confirmed"
)
assert (
    approved_merge["result"]["result"].get("aggregate_approval_receipt_id")
    == candidate["active_aggregate_approval_id"]
), "the durable merge result is not bound to the retained parent approval"
sync_entries = [
    entry
    for entry in journal
    if entry.get("operation") == "post_merge_parent_sync" and entry.get("target") == "architecture"
]
prior_refusals = [
    entry
    for entry in sync_entries
    if isinstance(entry.get("result"), dict)
    and isinstance(entry["result"].get("result"), dict)
    and entry["result"]["result"].get("sync_outcome") == "pre_mutation_refusal"
]
confirmed_syncs = [
    entry
    for entry in sync_entries
    if isinstance(entry.get("result"), dict)
    and isinstance(entry["result"].get("result"), dict)
    and entry["result"]["result"].get("sync_outcome") == "confirmed"
]
assert len(prior_refusals) == 1, "the immutable prior sync refusal was not retained"
assert len(confirmed_syncs) == 1, "parent synchronization was not confirmed exactly once"
assert confirmed_syncs[0]["arguments"].get("sync_attempt") == 2
kani_dispatches = [
    entry
    for entry in journal
    if entry.get("operation") == "emit_controller_event"
    and entry.get("target") == "kani"
    and isinstance(entry.get("arguments"), dict)
    and entry["arguments"].get("event_type") == "tl.dispatch_confirmed"
    and isinstance(entry["arguments"].get("payload"), dict)
    and entry["arguments"]["payload"].get("boundary") == "sub_tl_started"
]
assert len(kani_dispatches) == 1, f"expected one Kani stage dispatch, found {len(kani_dispatches)}"
assert not any(
    entry.get("operation") == "spawn_reviewer" and entry.get("target") == "architecture"
    for entry in journal
), "aggregate recovery started a root reviewer"
PY

LOCAL_EDIT_STATE_AFTER="$(capture_local_edit_state)"
[[ "$LOCAL_EDIT_STATE_AFTER" == "$LOCAL_EDIT_STATE_BEFORE" ]] \
    || e2e_fail "parent synchronization or bookkeeping changed unrelated local edits"
[[ "$(git -C "$AGG_REPO" rev-parse main)" == "$AGGREGATE_REMOTE_HEAD" ]] \
    || e2e_fail "confirmed local synchronization did not advance main to the verified merge commit"
run_managed_lock_fixture assert_managed_lock_quarantined_once_for_e2e

[[ "$(rg -c "pulls/$AGGREGATE_PR/merge" "$AGG_DIR/mock.log" || true)" == "$AGGREGATE_MERGE_REQUESTS_BEFORE" ]] \
    || e2e_fail "continuation repeated the aggregate merge instead of resuming local synchronization"
[[ "$(count_pull_publications "$AGG_DIR/mock.log")" == "$PULL_PUBLICATIONS_BEFORE" ]] \
    || e2e_fail "continuation published a replacement aggregate PR"
[[ "$(rg -c "pulls/$LEAF_PR/merge" "$AGG_DIR/mock.log" || true)" == "1" ]] \
    || e2e_fail "completed leaf PR was merged again during aggregate recovery"
[[ "$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$AGG_REPO/.exo/agents/architecture/invocation.json")" == "$ARCHITECTURE_INVOCATION_SHA256" ]] \
    || e2e_fail "completed architecture producer invocation changed during aggregate recovery"
read -r RECOVERED_TL_WINDOW RECOVERED_TL_DEAD < <(
    tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name} #{window_id} #{pane_dead}' \
        | awk '$1 == "TL" { print $2, $3; exit }'
)
[[ "$RECOVERED_TL_WINDOW" != "$DEAD_TL_WINDOW" ]] \
    || e2e_fail "init --continue did not replace the existing exited TL pane"
[[ "$(tmux list-windows -t "$AGGREGATE_SESSION" -F '#{window_name}' | grep -Fxc TL || true)" == "1" ]] \
    || e2e_fail "aggregate recovery created duplicate TL windows"

REPEAT_INIT_STATUS=0
HOME="$AGG_HOME" FORGEJO_TOKEN="test-token-e2e" \
    "$EXOMONAD_BIN" init --continue --session "$AGGREGATE_SESSION" \
    > "$AGG_DIR/init-repeat.log" 2>&1 || REPEAT_INIT_STATUS=$?
assert_init_status "$REPEAT_INIT_STATUS" "$AGG_DIR/init-repeat.log" "repeated aggregate init --continue"
rg -Fq "server_build=\"$AGGREGATE_BUILD_ID\" controller_build=\"$AGGREGATE_BUILD_ID\"" "$AGG_DIR/init-repeat.log" \
    || { cat "$AGG_DIR/init-repeat.log"; e2e_fail "repeat init did not attach using the verified binary/archive"; }
assert_one_live_window Server
assert_one_live_window Watcher
assert_one_live_window TL
[[ "$(rg -c "pulls/$AGGREGATE_PR/merge" "$AGG_DIR/mock.log" || true)" == "$AGGREGATE_MERGE_REQUESTS_BEFORE" ]] \
    || e2e_fail "repeated continuation duplicated the aggregate merge"
[[ "$(count_pull_publications "$AGG_DIR/mock.log")" == "$PULL_PUBLICATIONS_BEFORE" ]] \
    || e2e_fail "repeat init published a replacement aggregate PR"
[[ "$(rg -F -c 'Recovered a proven orphan native Git lock' "$AGG_DIR/init-repeat.log" || true)" == "0" ]] \
    || e2e_fail "repeated init reported an already-completed lock recovery again"
LOCAL_EDIT_STATE_AFTER_REPEAT="$(capture_local_edit_state)"
[[ "$LOCAL_EDIT_STATE_AFTER_REPEAT" == "$LOCAL_EDIT_STATE_BEFORE" ]] \
    || e2e_fail "repeated continuation changed unrelated local edits"
run_managed_lock_fixture assert_managed_lock_quarantined_once_for_e2e
e2e_log "PASS: actual init --continue resumed a previously merged aggregate without repeating merge/publication/leaf work, recovered one proven lock, preserved local edits, completed bookkeeping, and dispatched Kani once (archive=$AGGREGATE_ARCHIVE_SHA256, devswarm=$GUEST_WASM_SHA256, e2e-test=$E2E_WASM_SHA256, build=$AGGREGATE_BUILD_ID)"

echo ""
echo "============================================"
if [[ "${E2E_AGGREGATE_ONLY:-0}" == "1" ]]; then
    echo "  aggregate init-recovery E2E: passed"
else
    echo "  init-recovery E2E: all phases passed"
fi
echo "============================================"
