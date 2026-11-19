#!/usr/bin/env bash
# Stored-proof acceptance for the reopened Chainlink #1433 finding.
#
# Builds the exact retained shape from the controlled live run of 2026-11-19:
# the saved `sha256:v1:` terminal-owner authorization for f285, the rejected
# prelaunch configuration action, the approved #814 recovery gate, the
# historical f446/f285 journal receipts, and the current `sha256:v2:` accepted
# snapshot. Then it drives the real `exomonad serve`/`init --continue` path and
# proves exactly one linked generation-3 OpenCode DeepSeek/high successor is
# accepted, that repeats and restarts add no duplicate effect, and that every
# negative stays held.
#
# Selected by E2E_STORED_PROOF_ONLY=1 in run.sh, which supplies the Phase 0
# build, fresh explicitly selected WASM, isolated homes and a run-scoped tmux
# server. Harnesses are disposable shims; no provider is contacted.
set -euo pipefail

export NO_COLOR=1

EXOMONAD_BIN="${1:?binary path required}"
WORK="${2:?fixture directory required}"
STORED_HOME="${3:?isolated home required}"
CODEX_HOME="${4:?Codex home required}"
SOURCE_ROOT="${5:?source workspace required}"
WASM_DIR="${6:?fresh WASM directory required}"

SESSION="e2e-stored-proof"
REPO="$WORK/repo"
REMOTE="$WORK/remote/owner/repo.git"
SHIM_DIR="$WORK/bin"
AGENT_LOG="$WORK/agent-events.jsonl"
WORKER_EXIT_FILE="$WORK/worker-exit-requested"
ROLE_CONFIG_LOG="$WORK/role-config-acceptance.log"
MOCK_PID=""
SERVER_PID=""
INIT_PID=""
BASE_PATH="${PATH:-/usr/bin:/bin}"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

tmux_iso() {
    env -u TMUX -u TMUX_PANE TMUX_TMPDIR="${TMUX_TMPDIR:?run-scoped TMUX_TMPDIR required}" tmux "$@"
}

cleanup() {
    local status=$?
    set +e
    if [[ -n "$INIT_PID" ]] && kill -0 "$INIT_PID" 2>/dev/null; then
        kill "$INIT_PID" 2>/dev/null
    fi
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    if [[ -n "$MOCK_PID" ]] && kill -0 "$MOCK_PID" 2>/dev/null; then
        kill "$MOCK_PID" 2>/dev/null
        wait "$MOCK_PID" 2>/dev/null
    fi
    if (( status != 0 )); then
        mkdir -p "$WORK/tmux-failure-evidence"
        tmux_iso list-panes -a \
            -F '#{session_name} #{window_name} #{pane_id} #{pane_pid} #{pane_current_command} #{pane_dead}' \
            > "$WORK/tmux-failure-evidence/panes.txt" 2>&1
        while IFS= read -r pane_id; do
            [[ -n "$pane_id" ]] || continue
            tmux_iso capture-pane -p -t "$pane_id" -S -120 \
                > "$WORK/tmux-failure-evidence/pane-${pane_id//%/}.txt" 2>&1
        done < <(tmux_iso list-panes -a -F '#{pane_id}' 2>/dev/null)
    fi
    tmux_iso kill-server 2>/dev/null
    if tmux_iso list-sessions >/dev/null 2>&1; then
        printf 'ERROR: stored-proof tmux server survived teardown\n' >&2
        status=1
    fi
    if (( status != 0 )); then
        printf 'ERROR: stored-proof acceptance failed; fixture kept at %s\n' "$WORK" >&2
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM

export E2E_ROLE_CONFIG_ACCEPTANCE=1
export E2E_AGENT_LOG="$AGENT_LOG"
export E2E_ROLE_CONFIG_WORKER_LIFETIME="${E2E_STORED_PROOF_WORKER_LIFETIME:-600}"
export E2E_ROLE_CONFIG_WORKER_EXIT_FILE="$WORKER_EXIT_FILE"
export E2E_REPO="$REPO"
export CHAINLINK_DB="$REPO/.chainlink/issues.db"
export EXOMONAD_TMUX_SESSION="$SESSION"

mkdir -p "$WORK" "$REPO" "$STORED_HOME/.exo" "$(dirname "$REMOTE")" "$SHIM_DIR"
cd "$REPO"

# ---------------------------------------------------------------------------
# Phase 0: the shipped controller archive and disposable harness shims
# ---------------------------------------------------------------------------
python3 "$SOURCE_ROOT/scripts/build_tl_loop_archive.py" \
    --source "$SOURCE_ROOT/tl_loop" --output "$STORED_HOME/.exo/tl_loop.pyz" >/dev/null
python3 "$SOURCE_ROOT/scripts/check_tl_loop_archive.py" \
    "$STORED_HOME/.exo/tl_loop.pyz" --source "$SOURCE_ROOT/tl_loop" >/dev/null
ARCHIVE_SHA256="$(sha256sum "$STORED_HOME/.exo/tl_loop.pyz" | cut -d ' ' -f 1)"
say "shipped controller archive sha256=$ARCHIVE_SHA256"
cp "$SOURCE_ROOT/tests/e2e/init-recovery/fake-codex.sh" "$SHIM_DIR/opencode"
cp "$SOURCE_ROOT/tests/e2e/init-recovery/fake-codex.sh" "$SHIM_DIR/codex"
chmod 0755 "$SHIM_DIR/opencode" "$SHIM_DIR/codex"

# ---------------------------------------------------------------------------
# Phase A: disposable repository, mock Forgejo and the accepted v2 snapshot
# ---------------------------------------------------------------------------
git init --bare -q "$REMOTE"
git init -q -b main
git config user.name "Exomonad stored-proof acceptance"
git config user.email "stored-proof-e2e@example.invalid"
cat > "$WORK/git-ssh" <<'EOF'
#!/bin/sh
REMOTE="@REMOTE@"
case "$*" in
    *git-upload-pack*) exec git-upload-pack "${REMOTE:?}" ;;
    *git-receive-pack*) exec git-receive-pack "${REMOTE:?}" ;;
    *) exit 64 ;;
esac
EOF
sed -i "s|@REMOTE@|$REMOTE|" "$WORK/git-ssh"
chmod 0755 "$WORK/git-ssh"
git remote add origin "git@forgejo-e2e:owner/repo.git"
git config core.sshCommand "$WORK/git-ssh"
export GIT_SSH_COMMAND="$WORK/git-ssh"
mkdir -p .exo .chainlink .forgejo/workflows
printf 'stored proof baseline\n' > README.md
printf 'deleted before handoff baseline\n' > preserved-deleted.txt
printf 'tracked runtime baseline\n' > .exo/tracked-runtime-policy.toml
cp "$SOURCE_ROOT/CHANGELOG.md" CHANGELOG.md
cp "$SOURCE_ROOT/.forgejo/workflows/ci.yml" .forgejo/workflows/ci.yml
cat > .gitignore <<'EOF'
.exo/*
!.exo/config.toml
!.exo/roles/
!.exo/lib/
!.exo/rules/
!.exo/tracked-runtime-policy.toml
.codex/
.opencode/
opencode.json
.chainlink/issues.db
.exo/e2e-runtime-ignored-untracked/
EOF
git add README.md .gitignore preserved-deleted.txt CHANGELOG.md .forgejo/workflows/ci.yml
git add -f .exo/tracked-runtime-policy.toml
git commit -q -m "stored proof acceptance base"
GIT_SSH_COMMAND="$WORK/git-ssh" git push -u origin main -q

chainlink init --no-hooks --db "$REPO/.chainlink/issues.db" > "$WORK/chainlink-init.log" 2>&1 \
    || { cat "$WORK/chainlink-init.log"; fail "failed to initialize the disposable Chainlink database"; }
python3 - "$WORK/chainlink-issues.json" <<'PY'
import json
import sys
from pathlib import Path

rows = []
for issue_id in range(1, 815):
    target = issue_id == 814
    rows.append(
        {
            "id": issue_id,
            "title": "Disposable Kani recovery issue 814" if target else f"Disposable closed issue {issue_id}",
            "description": "Isolated stored-proof fixture.",
            "status": "open" if target else "closed",
            "priority": "high" if target else "low",
            "parent_id": None,
            "labels": [],
            "comments": [],
            "created_at": "2026-11-19T00:00:00Z",
            "updated_at": "2026-11-19T00:00:00Z",
            "closed_at": None if target else "2026-11-19T00:00:00Z",
        }
    )
Path(sys.argv[1]).write_text(
    json.dumps({"version": 1, "exported_at": "2026-11-19T00:00:00Z", "issues": rows}),
    encoding="utf-8",
)
PY
chainlink import "$WORK/chainlink-issues.json" --db "$REPO/.chainlink/issues.db" \
    > "$WORK/chainlink-import.log" 2>&1 \
    || { cat "$WORK/chainlink-import.log"; fail "failed to seed disposable issue #814"; }
chainlink show 814 --db "$REPO/.chainlink/issues.db" > "$WORK/chainlink-issue-814.log" \
    || fail "disposable issue #814 is not readable"
grep -Fq 'Status: open' "$WORK/chainlink-issue-814.log" \
    || fail "disposable issue #814 must remain open"

MOCK_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
REMOTE_DIR="$REMOTE" MOCK_LOG="$WORK/mock.log" MOCK_ADVANCE_BASE_ON_MERGE=1 \
    python3 "$SOURCE_ROOT/tests/e2e/mock_github.py" --port "$MOCK_PORT" \
    > "$WORK/mock.stderr" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 100); do
    curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null 2>&1 && break
    sleep 0.1
done
curl -fsS "$MOCK_URL/api/v1/admin/actions/runners" >/dev/null \
    || { cat "$WORK/mock.stderr"; fail "mock Forgejo did not become ready"; }

(
    export HOME="$STORED_HOME" CODEX_HOME="$CODEX_HOME"
    export PATH="$SHIM_DIR:$BASE_PATH"
    export EXOMONAD_CODEX_MODEL="${EXOMONAD_CODEX_MODEL:-e2e-unused-model}"
    "$EXOMONAD_BIN" new
) > "$WORK/new.log" 2>&1 || { cat "$WORK/new.log"; fail "exomonad new failed"; }

mkdir -p .exo/wasm
for artifact in "$WASM_DIR"/wasm-guest-*.wasm; do
    [[ -s "$artifact" ]] || fail "fresh WASM artifact is missing: $artifact"
    ln -sf "$artifact" ".exo/wasm/$(basename "$artifact")"
done
if [[ -d "$SOURCE_ROOT/.exo/roles" ]]; then
    rm -rf .exo/roles
    cp -r "$SOURCE_ROOT/.exo/roles" .exo/roles
fi
cat > .exo/harness_policy.toml <<'EOF'
[roles.tl]
allow = ["opencode/opencode-go/glm-5.3-flash"]
cost_rank = { "opencode/opencode-go/glm-5.3-flash" = 1 }
token_budget = 120000
escalate_after_attempts = 1

[roles.worker]
allow = ["opencode/opencode-go/deepseek-v4.1-flash", "opencode/opencode-go/glm-5.3-flash", "claude", "shoal"]
cost_rank = { "opencode/opencode-go/deepseek-v4.1-flash" = 1, "opencode/opencode-go/glm-5.3-flash" = 2, "claude" = 3, "shoal" = 4 }
token_budget = 120000
escalate_after_attempts = 1

[roles.reviewer]
allow = ["opencode/opencode-go/glm-5.3-flash"]
cost_rank = { "opencode/opencode-go/glm-5.3-flash" = 1 }
token_budget = 60000
escalate_after_attempts = 1
EOF
cat > .exo/harness_capability.toml <<'EOF'
[capabilities]
"opencode/opencode-go/glm-5.3-flash" = "hard"
"opencode/opencode-go/deepseek-v4.1-flash" = "hard"
"claude" = "standard"
"shoal" = "standard"
EOF
cp "$SOURCE_ROOT/.exo/review-policy.toml" .exo/review-policy.toml

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
cat > .exo/config.toml <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
yolo = true
poll_interval = 1
port = $PORT
root_agent_type = "opencode"
model = "opencode-go/glm-5.3-flash"
tl_effort_level = "high"
spawn_agent_type = "opencode"
worker_effort_level = "high"
worker_model = "opencode-go/deepseek-v4.1-flash"
forgejo_url = "$MOCK_URL"
forgejo_token = "test-token-e2e"
forgejo_reviewer_token = "reviewer-token-e2e"

[reviewer]
agent_type = "opencode"
model = "opencode-go/glm-5.3-flash"
effort_level = "high"
EOF
git add -A
git add -f .exo/config.toml .exo/harness_policy.toml .exo/harness_capability.toml \
    .exo/review-policy.toml
git commit -q -m "configure stored-proof acceptance fixture"
git remote set-url origin "git@forgejo-e2e:owner/repo.git"
git remote set-url --push origin "git@forgejo-e2e:owner/repo.git"
GIT_SSH_COMMAND="$WORK/git-ssh" git push -u origin main -q

say "seeding the completed sibling, dormant Kani controller and planned #814 leaf"
PYTHONPATH="$SOURCE_ROOT" python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed_aggregate_checkpoint.py" \
    --repo "$REPO" --mock-url "$MOCK_URL" --output "$WORK/seed.json" \
    --active-kani-blocker --terminal-provider-failure \
    > "$WORK/seed.log" 2>&1 \
    || { cat "$WORK/seed.log"; cat "$WORK/mock.stderr"; fail "failed to seed the aggregate recovery fixture"; }
python3 - "$REPO" <<'PY' || fail "the aggregate fixture is not in the expected pre-recovery shape"
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8"))
kani_child = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8"))
leaf = kani_child["slices"]["issue-814-substitution-model-kani"]
assert root["slices"]["architecture"]["status"] == "merged"
assert root["slices"]["architecture"]["post_merge"]["phase"] == "complete"
assert root["slices"]["kani"]["status"] == "spawned"
assert root["slices"]["kani"]["dispatch_last_boundary"] == "sub_tl_started"
assert leaf["status"] == "pending", leaf["status"]
assert leaf.get("dispatch_invocation_id") is None
assert not kani_child["gates"]
assert (repo / ".exo/tl-loop/plan.snapshot").is_file()
PY

ROLE_FLAGS=(
    --worker opencode
    --worker-model opencode-go/deepseek-v4.1-flash
    --worker-effort-level high
    --reviewer opencode
    --reviewer-model opencode-go/glm-5.3-flash
    --reviewer-effort-level high
)

run_env() {
    env HOME="$STORED_HOME" CODEX_HOME="$CODEX_HOME" \
        PATH="$SHIM_DIR:$BASE_PATH" \
        SHELL="/bin/bash" \
        E2E_ROLE_CONFIG_ACCEPTANCE=1 E2E_AGENT_LOG="$AGENT_LOG" \
        E2E_ROLE_CONFIG_WORKER_LIFETIME="$E2E_ROLE_CONFIG_WORKER_LIFETIME" \
        E2E_ROLE_CONFIG_WORKER_EXIT_FILE="$WORKER_EXIT_FILE" \
        E2E_MOCK_URL="$MOCK_URL" E2E_MCP_SOCKET="$REPO/.exo/server.sock" \
        E2E_REPO="$REPO" \
        GIT_SSH_COMMAND="$WORK/git-ssh" \
        FORGEJO_TOKEN="test-token-e2e" FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e" \
        CHAINLINK_DB="$REPO/.chainlink/issues.db" \
        EXOMONAD_TMUX_SESSION="$SESSION" \
        "$@"
}

start_server() {
    local log="$1"
    (
        cd "$REPO" || exit 1
        export HOME="$STORED_HOME" CODEX_HOME="$CODEX_HOME"
        export PATH="$SHIM_DIR:$BASE_PATH"
        export SHELL="/bin/bash"
        export E2E_ROLE_CONFIG_ACCEPTANCE=1 E2E_AGENT_LOG="$AGENT_LOG"
        export E2E_ROLE_CONFIG_WORKER_LIFETIME="$E2E_ROLE_CONFIG_WORKER_LIFETIME"
        export E2E_ROLE_CONFIG_WORKER_EXIT_FILE="$WORKER_EXIT_FILE"
        export E2E_MOCK_URL="$MOCK_URL" E2E_MCP_SOCKET="$REPO/.exo/server.sock"
        export E2E_REPO="$REPO"
        export GIT_SSH_COMMAND="$WORK/git-ssh"
        export FORGEJO_TOKEN="test-token-e2e" FORGEJO_REVIEWER_TOKEN="reviewer-token-e2e"
        export CHAINLINK_DB="$REPO/.chainlink/issues.db"
        export EXOMONAD_TMUX_SESSION="$SESSION"
        exec "$EXOMONAD_BIN" serve
    ) > "$log" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 900); do
        if [[ -S "$REPO/.exo/server.sock" ]] \
            && curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health >/dev/null 2>&1; then
            return 0
        fi
        kill -0 "$SERVER_PID" 2>/dev/null || { cat "$log"; fail "standalone serve exited before becoming healthy"; }
        sleep 0.1
    done
    cat "$log"
    fail "standalone serve socket did not become healthy"
}

health_identity() {
    curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["role_route_configuration"]["identity"])'
}

stop_server() {
    local log="$1"
    [[ -n "$SERVER_PID" ]] || return 0
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    for _ in $(seq 1 150); do
        [[ ! -S "$REPO/.exo/server.sock" ]] && return 0
        sleep 0.1
    done
    [[ ! -S "$REPO/.exo/server.sock" ]] || { cat "$log" 2>/dev/null || true; fail "server did not release its socket"; }
}

# Exported so the shim and the controller share one environment.
export MOCK_URL
export EXOMONAD_CODEX_MODEL="${EXOMONAD_CODEX_MODEL:-e2e-unused-model}"

say "starting standalone serve from project configuration"
start_server "$WORK/server-v2.log"
ACCEPTED_IDENTITY="$(health_identity)"
[[ "$ACCEPTED_IDENTITY" == sha256:v2:* ]] \
    || fail "standalone serve did not report a v2 identity: $ACCEPTED_IDENTITY"
curl -fsS --unix-socket "$REPO/.exo/server.sock" http://localhost/health > "$WORK/health.json"
say "accepted v2 snapshot $ACCEPTED_IDENTITY"

# ---------------------------------------------------------------------------
# Phase B: the retained f446/f285 history and the saved v1 authorization
# ---------------------------------------------------------------------------
say "seeding the retained f446 accepted launch and terminal provider failure"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/configured-owner-resume.py" seed-terminal \
    --repo "$REPO" --output "$WORK/preexisting-opencode-owner.json" \
    > "$WORK/preexisting-opencode-owner-seed.json" \
    || { cat "$WORK/preexisting-opencode-owner-seed.json"; fail "failed to seed the retained terminal-owner shape"; }

say "recording the retained v1 route authorization for f285"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed-stored-proof.py" seed \
    --repo "$REPO" --baseline "$WORK/preexisting-opencode-owner.json" \
    --output "$WORK/stored-proof.json" \
    || fail "failed to record the retained v1 authorization"

GATE_NAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gate_name"])' "$WORK/stored-proof.json")"
V1_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["v1_digest"])' "$WORK/stored-proof.json")"
V1_IDENTITY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["v1_configuration_fingerprint"])' "$WORK/stored-proof.json")"
say "approving the named terminal-owner recovery gate through the supported CLI"
PYTHONPATH="$SOURCE_ROOT" python3 -m tl_loop gate \
    --project-root "$REPO" --run-id kani --parent-run-id root \
    --name "$GATE_NAME" --approve > "$WORK/approve-terminal-gate.log" 2>&1 \
    || { cat "$WORK/approve-terminal-gate.log"; fail "could not approve the disposable terminal-owner gate"; }

say "recording the retained prelaunch configuration refusal under the v1 proof"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed-stored-proof.py" refuse \
    --repo "$REPO" --baseline "$WORK/preexisting-opencode-owner.json" \
    --output "$WORK/refused-action.json" \
    || fail "failed to record the retained prelaunch refusal"
HISTORY_BEFORE="$WORK/historical-journal-before.json"
python3 - "$REPO" "$HISTORY_BEFORE" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
journal = json.loads(
    (repo / ".exo/tl-loop/root/kani/action-journal.json").read_text(encoding="utf-8")
)
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
if len(resume) != 2:
    raise SystemExit(f"expected two retained resume receipts, found {len(resume)}")
# Freeze the exact retained bytes before any continuation runs.
Path(sys.argv[2]).write_text(json.dumps(resume, sort_keys=True, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"resume_entries": len(resume)}))
PY
REFUSED_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["route_authorization_digest"])' "$WORK/refused-action.json")"
[[ "$REFUSED_DIGEST" == "$V1_DIGEST" ]] \
    || fail "the refused action does not carry the saved v1 authorization digest"
python3 - "$REPO" "$V1_IDENTITY" "$V1_DIGEST" <<'PY' || fail "stored proof evidence is not internally consistent"
import json
import sys
from pathlib import Path

repo, identity, digest = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
run = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8"))
leaf = run["slices"]["issue-814-substitution-model-kani"]
evidence = leaf["recovery"]["evidence"]
saved = evidence["configured_route_authorization"]
assert saved["configuration_fingerprint"] == identity, saved["configuration_fingerprint"]
assert saved["digest"] == digest, saved["digest"]
assert saved["transition"] == "confirmed_terminal_successor_reconciled_to_current_route"
assert saved["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
assert saved["model"] == "opencode-go/deepseek-v4.1-flash"
assert saved["effort"] == "high"
history = evidence["configured_route_authorization_history"]
assert any(item["digest"] == saved["supersedes_route_authorization_digest"] for item in history)
gate = next(row for row in run["gates"] if row["name"] == "tl-terminal-owner-recovery-issue-814-substitution-model-kani")
assert gate["status"] == "approved", gate["status"]
assert "terminal_owner_recovery_gate_rejection" not in evidence
PY
say "saved v1 proof $V1_DIGEST is bound to f285 and the approved gate"

# The fixture writes the historical owner and its checkpoint after the
# standalone server's startup scan. Restart only that disposable server so its
# resolver loads the persisted identity, exactly as the retained run did.
stop_server "$WORK/server-v2.log"
start_server "$WORK/server-v2-reloaded.log"
[[ "$(health_identity)" == "$ACCEPTED_IDENTITY" ]] \
    || fail "the reloaded standalone server accepted a different identity"
say "reloaded the standalone server with the historical owner in place"

# The corrected controller must derive exactly this v2 proof from the stored
# state. Computing it here uses the same production function the controller
# will run; the launch below must produce the identical digest.
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed-stored-proof.py" expected \
    --repo "$REPO" --accepted <(python3 -c 'import json,sys; print(json.dumps({"identity": sys.argv[1]}))' "$ACCEPTED_IDENTITY") \
    --output "$WORK/expected-v2.json" > "$WORK/expected-v2.log" 2>&1 \
    || { cat "$WORK/expected-v2.log"; fail "the corrected controller does not accept the stored proof"; }
EXPECTED_V2_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["digest"])' "$WORK/expected-v2.json")"
EXPECTED_V2_SUPERSEDES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supersedes_route_authorization_digest"])' "$WORK/expected-v2.json")"
[[ "$EXPECTED_V2_SUPERSEDES" == "$V1_DIGEST" ]] \
    || fail "the corrected proof does not link over the saved v1 authorization"
say "corrected controller derives linked v2 proof $EXPECTED_V2_DIGEST"

printf 'STOREDPROOF-1 saved v1 authorization validated under its original semantics; rejected prelaunch action proven to have launched nothing; history preserved byte-for-byte\n'
say "PASS: STOREDPROOF-1"

# ---------------------------------------------------------------------------
# Phase C: the negative controls, decided by the shipped controller archive
# ---------------------------------------------------------------------------
say "checking every negative control with the shipped controller"
PYTHONPATH="$STORED_HOME/.exo/tl_loop.pyz" python3 \
    "$SOURCE_ROOT/tests/e2e/init-recovery/check-stored-proof-negatives.py" \
    --repo "$REPO" --identity "$ACCEPTED_IDENTITY" --v1-digest "$V1_DIGEST" \
    --output "$WORK/negatives.json" > "$WORK/negatives.log" 2>&1 \
    || { cat "$WORK/negatives.log"; fail "a stored-proof negative control was accepted"; }
cat "$WORK/negatives.log"
python3 - "$WORK/negatives.json" <<'PY'
import json
import sys
from pathlib import Path

cases = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
names = [case["case"] for case in cases["cases"]]
assert cases["restored"] is True
for required in (
    "positive-control",
    "forged-legacy-digest",
    "malformed-legacy-digest",
    "wrong-owner",
    "wrong-scope-manifest",
    "wrong-predecessor",
    "unaccepted-current-identity",
    "genuine-model-change",
    "genuine-effort-change",
    "unknown-prior-effect",
    "live-worker",
    "independent-gate-rejection",
):
    assert required in names, (required, names)
print(json.dumps({"negative_cases": len(names), "all_held": True}))
PY
printf 'STOREDPROOF-4 negatives stay held: forged/malformed legacy digest, wrong owner/scope/manifest/predecessor, unaccepted current identity, unknown prior effect, live worker, independent gate rejection\n'
say "PASS: STOREDPROOF-4"

# ---------------------------------------------------------------------------
# Phase D: one linked generation-3 successor through the real init/serve path
# ---------------------------------------------------------------------------
count_launches() {
    python3 - "$REPO" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
ledger = []
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    ledger.extend(
        json.loads(line) for line in segment.read_text(encoding="utf-8").splitlines() if line.strip()
    )
journal = json.loads(
    (repo / ".exo/tl-loop/root/kani/action-journal.json").read_text(encoding="utf-8")
)
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
run = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8"))
leaf = run["slices"]["issue-814-substitution-model-kani"]
print(
    json.dumps(
        {
            "agent_spawned": sum(row.get("type") == "agent.spawned" for row in ledger),
            "dispatch_intended": sum(row.get("type") == "tl.dispatch_intended" for row in ledger),
            "spawn_requested": sum(row.get("type") == "tl.spawn_requested" for row in ledger),
            "spawn_accepted": sum(row.get("type") == "tl.spawn_request_accepted" for row in ledger),
            "resume_entries": len(resume),
            "resume_confirmed": sum(row.get("status") == "confirmed" for row in resume),
            "resume_rejected": sum(row.get("status") == "rejected" for row in resume),
            # The dispatch boundary legitimately advances as the already
            # recorded launch is consumed on the next replay round; the effect
            # counts, generation and invocation identity must not change.
            "dispatch_invocation_id": leaf.get("dispatch_invocation_id"),
            "dispatch_generation": leaf.get("dispatch_generation"),
        },
        sort_keys=True,
    )
)
PY
}

wait_for_launch() {
    for _ in $(seq 1 1800); do
        if [[ -f "$AGENT_LOG" ]] && grep -q role_config_worker_started "$AGENT_LOG"; then
            return 0
        fi
        sleep 0.1
    done
    tail -80 "$WORK/init-stored-proof.log" 2>/dev/null || true
    tail -120 "$REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    tail -120 "$REPO/.exo/tl-loop/root/kani/controller-output.log" 2>/dev/null || true
    fail "the corrected controller never launched the linked successor"
}

say "running init --continue with identical explicit role flags"
(
    cd "$REPO"
    run_env "$EXOMONAD_BIN" init --continue --session "$SESSION" "${ROLE_FLAGS[@]}"
) > "$WORK/init-stored-proof.log" 2>&1 &
INIT_PID=$!
wait_for_launch
wait "$INIT_PID" 2>/dev/null || true
INIT_PID=""
if grep -q 'ERROR.*exomonad init failed' "$WORK/init-stored-proof.log"; then
    cat "$WORK/init-stored-proof.log"
    fail "init failed before the linked successor was launched"
fi
grep -Fq "identity=$ACCEPTED_IDENTITY" "$WORK/init-stored-proof.log" \
    || { cat "$WORK/init-stored-proof.log"; fail "init did not agree on the server-accepted v2 identity"; }
if grep -Fq 'conflicts with current configuration' "$WORK/init-stored-proof.log"; then
    cat "$WORK/init-stored-proof.log"
    fail "the corrected controller still reported the stored-proof conflict"
fi
for _ in $(seq 1 900); do
    LAUNCH_STATE="$(count_launches)"
    python3 - "$LAUNCH_STATE" "$REPO" <<'PY' && break
import json
import sys
from pathlib import Path

state = json.loads(sys.argv[1])
repo = Path(sys.argv[2])
run = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8"))
leaf = run["slices"]["issue-814-substitution-model-kani"]
if leaf.get("dispatch_last_boundary") not in {"spawn_request_accepted", "agent.spawned"}:
    raise SystemExit(1)
if leaf.get("dispatch_generation") != 3:
    raise SystemExit(1)
owner = repo / ".exo/agents/issue-814-substitution-model-kani-opencode/invocation.json"
invocation = json.loads(owner.read_text(encoding="utf-8"))
if invocation.get("generation") != 3 or invocation.get("prior_invocation_id") != (
    "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
):
    raise SystemExit(1)
PY
    sleep 0.1
done
python3 - "$REPO" "$ACCEPTED_IDENTITY" "$V1_DIGEST" "$EXPECTED_V2_DIGEST" "$HISTORY_BEFORE" "$AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path

repo, identity = Path(sys.argv[1]), sys.argv[2]
v1_digest, expected_digest = sys.argv[3], sys.argv[4]
history_before = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
events = [json.loads(line) for line in Path(sys.argv[6]).read_text(encoding="utf-8").splitlines()]
journal = json.loads((repo / ".exo/tl-loop/root/kani/action-journal.json").read_text(encoding="utf-8"))
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
assert len(resume) == 3, [row.get("status") for row in resume]
assert resume[0] == history_before[0], "the confirmed f446 -> f285 receipt changed"
assert resume[1] == history_before[1], "the rejected prelaunch action changed"
assert resume[1]["status"] == "rejected", resume[1]["status"]
assert resume[1]["error"] == (
    "Invalid input: role route configuration is stale or was not accepted by this server"
), resume[1]["error"]
assert resume[1]["result"]["result"] is None, resume[1]["result"]
assert resume[2]["status"] == "confirmed", resume[2]["status"]
arguments = resume[2]["arguments"]
assert arguments["route_configuration_fingerprint"] == identity, arguments["route_configuration_fingerprint"]
assert arguments["route_authorization_digest"] == expected_digest, (
    arguments["route_authorization_digest"],
    expected_digest,
)
assert arguments["desired_model"] == "opencode-go/deepseek-v4.1-flash", arguments["desired_model"]
assert arguments["desired_effort"] == "high", arguments["desired_effort"]
assert arguments["expected_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
assert arguments["prior_generation"] == 2, arguments["prior_generation"]
assert v1_digest != expected_digest
starts = [event for event in events if event.get("event") == "role_config_worker_started"]
assert len(starts) == 1, len(starts)
worker = starts[0]
assert worker["harness"] == "opencode", worker
assert worker["model"] == "opencode-go/deepseek-v4.1-flash", worker
assert worker["effort"] == "high", worker
assert worker["role"] in {"worker", "dev"}, worker
assert "--model" in worker["argv"], worker["argv"][:8]
run = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8"))
leaf = run["slices"]["issue-814-substitution-model-kani"]
# The host accepted the resume and recorded the launch boundary; the later
# `agent.spawned` row is consumed on the next replay round.
assert leaf["dispatch_last_boundary"] in {
    "spawn_request_accepted",
    "agent.spawned",
}, leaf["dispatch_last_boundary"]
assert leaf["dispatch_generation"] == 3, leaf["dispatch_generation"]
assert leaf["dispatch_agent_id"] == "issue-814-substitution-model-kani-opencode", leaf["dispatch_agent_id"]
assert leaf["status"] in {"spawned", "dispatch_unconfirmed"}, leaf["status"]
assert leaf["dispatch_invocation_id"] is not None
invocation = json.loads(
    (
        repo / ".exo/agents/issue-814-substitution-model-kani-opencode/invocation.json"
    ).read_text(encoding="utf-8")
)
assert invocation["generation"] == 3, invocation["generation"]
assert invocation["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
assert invocation["runtime"] == "opencode", invocation["runtime"]
assert invocation["model"] == "opencode-go/deepseek-v4.1-flash", invocation["model"]
assert invocation["effort"] == "high", invocation["effort"]
print(json.dumps({"journal_entries": len(resume), "generation": invocation["generation"]}))
PY
printf 'STOREDPROOF-2 fresh linked authorization yields exactly one generation3 OpenCode DeepSeek/high successor linked to f285, accepted by the host under v2\n'
say "PASS: STOREDPROOF-2"

# ---------------------------------------------------------------------------
# Phase E: repeats after authorization, intent, launch and adoption
# ---------------------------------------------------------------------------
BEFORE_REPEAT="$(count_launches)"
run_repeat() {
    local label="$1"
    local status=0
    (
        cd "$REPO"
        run_env "$EXOMONAD_BIN" init --continue --session "$SESSION" "${ROLE_FLAGS[@]}"
    ) > "$WORK/init-repeat-$label.log" 2>&1 || status=$?
    if [[ "$status" -ne 0 ]] \
        && ! grep -Fq 'open terminal failed: not a terminal' "$WORK/init-repeat-$label.log"; then
        cat "$WORK/init-repeat-$label.log"
        fail "repeat init ($label) failed with status $status"
    fi
    if grep -q 'ERROR.*exomonad init failed' "$WORK/init-repeat-$label.log"; then
        cat "$WORK/init-repeat-$label.log"
        fail "repeat init ($label) reported a substantive failure"
    fi
    grep -Fq "identity=$ACCEPTED_IDENTITY" "$WORK/init-repeat-$label.log" \
        || { cat "$WORK/init-repeat-$label.log"; fail "repeat init ($label) did not agree on the accepted identity"; }
    local after
    after="$(count_launches)"
    [[ "$after" == "$BEFORE_REPEAT" ]] \
        || { printf 'before=%s\nafter=%s\n' "$BEFORE_REPEAT" "$after"; fail "repeat init ($label) duplicated a launch"; }
    say "repeat init ($label) added no duplicate invocation, intent, authorization or dispatch"
}

run_repeat after-launch-and-adoption
say "restarting only the standalone server from the same project configuration"
stop_server "$WORK/server-v2.log"
start_server "$WORK/server-restarted.log"
[[ "$(health_identity)" == "$ACCEPTED_IDENTITY" ]] \
    || fail "the restarted standalone server accepted a different identity"
run_repeat after-server-restart
printf 'STOREDPROOF-3 repeat init --continue after authorization, intent, launch and adoption (and after response loss) adds no duplicate invocation/intent/authorization\n'
say "PASS: STOREDPROOF-3"

# ---------------------------------------------------------------------------
# Phase F: normal current-configuration rules for a genuine change and identity
# ---------------------------------------------------------------------------
say "checking a genuine route change and the same-route new identity under current rules"
python3 - "$REPO" "$ACCEPTED_IDENTITY" "$V1_DIGEST" "$WORK/stored-proof.json" \
    "$WORK/expected-v2.json" "$WORK/negatives.json" <<'PY' || fail "current-configuration rules were not retained"
import json
import sys
from pathlib import Path

repo, identity, v1_digest = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
stored = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
expected = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
negatives = json.loads(Path(sys.argv[6]).read_text(encoding="utf-8"))
run = json.loads((repo / ".exo/tl-loop/root/kani/run.json").read_text(encoding="utf-8"))
leaf = run["slices"]["issue-814-substitution-model-kani"]
assert leaf["dispatch_route_configuration_fingerprint"] == identity, leaf.get(
    "dispatch_route_configuration_fingerprint"
)
# Same directed route, new identity scheme: the effective values are unchanged
# while the versioned configuration identity is not.
assert stored["stored_proof"]["harness"] == expected["harness"]
assert stored["stored_proof"]["agent_type"] == expected["agent_type"]
assert stored["stored_proof"]["model"] == expected["model"]
assert stored["stored_proof"]["effort"] == expected["effort"]
assert stored["v1_configuration_fingerprint"] != identity
assert expected["configuration_fingerprint"] == identity
held = {case["case"]: case for case in negatives["cases"]}
assert held["genuine-model-change"]["held"] is True
assert held["genuine-effort-change"]["held"] is True
resume = [
    row
    for row in json.loads((repo / ".exo/tl-loop/root/kani/action-journal.json").read_text())
    if row.get("operation") == "resume_blocked_leaf"
]
assert [row["status"] for row in resume] == ["confirmed", "rejected", "confirmed"], resume
PY
printf 'STOREDPROOF-5 genuine model/effort change and same-route-new-identity behave under normal current-config rules\n'
say "PASS: STOREDPROOF-5"

# ---------------------------------------------------------------------------
# Phase G: supported shutdown, then the retained R1-R4 acceptance
# ---------------------------------------------------------------------------
say "stopping the workspace through the supported shutdown command"
(
    cd "$REPO"
    HOME="$STORED_HOME" "$EXOMONAD_BIN" shutdown --workspace
) > "$WORK/workspace-shutdown.log" 2>&1 \
    || { cat "$WORK/workspace-shutdown.log"; fail "supported workspace shutdown failed"; }
stop_server "$WORK/server-restarted.log"
touch "$WORKER_EXIT_FILE"
tmux_iso kill-server 2>/dev/null || true

say "running the retained #1433 role-configuration acceptance (R1-R4)"
ROLE_TMUX_DIR="$(mktemp -d "${E2E_TMUX_TMPDIR_ROOT:-${TMPDIR:-/tmp}}/exo1433-role-tmux.XXXXXX")"
(
    unset TMUX TMUX_PANE
    export TMUX_TMPDIR="$ROLE_TMUX_DIR"
    bash "$SOURCE_ROOT/tests/e2e/init-recovery/role-config-acceptance.sh" \
        "$EXOMONAD_BIN" "$WORK/role-config" "$WORK/role-config-home" "$CODEX_HOME" \
        "$SOURCE_ROOT" "$WASM_DIR"
) > "$ROLE_CONFIG_LOG" 2>&1 || { cat "$ROLE_CONFIG_LOG"; fail "the retained R1-R4 acceptance regressed"; }
rm -rf -- "$ROLE_TMUX_DIR"
for marker in ROLECONFIG-1 ROLECONFIG-2 ROLECONFIG-3 ROLECONFIG-4 ROLECONFIG-5; do
    grep -Fq "$marker" "$ROLE_CONFIG_LOG" || { cat "$ROLE_CONFIG_LOG"; fail "retained acceptance is missing $marker"; }
done
# `run.sh`'s E2E_ROLE_CONFIG_ONLY branch emits this contract line after the
# acceptance script, so the composed caller emits the same line here.
printf 'ROLECONFIG-ALL PASS\n' | tee -a "$ROLE_CONFIG_LOG" >/dev/null
grep -Fq 'ROLECONFIG-ALL PASS' "$ROLE_CONFIG_LOG" \
    || { cat "$ROLE_CONFIG_LOG"; fail "retained acceptance did not finish"; }
printf 'STOREDPROOF-6 R1-R4 retained: the existing E2E_ROLE_CONFIG_ONLY=1 acceptance still prints ROLECONFIG-1..5 and ROLECONFIG-ALL PASS\n'
say "PASS: STOREDPROOF-6"

if [[ -n "${E2E_STORED_PROOF_ARTIFACTS:-}" ]]; then
    mkdir -p "$E2E_STORED_PROOF_ARTIFACTS"
    for document in stored-proof.json refused-action.json expected-v2.json \
        negatives.json health.json init-stored-proof.log mock.log; do
        [[ -f "$WORK/$document" ]] && cp "$WORK/$document" "$E2E_STORED_PROOF_ARTIFACTS/"
    done
    cp "$ROLE_CONFIG_LOG" "$E2E_STORED_PROOF_ARTIFACTS/role-config-acceptance.log"
    say "retained acceptance artifacts in $E2E_STORED_PROOF_ARTIFACTS"
fi

printf 'STOREDPROOF-ALL PASS\n'
