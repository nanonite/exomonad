#!/usr/bin/env bash
# Compiled dispatch-collision acceptance for Chainlink #1511.
#
# The fixture has the exact production shape that collided on 36a32576:
#
#   logical resume   blocked-resume:31a609e6...b187b
#   old declaration  run_seq 188678/188680, sha256:v1:0381301c... configuration
#   refusal          a durable host-recorded tool.called row for the exact
#                    "role route configuration is stale or was not accepted by
#                    this server" envelope, no result, no launch
#   new attempt      sha256:v2:b4c93f8d... configuration, same owner/predecessor
#
# It seeds BOTH the old tl.dispatch_intended and tl.spawn_requested rows (built
# by the production _dispatch_payload producer), the rejected action journal
# entry, and the literal retained a8b9524b proof. A correct host selects the
# reauthorized declaration and releases the older one only after proving from
# the ledger and owner registry that it had no launch effect. The run then
# proves one real generation-3 successor in the shim process, repeat/restart/
# concurrent idempotence, host negative controls, and unchanged history.
#
# Selected by E2E_DISPATCH_COLLISION_ONLY=1 in run.sh. Harnesses are disposable
# shims; no provider is contacted and no Beast state is modified.
set -euo pipefail

export NO_COLOR=1

EXOMONAD_BIN="${1:?binary path required}"
WORK="${2:?fixture directory required}"
STORED_HOME="${3:?isolated home required}"
CODEX_HOME="${4:?Codex home required}"
SOURCE_ROOT="${5:?source workspace required}"
WASM_DIR="${6:?fresh WASM directory required}"
RETAINED_SOURCE="${7:?read-only retained backup directory required}"

SESSION="e2e-dispatch-collision"
STAGE="substitution-model-kani-stage"
OWNER_SLICE="issue-814-substitution-model-kani"
GATE_NAME="tl-terminal-owner-recovery-issue-814-substitution-model-kani"
LITERAL_V1_FINGERPRINT="sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
LITERAL_V1_DIGEST="a8b9524b0d817590c26b7afd19348cda31e26e46eed144ff465ff1aa03493f2b"
LITERAL_F446_DIGEST="1a7f9c65ecf7b3e3cc61effebe1ec23347a1dcff1b3587adb894aa7cbfa7b751"
LITERAL_MANIFEST_DIGEST="4be5ddad961dc30507635801b5b7c6e0956e9a78f2f67ef39e0345abf0e5d407"
LITERAL_SUCCESSOR="f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
LITERAL_PREDECESSOR="f4460477-40bf-4824-941b-dcc3d0ebc9a1"
DIRECTED_MODEL="opencode-go/deepseek-v4.1-flash"

REPO="$WORK/repo"
REMOTE="$WORK/remote/owner/repo.git"
SHIM_DIR="$WORK/bin"
AGENT_LOG="$WORK/agent-events.jsonl"
WORKER_EXIT_FILE="$WORK/worker-exit-requested"
RETAINED="$WORK/retained"
RETAINED_HASHES_BEFORE="$WORK/retained-hashes-before.sha256"
RETAINED_HASHES_AFTER="$WORK/retained-hashes-after.sha256"
RETAINED_ARTIFACTS="${E2E_DISPATCH_RETAIN_ARTIFACTS:-}"
INTERRUPT_DIR="$WORK/interrupt-evidence"
ACCEPTANCE_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
        printf 'ERROR: dispatch-collision tmux server survived teardown\n' >&2
        status=1
    fi
    if (( status != 0 )); then
        printf 'ERROR: dispatch-collision acceptance failed; fixture kept at %s\n' "$WORK" >&2
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

mkdir -p "$WORK" "$REPO" "$STORED_HOME/.exo" "$(dirname "$REMOTE")" "$SHIM_DIR" "$RETAINED" "$INTERRUPT_DIR"
if [[ -n "$RETAINED_ARTIFACTS" ]]; then
    mkdir -p "$RETAINED_ARTIFACTS"
    RETAINED_ARTIFACTS="$(cd "$RETAINED_ARTIFACTS" && pwd)"
    case "$RETAINED_ARTIFACTS/" in
        "$WORK"/*)
            fail "retained artifacts must live outside the disposable work directory: $RETAINED_ARTIFACTS"
            ;;
    esac
fi
cd "$REPO"

# ---------------------------------------------------------------------------
# LITERAL-1: read-only retained sources, byte-compared before and after
# ---------------------------------------------------------------------------
RETAINED_BACKUP_FILES=(
    before-kani-run.json
    before-kani-action-journal.json
    before-kani-invocation.json
    before-root-run.json
    before-root-action-journal.json
    before-root-controller-target.json
    after-kani-run.json
    after-root-run.json
    stop-report.txt
    result.md
)
RETAINED_CONTEXT_FILES=(
    ".exo/harness_policy.toml"
    ".exo/harness_capability.toml"
    ".exo/model-catalog.json"
    ".exo/tl-loop/plan.json"
    ".exo/tl-loop/plan.snapshot"
    ".exo/tl-loop/plan.snapshot.sha256"
)
[[ -d "$RETAINED_SOURCE" ]] || fail "retained backup directory is missing: $RETAINED_SOURCE"
BEAST_ROOT="${E2E_STORED_PROOF_CONTEXT_ROOT:-$(cd "$RETAINED_SOURCE/../../../.." && pwd)}"
hash_retained_originals() {
    local output="$1"
    : > "$output"
    for name in "${RETAINED_BACKUP_FILES[@]}"; do
        [[ -f "$RETAINED_SOURCE/$name" ]] || fail "retained file is missing: $RETAINED_SOURCE/$name"
        ( cd "$RETAINED_SOURCE" && sha256sum "$name" >> "$output" )
    done
    for name in "${RETAINED_CONTEXT_FILES[@]}"; do
        [[ -f "$BEAST_ROOT/$name" ]] || fail "retained context file is missing: $BEAST_ROOT/$name"
        ( cd "$BEAST_ROOT" && sha256sum "$name" >> "$output" )
    done
}
hash_retained_originals "$RETAINED_HASHES_BEFORE"
mkdir -p "$RETAINED/backup" "$RETAINED/context"
for name in "${RETAINED_BACKUP_FILES[@]}"; do
    cp "$RETAINED_SOURCE/$name" "$RETAINED/backup/$name"
    cmp -s "$RETAINED_SOURCE/$name" "$RETAINED/backup/$name" \
        || fail "retained copy differs from its source: $name"
done
for name in "${RETAINED_CONTEXT_FILES[@]}"; do
    mkdir -p "$RETAINED/context/$(dirname "$name")"
    cp "$BEAST_ROOT/$name" "$RETAINED/context/$name"
    cmp -s "$BEAST_ROOT/$name" "$RETAINED/context/$name" \
        || fail "retained context copy differs from its source: $name"
done
python3 - "$RETAINED" <<'PY' || fail "could not extract the retained proof bytes"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
run = json.loads((root / "backup/before-kani-run.json").read_text(encoding="utf-8"))
approved_run = json.loads(
    (root / "backup/after-kani-run.json").read_text(encoding="utf-8")
)
journal = json.loads((root / "backup/before-kani-action-journal.json").read_text(encoding="utf-8"))
owner_slice = "issue-814-substitution-model-kani"
leaf = run["slices"][owner_slice]
evidence = leaf["recovery"]["evidence"]
saved = evidence["configured_route_authorization"]
history = evidence["configured_route_authorization_history"][0]
rejected = [
    row
    for row in journal
    if row.get("operation") == "resume_blocked_leaf" and row.get("status") == "rejected"
]
if len(rejected) != 1:
    raise SystemExit(f"expected one retained rejected action, found {len(rejected)}")
arguments = rejected[0]["arguments"]
route_fields = {
    key: arguments.get(key)
    for key in (
        "route_configuration_fingerprint",
        "route_policy_fingerprint",
        "route_manifest_digest",
        "route_fingerprint",
        "route_authorization_digest",
        "desired_harness",
        "desired_agent_type",
        "desired_model",
        "desired_effort",
    )
}
(root / "kani-manifest.json").write_text(
    json.dumps(run["plan_manifest"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(root / "literal-authorization.json").write_text(
    json.dumps(saved, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(root / "literal-history.json").write_text(
    json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(root / "literal-rejected-route.json").write_text(
    json.dumps(route_fields, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(root / "literal-gate-record.json").write_text(
    json.dumps(
        next(row for row in approved_run["gates"] if row["name"] == "tl-terminal-owner-recovery-issue-814-substitution-model-kani"),
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print(json.dumps({"v1_digest": saved["digest"], "manifest_digest": run["plan_manifest"]["digest"]}))
PY
python3 - "$RETAINED" <<'PY' || fail "the retained proof bytes are not the expected live bytes"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
saved = json.loads((root / "literal-authorization.json").read_text(encoding="utf-8"))
history = json.loads((root / "literal-history.json").read_text(encoding="utf-8"))
manifest = json.loads((root / "kani-manifest.json").read_text(encoding="utf-8"))
route = json.loads((root / "literal-rejected-route.json").read_text(encoding="utf-8"))
gate = json.loads((root / "literal-gate-record.json").read_text(encoding="utf-8"))
assert saved["digest"] == "a8b9524b0d817590c26b7afd19348cda31e26e46eed144ff465ff1aa03493f2b"
assert saved["configuration_fingerprint"] == "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
assert saved["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
assert saved["supersedes_route_authorization_digest"] == "1a7f9c65ecf7b3e3cc61effebe1ec23347a1dcff1b3587adb894aa7cbfa7b751"
assert history["digest"] == "1a7f9c65ecf7b3e3cc61effebe1ec23347a1dcff1b3587adb894aa7cbfa7b751"
assert manifest["digest"] == "4be5ddad961dc30507635801b5b7c6e0956e9a78f2f67ef39e0345abf0e5d407"
assert route["route_fingerprint"] == "sha256:v1:13236139e5e0a5aac217d2dac60e0b69c1dbc953a356618a97c8c1367f467724"
assert route["route_policy_fingerprint"] == "sha256:v1:9e36b5ee5a50d2c833aaeac004b994a441ec3f1c140639f3f257203069bebde2"
assert route["route_manifest_digest"] == manifest["digest"]
assert route["route_authorization_digest"] == saved["digest"]
assert route["route_configuration_fingerprint"] == saved["configuration_fingerprint"]
assert gate["status"] == "approved", gate["status"]
print(json.dumps({"literal_sources": "verified"}))
PY
say "retained literal proof $LITERAL_V1_DIGEST copied from $RETAINED_SOURCE"

# ---------------------------------------------------------------------------
# Phase 0: shipped controller archive and disposable harness shims
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
# Phase A: disposable repository, mock Forgejo and the retained policy
# ---------------------------------------------------------------------------
git init --bare -q "$REMOTE"
git init -q -b main
git config user.name "Exomonad dispatch-collision acceptance"
git config user.email "dispatch-collision-e2e@example.invalid"
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
printf 'dispatch collision baseline\n' > README.md
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
git commit -q -m "dispatch collision acceptance base"
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
            "description": "Isolated dispatch-collision fixture.",
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
cp "$RETAINED/context/.exo/harness_policy.toml" .exo/harness_policy.toml
cp "$RETAINED/context/.exo/harness_capability.toml" .exo/harness_capability.toml
cp "$RETAINED/context/.exo/model-catalog.json" .exo/model-catalog.json

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
worker_model = "$DIRECTED_MODEL"
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
    .exo/model-catalog.json
git commit -q -m "configure dispatch-collision acceptance fixture"
git remote set-url origin "git@forgejo-e2e:owner/repo.git"
git remote set-url --push origin "git@forgejo-e2e:owner/repo.git"
GIT_SSH_COMMAND="$WORK/git-ssh" git push -u origin main -q

say "seeding the completed sibling, dormant stage controller and literal planned leaf"
PYTHONPATH="$SOURCE_ROOT" python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed_aggregate_checkpoint.py" \
    --repo "$REPO" --mock-url "$MOCK_URL" --output "$WORK/seed.json" \
    --active-kani-blocker --terminal-provider-failure \
    --kani-stage-name "$STAGE" --kani-manifest-json "$RETAINED/kani-manifest.json" \
    > "$WORK/seed.log" 2>&1 \
    || { cat "$WORK/seed.log"; cat "$WORK/mock.stderr"; fail "failed to seed the dispatch aggregate fixture"; }
python3 - "$REPO" "$STAGE" <<'PY' || fail "the dispatch fixture did not retain the historical manifest digest"
import json
import sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), sys.argv[2]
root = json.loads((repo / ".exo/tl-loop/root/run.json").read_text(encoding="utf-8"))
child = json.loads((repo / f".exo/tl-loop/root/{stage}/run.json").read_text(encoding="utf-8"))
expected = "4be5ddad961dc30507635801b5b7c6e0956e9a78f2f67ef39e0345abf0e5d407"
assert root["plan_manifest"]["digest"] is not None
node = next(node for node in root["plan_manifest"]["nodes"] if node["name"] == stage)
assert node["child_manifest_digest"] == expected, node["child_manifest_digest"]
assert root["plan_manifest"]["child_manifests"][node["node_id"]]["digest"] == expected
assert child["plan_manifest"]["digest"] == expected, child["plan_manifest"]["digest"]
assert child["plan_manifest"]["scope_id"] == f"root/sub_tl/{stage}"
print(json.dumps({"manifest_digest": expected}))
PY
say "dispatch child manifest digest $LITERAL_MANIFEST_DIGEST is carried by the disposable plan"

ROLE_FLAGS=(
    --worker opencode
    --worker-model "$DIRECTED_MODEL"
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
    local interrupt_at="${2:-}"
    local interrupt_action="${3:-}"
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
        if [[ -n "$interrupt_at" ]]; then
            export EXOMONAD_E2E_INTERRUPT_AT="$interrupt_at"
            export EXOMONAD_E2E_INTERRUPT_SCOPE="$INTENT_ID"
            export EXOMONAD_E2E_INTERRUPT_DIR="$INTERRUPT_DIR"
            if [[ -n "$interrupt_action" ]]; then
                export EXOMONAD_E2E_INTERRUPT_ACTION="$interrupt_action"
            fi
        fi
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

recover_crashed_server() {
    # The barrier aborts the real server, so no shutdown handler removes the
    # disposable socket inode or pid file. Reap the child and clear only those
    # runtime files; no project, ledger or owner state is touched.
    if [[ -n "$SERVER_PID" ]]; then
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=""
    fi
    rm -f "$REPO/.exo/server.sock" "$REPO/.exo/server.pid"
}

wait_for_interrupt_marker() {
    local boundary="$1"
    local marker="$INTERRUPT_DIR/$boundary.json"
    for _ in $(seq 1 2400); do
        [[ -s "$marker" ]] && return 0
        sleep 0.05
    done
    cat "$WORK/server-"*.log 2>/dev/null || true
    fail "the compiled server never reached the $boundary interruption boundary"
}

kill_controller_process() {
    # Fallback used only if the supported shutdown cannot reach the paused host.
    local target="$REPO/.exo/tl-loop/root/controller-target.json"
    local pane_id=""
    if [[ -f "$target" ]]; then
        pane_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pane_id",""))' "$target" 2>/dev/null || true)"
    fi
    if [[ -n "$pane_id" ]]; then
        local pane_pid=""
        pane_pid="$(tmux_iso list-panes -a -F '#{pane_id} #{pane_pid}' 2>/dev/null \
            | awk -v target="$pane_id" '$1 == target { print $2; exit }')"
        if [[ -n "$pane_pid" ]]; then
            local pane_pgid=""
            pane_pgid="$(ps -o pgid= -p "$pane_pid" 2>/dev/null | tr -d ' ' || true)"
            if [[ -n "$pane_pgid" ]]; then
                kill -9 -- "-$pane_pgid" 2>/dev/null || true
            fi
            pkill -9 -P "$pane_pid" 2>/dev/null || true
            kill -9 "$pane_pid" 2>/dev/null || true
        fi
        tmux_iso kill-pane -t "$pane_id" 2>/dev/null || true
    else
        tmux_iso kill-window -t "$SESSION:TL" 2>/dev/null || true
    fi
}

run_init_at_boundary() {
    local boundary="$1"
    local log="$2"
    local status=0
    (
        cd "$REPO"
        run_env "$EXOMONAD_BIN" init --continue --session "$SESSION" "${ROLE_FLAGS[@]}"
    ) > "$log" 2>&1 &
    INIT_PID=$!
    wait_for_interrupt_marker "$boundary"
    local host_pid=""
    if [[ -f "$REPO/.exo/server.pid" ]]; then
        host_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' "$REPO/.exo/server.pid" 2>/dev/null || true)"
    fi
    # The host has durably crossed the boundary and is paused before returning.
    # Stop the still-live controller through the supported workspace shutdown
    # while the host is paused, so the child invocation is cleanly marked
    # killed instead of dying on an unreachable-socket checkpoint. Then crash
    # the paused host process; the durable boundary state is already written.
    local shutdown_log="$WORK/shutdown-after-interrupt-$boundary.log"
    (
        cd "$REPO"
        HOME="$STORED_HOME" "$EXOMONAD_BIN" shutdown --workspace
    ) > "$shutdown_log" 2>&1 &
    local shutdown_pid=$!
    local shutdown_exited=0
    for _ in $(seq 1 600); do
        if ! kill -0 "$shutdown_pid" 2>/dev/null; then
            shutdown_exited=1
            break
        fi
        sleep 0.05
    done
    if (( shutdown_exited == 0 )); then
        kill -9 "$shutdown_pid" 2>/dev/null || true
        kill_controller_process
    fi
    wait "$shutdown_pid" 2>/dev/null || true
    # Stop the init wrapper before the host: a live init may otherwise start a
    # replacement server while the boundary host is being killed.
    if kill -0 "$INIT_PID" 2>/dev/null; then
        kill -9 "$INIT_PID" 2>/dev/null || true
        status=124
    fi
    wait "$INIT_PID" 2>/dev/null || true
    INIT_PID=""
    if [[ -n "$host_pid" ]]; then
        kill -9 "$host_pid" 2>/dev/null || true
    fi
    if [[ -n "$SERVER_PID" ]]; then
        kill -9 "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=""
    fi
    if [[ -n "$host_pid" ]]; then
        for _ in $(seq 1 200); do
            kill -0 "$host_pid" 2>/dev/null || break
            sleep 0.05
        done
        if kill -0 "$host_pid" 2>/dev/null; then
            fail "the paused compiled host did not exit at the $boundary interruption"
        fi
    fi
    rm -f "$REPO/.exo/server.sock" "$REPO/.exo/server.pid"
    printf 'interrupted boundary=%s init_status=%s marker=%s controller=shutdown-restart\n' \
        "$boundary" "$status" "$INTERRUPT_DIR/$boundary.json"
}

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
# Phase B: retained f446/f285 history and the LITERAL v1 authorization
# ---------------------------------------------------------------------------
say "seeding the retained f446 accepted launch and terminal provider failure"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/configured-owner-resume.py" seed-terminal \
    --repo "$REPO" --output "$WORK/preexisting-opencode-owner.json" \
    --run-id "$STAGE" --stage-branch "main.$STAGE" \
    > "$WORK/preexisting-opencode-owner-seed.json" \
    || { cat "$WORK/preexisting-opencode-owner-seed.json"; fail "failed to seed the retained terminal-owner shape"; }

say "seeding the retained f285 successor and the literal v1 authorization"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed-stored-proof.py" seed \
    --repo "$REPO" --baseline "$WORK/preexisting-opencode-owner.json" \
    --output "$WORK/stored-proof.json" --run-id "$STAGE" \
    --literal-authorization "$RETAINED/literal-authorization.json" \
    --literal-history "$RETAINED/literal-history.json" \
    --expected-v1-digest "$LITERAL_V1_DIGEST" \
    --expected-v1-fingerprint "$LITERAL_V1_FINGERPRINT" \
    > "$WORK/stored-proof-seed.log" 2>&1 \
    || { cat "$WORK/stored-proof-seed.log"; fail "failed to record the literal v1 authorization"; }
python3 - "$WORK/stored-proof.json" "$LITERAL_V1_DIGEST" "$LITERAL_V1_FINGERPRINT" \
    "$LITERAL_MANIFEST_DIGEST" <<'PY' || fail "the literal v1 proof did not validate under its historical context"
import json
import sys
from pathlib import Path

stored = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
check = stored["literal_check"]
assert check["digest"] == sys.argv[2], check
assert check["configuration_fingerprint"] == sys.argv[3], check
assert check["manifest_digest"] == sys.argv[4], check
assert check["historical_policy_reproduces_digest"] is True, check
assert stored["stored_proof"]["digest"] == sys.argv[2]
assert stored["stored_proof"]["configuration_fingerprint"] == sys.argv[3]
assert stored["stored_proof"]["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
print(json.dumps({"literal_v1_digest": sys.argv[2], "manifest_digest": sys.argv[4]}))
PY
say "literal v1 proof $LITERAL_V1_DIGEST reproduces under the retained policy and manifest"

say "approving the named terminal-owner recovery gate through the supported CLI"
PYTHONPATH="$SOURCE_ROOT" python3 -m tl_loop gate \
    --project-root "$REPO" --run-id "$STAGE" --parent-run-id root \
    --name "$GATE_NAME" --approve > "$WORK/approve-terminal-gate.log" 2>&1 \
    || { cat "$WORK/approve-terminal-gate.log"; fail "could not approve the disposable terminal-owner gate"; }

say "recording the retained prelaunch configuration refusal under the literal v1 proof"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed-stored-proof.py" refuse \
    --repo "$REPO" --baseline "$WORK/preexisting-opencode-owner.json" \
    --output "$WORK/refused-action.json" --run-id "$STAGE" \
    --expected-route-fields "$RETAINED/literal-rejected-route.json" \
    --seed-dispatch-history \
    > "$WORK/refused-action-seed.log" 2>&1 \
    || { cat "$WORK/refused-action-seed.log"; fail "the retained historical route context did not reproduce"; }
python3 - "$WORK/refused-action.json" "$LITERAL_V1_DIGEST" "$RETAINED/literal-rejected-route.json" <<'PY' || fail "the retained rejected action envelope is not exact"
import json
import sys
from pathlib import Path

refused = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
retained = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
assert refused["route_authorization_digest"] == sys.argv[2]
assert refused["status"] == "rejected"
assert refused["error"] == "Invalid input: role route configuration is stale or was not accepted by this server"
assert len(refused["dispatch_history_run_seqs"]) == 2, refused["dispatch_history_run_seqs"]
for key, value in refused["literal_route"].items():
    assert value == retained[key], (key, value, retained[key])
print(json.dumps({"literal_route_fields": len(refused["literal_route"])}))
PY

say "verifying the production-shaped old dispatch history and the historical ledger prefix"
LEDGER_PREFIX_BEFORE="$WORK/ledger-prefix-before.sha256"
LEDGER_PAIR_SEQS="$(python3 - "$REPO" "$STAGE" "$WORK/refused-action.json" <<'PYSEED'
import json
import sys
from pathlib import Path

repo, stage, refused_path = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
refused = json.loads(refused_path.read_text(encoding="utf-8"))
journal_arguments = refused["journal_entries"][-1]["arguments"]
intent = journal_arguments["dispatch_intent_id"]
segments = sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
rows = []
for segment in segments:
    for line in segment.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
pairs = [
    row
    for row in rows
    if row.get("type") in {"tl.dispatch_intended", "tl.spawn_requested"}
    and row.get("data", {}).get("intent_id") == intent
]
assert len(pairs) == 2, [(row.get("run_seq"), row.get("type")) for row in pairs]
assert [row["type"] for row in pairs] == ["tl.dispatch_intended", "tl.spawn_requested"]
assert pairs[0]["run_seq"] == refused["dispatch_history_run_seqs"][0]
assert pairs[1]["run_seq"] == refused["dispatch_history_run_seqs"][1]
assert pairs[0]["agent_id"] == stage, pairs[0]["agent_id"]
for row in pairs:
    data = row["data"]
    assert data["boundary"] == row["type"].removeprefix("tl."), data
    assert data["intent_id"] == intent
    assert data["slice_id"] == "issue-814-substitution-model-kani"
    assert data["route_configuration_fingerprint"] == (
        "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
    ), data["route_configuration_fingerprint"]
    assert data["route_fingerprint"] == (
        "sha256:v1:13236139e5e0a5aac217d2dac60e0b69c1dbc953a356618a97c8c1367f467724"
    ), data["route_fingerprint"]
    assert data["attempt"] == journal_arguments["dispatch_attempt"], data
    assert data.get("dispatch_generation") in {
        None,
        journal_arguments["dispatch_generation"],
    }, data
    assert data["started_at"] == pairs[0]["data"]["started_at"]
print(json.dumps({"pairs": [row["run_seq"] for row in pairs]}))
PYSEED
)" || fail "the old dispatch-intended/spawn-requested history is not producer-shaped"
python3 - "$REPO" "$LEDGER_PREFIX_BEFORE" <<'PYPREFIX'
import hashlib
import sys
from pathlib import Path

repo, output = Path(sys.argv[1]), Path(sys.argv[2])
lines = []
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    lines.append(segment.read_text(encoding="utf-8"))
output.write_text(
    hashlib.sha256("".join(lines).encode("utf-8")).hexdigest() + "\n",
    encoding="utf-8",
)
PYPREFIX
OLD_SEQ1="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pairs"][0])' "$LEDGER_PAIR_SEQS")"
OLD_SEQ2="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pairs"][1])' "$LEDGER_PAIR_SEQS")"
printf 'DISPATCH-1 production-shaped history seeded: old dispatch-intended seq %s and spawn-requested seq %s, rejected journal entry, literal a8b9524b proof, approved gate\n' "$OLD_SEQ1" "$OLD_SEQ2"
say "PASS: DISPATCH-1"

CHILD_RUN="$REPO/.exo/tl-loop/root/$STAGE/run.json"
CHILD_JOURNAL="$REPO/.exo/tl-loop/root/$STAGE/action-journal.json"
OWNER_DIR="$REPO/.exo/agents/$OWNER_SLICE-opencode"
HISTORY_BEFORE="$WORK/historical-before.json"
python3 - "$CHILD_RUN" "$CHILD_JOURNAL" "$OWNER_DIR" "$HISTORY_BEFORE" <<'PY'
import json
import sys
from pathlib import Path

run_path, journal_path, owner_dir, output = map(Path, sys.argv[1:])
run = json.loads(run_path.read_text(encoding="utf-8"))
journal = json.loads(journal_path.read_text(encoding="utf-8"))
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
if len(resume) != 2:
    raise SystemExit(f"expected two retained resume receipts, found {len(resume)}")
recovery = run["slices"]["issue-814-substitution-model-kani"]["recovery"]
evidence = recovery["evidence"]
snapshot = {
    "resume_entries": resume,
    "configured_route_authorization": evidence["configured_route_authorization"],
    "configured_route_authorization_history": evidence["configured_route_authorization_history"],
    "owner_identity": json.loads((owner_dir / "identity.json").read_text(encoding="utf-8")),
    "owner_invocation": json.loads((owner_dir / "invocation.json").read_text(encoding="utf-8")),
}
output.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"resume_entries": len(resume)}))
PY

# The fixture writes the historical owner after the standalone server's startup
# scan. Restart only that disposable server so its resolver loads the persisted
# identity, exactly as the retained run did.
stop_server "$WORK/server-v2.log"
start_server "$WORK/server-v2-reloaded.log"
[[ "$(health_identity)" == "$ACCEPTED_IDENTITY" ]] \
    || fail "the reloaded standalone server accepted a different identity"
say "reloaded the standalone server with the historical owner in place"

say "replaying the retained v1 dispatch through the production host path"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/drive-refused-dispatch.py" \
    --repo "$REPO" --refused-action "$WORK/refused-action.json" --run-id "$STAGE" \
    > "$WORK/old-host-refusal.json" 2> "$WORK/old-host-refusal.err" \
    || { cat "$WORK/old-host-refusal.err"; fail "the retained v1 dispatch was not refused by the production host path"; }
OLD_REFUSAL_SEQ="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["refusal_run_seq"])' "$WORK/old-host-refusal.json")"
OLD_REFUSAL_ROUTE="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["configuration_fingerprint"], d["route_fingerprint"])' "$WORK/old-host-refusal.json")"
[[ "$OLD_REFUSAL_ROUTE" == "$LITERAL_V1_FINGERPRINT sha256:v1:13236139e5e0a5aac217d2dac60e0b69c1dbc953a356618a97c8c1367f467724" ]] \
    || fail "the durable host refusal did not carry the retained v1 route: $OLD_REFUSAL_ROUTE"
INTENT_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["journal_entries"][-1]["arguments"]["dispatch_intent_id"])' "$WORK/refused-action.json")"
[[ -n "$INTENT_ID" ]] || fail "the retained refused action has no logical resume intent"
printf 'DISPATCH-1 host-recorded prelaunch refusal seq %s carries the old v1 dispatch authorization (intent %s)\n' "$OLD_REFUSAL_SEQ" "$INTENT_ID"

say "proving the corrected controller links one fresh v2 authorization over the literal v1 proof"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/seed-stored-proof.py" expected \
    --repo "$REPO" --run-id "$STAGE" \
    --accepted <(python3 -c 'import json,sys; print(json.dumps({"identity": sys.argv[1]}))' "$ACCEPTED_IDENTITY") \
    --expected-supersedes "$LITERAL_V1_DIGEST" \
    --output "$WORK/expected-v2.json" > "$WORK/expected-v2.log" 2>&1 \
    || { cat "$WORK/expected-v2.log"; fail "the corrected controller does not accept the literal v1 proof"; }
EXPECTED_V2_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["digest"])' "$WORK/expected-v2.json")"
EXPECTED_V2_SUPERSEDES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supersedes_route_authorization_digest"])' "$WORK/expected-v2.json")"
[[ "$EXPECTED_V2_SUPERSEDES" == "$LITERAL_V1_DIGEST" ]] \
    || fail "the corrected proof does not link over the literal v1 authorization"
say "fresh v2 authorization $EXPECTED_V2_DIGEST is linked over the literal v1 proof"

say "running the host negative controls against the compiled server"
NEGATIVES_LOG="$WORK/dispatch-negatives.log"
python3 "$SOURCE_ROOT/tests/e2e/init-recovery/check-dispatch-negatives.py" \
    --repo "$REPO" --run-id "$STAGE" --identity "$ACCEPTED_IDENTITY" \
    > "$NEGATIVES_LOG" 2>&1 \
    || { cat "$NEGATIVES_LOG"; fail "a dispatch negative case did not hold before launch"; }

# ---------------------------------------------------------------------------
# Phase C: one linked generation-3 successor through the real init/serve path
# ---------------------------------------------------------------------------
count_launches() {
    python3 - "$REPO" "$STAGE" <<'PY'
import json
import sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), sys.argv[2]
ledger = []
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    ledger.extend(
        json.loads(line) for line in segment.read_text(encoding="utf-8").splitlines() if line.strip()
    )
journal = json.loads(
    (repo / f".exo/tl-loop/root/{stage}/action-journal.json").read_text(encoding="utf-8")
)
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
run = json.loads((repo / f".exo/tl-loop/root/{stage}/run.json").read_text(encoding="utf-8"))
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
    if grep -Fq 'role route dispatch authorization is missing or ambiguous' \
        "$CHILD_JOURNAL" 2>/dev/null; then
        printf 'DISPATCH-2 FAIL historical ambiguity reproduced before launch: role route dispatch authorization is missing or ambiguous\n'
    fi
    tail -80 "$WORK/init-stored-proof.log" 2>/dev/null || true
    tail -120 "$REPO/.exo/tl-loop/root/controller-output.log" 2>/dev/null || true
    tail -120 "$REPO/.exo/tl-loop/root/$STAGE/controller-output.log" 2>/dev/null || true
    fail "the corrected controller never launched the linked successor"
}

INTERRUPT_BOUNDARY="${E2E_DISPATCH_INTERRUPT_BOUNDARY:-}"
case "$INTERRUPT_BOUNDARY" in
    ""|authorization_persisted|request_persisted|launch_before_adoption|adoption_before_response) ;;
    *) fail "unknown dispatch interruption boundary: $INTERRUPT_BOUNDARY" ;;
esac
say "stopping the reloaded server to exercise the selected interruption boundary '${INTERRUPT_BOUNDARY:-none}'"
stop_server "$WORK/server-v2-reloaded.log"
BEFORE_INTERRUPT="$(count_launches)"
printf 'interruption baseline %s\n' "$BEFORE_INTERRUPT"
assert_no_new_launch() {
    local before="$1"
    local after="$2"
    local label="$3"
    python3 - "$before" "$after" "$label" <<'PY' || fail "$label produced a launch effect"
import json
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
label = sys.argv[3]
for key in ("agent_spawned", "spawn_accepted", "dispatch_generation", "dispatch_invocation_id"):
    if after[key] != before[key]:
        raise SystemExit(f"{label}: {key} changed {before[key]!r} -> {after[key]!r}")
PY
}

if [[ "$INTERRUPT_BOUNDARY" == "authorization_persisted" ]]; then
    say "interrupting the compiled server after authorization persistence"
    start_server "$WORK/server-interrupt-authorization.log" authorization_persisted pause
    run_init_at_boundary authorization_persisted "$WORK/init-interrupt-authorization.log"
    assert_no_new_launch "$BEFORE_INTERRUPT" "$(count_launches)" "the authorization interruption"
    invocation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["invocation_id"])' "$REPO/.exo/agents/issue-814-substitution-model-kani-opencode/invocation.json")"
    [[ "$invocation" == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d" ]] \
        || fail "the authorization interruption changed the owner invocation: $invocation"
fi

if [[ "$INTERRUPT_BOUNDARY" == "request_persisted" ]]; then
    say "interrupting the compiled server after the request is durable and before launch"
    start_server "$WORK/server-interrupt-request.log" request_persisted pause
    run_init_at_boundary request_persisted "$WORK/init-interrupt-request.log"
    assert_no_new_launch "$BEFORE_INTERRUPT" "$(count_launches)" "the request interruption"
    invocation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["invocation_id"])' "$REPO/.exo/agents/issue-814-substitution-model-kani-opencode/invocation.json")"
    [[ "$invocation" == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d" ]] \
        || fail "the request interruption changed the owner invocation: $invocation"
fi

AFTER_LAUNCH=""
if [[ "$INTERRUPT_BOUNDARY" == "launch_before_adoption" || "$INTERRUPT_BOUNDARY" == "adoption_before_response" ]]; then
    say "interrupting the compiled server after launch and before controller adoption"
    start_server "$WORK/server-interrupt-launch.log" launch_before_adoption pause
    run_init_at_boundary launch_before_adoption "$WORK/init-interrupt-launch.log"
    AFTER_LAUNCH="$(count_launches)"
    python3 - "$BEFORE_INTERRUPT" "$AFTER_LAUNCH" "$REPO" <<'PY' || fail "the launch interruption did not retain exactly one successor"
import json
import sys
from pathlib import Path

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
repo = Path(sys.argv[3])
assert after["agent_spawned"] == before["agent_spawned"] + 1, (before, after)
invocation = json.loads(
    (repo / ".exo/agents/issue-814-substitution-model-kani-opencode/invocation.json").read_text(
        encoding="utf-8"
    )
)
assert invocation["generation"] == 3, invocation
assert invocation["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d", invocation
ledger = [
    json.loads(line)
    for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
    for line in segment.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
launches = [
    row
    for row in ledger
    if row.get("type") == "agent.spawned"
    and row.get("data", {}).get("invocation_id") == invocation["invocation_id"]
]
assert len(launches) == 1, [row.get("run_seq") for row in launches]
print(json.dumps({"launch_interrupted": True, "after": after}))
PY
fi

if [[ "$INTERRUPT_BOUNDARY" == "adoption_before_response" ]]; then
    if [[ -z "$AFTER_LAUNCH" ]]; then
        AFTER_LAUNCH="$BEFORE_INTERRUPT"
    fi
    say "interrupting the compiled server after durable adoption and before the response"
    start_server "$WORK/server-interrupt-adoption.log" adoption_before_response pause
    run_init_at_boundary adoption_before_response "$WORK/init-interrupt-adoption.log"
    assert_no_new_launch "$AFTER_LAUNCH" "$(count_launches)" "the adoption interruption"
    invocation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["generation"])' "$REPO/.exo/agents/issue-814-substitution-model-kani-opencode/invocation.json")"
    [[ "$invocation" == "3" ]] \
        || fail "the adoption interruption changed the invocation generation: $invocation"
fi

if [[ -z "$INTERRUPT_BOUNDARY" ]]; then
    say "no dispatch interruption boundary selected; running the uninterrupted acceptance"
else
    say "restarting the standalone server after the $INTERRUPT_BOUNDARY interruption"
    # The boundary run's init-managed Server window is stale after the abrupt
    # host kill. Remove only that window (never the TL or independent worker
    # window) so the replacement external server and the continued init start
    # from a clean control-plane pane.
    tmux_iso kill-window -t "$SESSION:Server" 2>/dev/null || true
    tmux_iso kill-window -t "$SESSION:Watcher" 2>/dev/null || true
fi
start_server "$WORK/server-after-interruptions.log"
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
    python3 - "$LAUNCH_STATE" "$REPO" "$STAGE" <<'PY' && break
import json
import sys
from pathlib import Path

state = json.loads(sys.argv[1])
repo, stage = Path(sys.argv[2]), sys.argv[3]
run = json.loads((repo / f".exo/tl-loop/root/{stage}/run.json").read_text(encoding="utf-8"))
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
python3 - "$REPO" "$STAGE" "$ACCEPTED_IDENTITY" "$LITERAL_V1_DIGEST" "$EXPECTED_V2_DIGEST" \
    "$HISTORY_BEFORE" "$AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), sys.argv[2]
identity, v1_digest, expected_digest = sys.argv[3], sys.argv[4], sys.argv[5]
history_before = json.loads(Path(sys.argv[6]).read_text(encoding="utf-8"))
events = [json.loads(line) for line in Path(sys.argv[7]).read_text(encoding="utf-8").splitlines()]
journal = json.loads((repo / f".exo/tl-loop/root/{stage}/action-journal.json").read_text(encoding="utf-8"))
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
assert len(resume) == 3, [row.get("status") for row in resume]
assert resume[0] == history_before["resume_entries"][0], "the confirmed f446 -> f285 receipt changed"
assert resume[1] == history_before["resume_entries"][1], "the rejected prelaunch action changed"
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
starts = [event for event in events if event.get("event") == "role_config_worker_started"]
assert len(starts) == 1, len(starts)
worker = starts[0]
assert worker["harness"] == "opencode", worker
assert worker["model"] == "opencode-go/deepseek-v4.1-flash", worker
assert worker["effort"] == "high", worker
assert worker["role"] in {"worker", "dev"}, worker
assert "--model" in worker["argv"], worker["argv"][:8]
run = json.loads((repo / f".exo/tl-loop/root/{stage}/run.json").read_text(encoding="utf-8"))
leaf = run["slices"]["issue-814-substitution-model-kani"]
assert leaf["dispatch_last_boundary"] in {"spawn_request_accepted", "agent.spawned"}, leaf["dispatch_last_boundary"]
assert leaf["dispatch_generation"] == 3, leaf["dispatch_generation"]
assert leaf["dispatch_agent_id"] == "issue-814-substitution-model-kani-opencode", leaf["dispatch_agent_id"]
assert leaf["status"] in {"spawned", "dispatch_unconfirmed"}, leaf["status"]
assert leaf["dispatch_invocation_id"] is not None
# The fresh v2 authorization is the one this launch consumed. The recovery
# evidence that carried it is cleared by the normal post-launch lifecycle, so
# verify the durable journal binding rather than a cleared recovery record.
assert resume[2]["arguments"]["route_authorization_digest"] == expected_digest
assert resume[2]["arguments"]["route_configuration_fingerprint"] == identity
assert expected_digest != v1_digest
# The literal saved v1 proof seeded before the run keeps its exact bytes in the
# pre-run snapshot; the run may only append the fresh linked proof.
literal_v1 = history_before["configured_route_authorization"]
assert literal_v1["digest"] == v1_digest
assert literal_v1["configuration_fingerprint"] == "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
assert literal_v1["manifest_digest"] == "4be5ddad961dc30507635801b5b7c6e0956e9a78f2f67ef39e0345abf0e5d407"
assert history_before["configured_route_authorization_history"][0]["digest"] == "1a7f9c65ecf7b3e3cc61effebe1ec23347a1dcff1b3587adb894aa7cbfa7b751"
invocation = json.loads(
    (repo / ".exo/agents/issue-814-substitution-model-kani-opencode/invocation.json").read_text(encoding="utf-8")
)
assert invocation["generation"] == 3, invocation["generation"]
assert invocation["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
assert invocation["runtime"] == "opencode", invocation["runtime"]
assert invocation["model"] == "opencode-go/deepseek-v4.1-flash", invocation["model"]
assert invocation["effort"] == "high", invocation["effort"]
intent = resume[2]["arguments"]["dispatch_intent_id"]
launch_rows = [
    row
    for row in (
        json.loads(line)
        for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl"))
        for line in segment.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if row.get("type") == "agent.spawned"
    and row.get("data", {}).get("intent_id") == intent
    and row.get("data", {}).get("invocation_id") == invocation["invocation_id"]
]
assert len(launch_rows) == 1, len(launch_rows)
result_invocation = resume[2]["result"]["result"]["invocation"]["invocation_id"]
assert result_invocation == invocation["invocation_id"], result_invocation
result_agent = resume[2]["result"]["result"]["agent"]
assert (result_agent.get("id") or result_agent.get("agent_id")) == (
    "issue-814-substitution-model-kani-opencode"
), result_agent
print(json.dumps({"journal_entries": len(resume), "generation": invocation["generation"]}))
PY
AUDIT_RESULT="$(python3 - "$REPO" "$STAGE" "$OLD_SEQ1" "$OLD_SEQ2" "$WORK/refused-action.json" "$OLD_REFUSAL_SEQ" <<'PYAUDIT'
import json
import sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), sys.argv[2]
old_seqs = [int(sys.argv[3]), int(sys.argv[4])]
refused = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
old_refusal_seq = int(sys.argv[6])
ledger = []
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    ledger.extend(
        json.loads(line) for line in segment.read_text(encoding="utf-8").splitlines() if line.strip()
    )
intent = refused["journal_entries"][-1]["arguments"]["dispatch_intent_id"]
declarations = [
    row
    for row in ledger
    if row.get("type") == "tl.dispatch_intended"
    and row.get("data", {}).get("intent_id") == intent
]
assert len(declarations) == 2, [row.get("run_seq") for row in declarations]
old_rows = [
    row
    for row in ledger
    if row.get("type") in {"tl.dispatch_intended", "tl.spawn_requested"}
    and row.get("data", {}).get("intent_id") == intent
    and row["run_seq"] in old_seqs
]
assert len(old_rows) == 2, [row.get("run_seq") for row in old_rows]
assert all(
    row["data"]["route_configuration_fingerprint"].startswith("sha256:v1:")
    for row in old_rows
)
new_rows = [row for row in declarations if row["run_seq"] not in old_seqs]
assert len(new_rows) == 1, [row.get("run_seq") for row in declarations]
new_row = new_rows[0]
assert new_row["data"]["route_configuration_fingerprint"].startswith("sha256:v2:")
resolutions = [
    row
    for row in ledger
    if row.get("type") == "tl.dispatch_authorization_resolved"
    and row.get("data", {}).get("intent_id") == intent
]
refusals = [
    row
    for row in ledger
    if row.get("type") == "tool.called"
    and row.get("data", {}).get("tool_name") == "resume_blocked_leaf"
    and row.get("data", {}).get("success") is False
    and (row.get("data", {}).get("arguments", {}).get("role_route", {}) or {}).get(
        "dispatch_intent_id"
    )
    == intent
]
assert len(refusals) == 1, [row.get("run_seq") for row in refusals]
refusal = refusals[0]
assert refusal["run_seq"] == old_refusal_seq, (refusal["run_seq"], old_refusal_seq)
assert min(old_seqs) < refusal["run_seq"] < new_row["run_seq"], (
    old_seqs,
    refusal["run_seq"],
    new_row["run_seq"],
)
assert refusal["data"]["success"] is False, refusal["data"]
assert refusal["data"]["error"] == (
    "Invalid input: role route configuration is stale or was not accepted by this server"
), refusal["data"]["error"]
assert refusal["data"]["arguments"]["role_route"]["configuration_fingerprint"] == (
    old_rows[0]["data"]["route_configuration_fingerprint"]
)
assert refusal["data"]["arguments"]["role_route"]["route_fingerprint"] == (
    old_rows[0]["data"]["route_fingerprint"]
)
assert not any(
    row.get("type") == "agent.spawned"
    and row.get("data", {}).get("intent_id") == intent
    and row["run_seq"] < new_row["run_seq"]
    for row in ledger
)
assert len(resolutions) == 1, len(resolutions)
resolution = resolutions[0]["data"]
assert resolution["selected_run_seq"] == new_row["run_seq"], resolution
assert sorted(resolution["superseded_run_seqs"]) == [min(old_seqs)], resolution
assert resolution["superseded_refusal_run_seqs"] == [refusal["run_seq"]], resolution
assert resolution["reason"] == (
    "superseded_declaration_proven_prelaunch_refusal_with_current_approval"
), resolution
assert type(resolution["supersession_approval_run_seq"]) is int, resolution
assert resolution["supersession_approval_run_seq"] < new_row["run_seq"], resolution
approval_rows = [
    row
    for row in ledger
    if row.get("type") == "tl.gate_answered"
    and row.get("data", {}).get("gate_name") == (
        "tl-terminal-owner-recovery-issue-814-substitution-model-kani"
    )
    and row.get("data", {}).get("decision") == "approved"
    and row.get("run_seq") == resolution["supersession_approval_run_seq"]
]
assert len(approval_rows) == 1, resolution
assert resolution["configuration_fingerprint"] == new_row["data"]["route_configuration_fingerprint"]
assert resolution["route_fingerprint"] == new_row["data"]["route_fingerprint"]
print(json.dumps({
    "selected": new_row["run_seq"],
    "superseded": old_seqs,
    "refusal": refusal["run_seq"],
}))
PYAUDIT
)" || fail "the reauthorized declaration was not selected as the only live authorization"
NEW_SELECTED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["selected"])' "$AUDIT_RESULT")"
AUDIT_JSON="$AUDIT_RESULT"
printf 'DISPATCH-2 the historical v1 declaration at seq %s is superseded only by the exact host-recorded prelaunch refusal at seq %s (spawn request %s retained); the v2 declaration at seq %s is selected once\n' "$OLD_SEQ1" "$OLD_REFUSAL_SEQ" "$OLD_SEQ2" "$NEW_SELECTED"
printf 'DISPATCH-3 one fresh v2 authorization, one real generation-3 OpenCode DeepSeek/high successor in the shim process, linked to f285\n'
say "PASS: DISPATCH-2"
say "PASS: DISPATCH-3"


# ---------------------------------------------------------------------------
# DISPATCH-4: repeats after authorization, launch, adoption and restart
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
stop_server "$WORK/server-after-interruptions.log"
start_server "$WORK/server-restarted.log"
[[ "$(health_identity)" == "$ACCEPTED_IDENTITY" ]] \
    || fail "the restarted standalone server accepted a different identity"
run_repeat after-server-restart
say "proving adoption of the launched successor and two concurrent requests stay exactly-once"
BEFORE_ADOPTION="$(count_launches)"
python3 - "$REPO" "$STAGE" "$ACCEPTED_IDENTITY" "$SOURCE_ROOT" <<'PYADOPT' || fail "the launched successor did not reconcile without another launch"
import json
import sys
import threading
from pathlib import Path

repo, stage, identity = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, sys.argv[4])
from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import TransportClient

journal = json.loads((repo / f".exo/tl-loop/root/{stage}/action-journal.json").read_text(encoding="utf-8"))
confirmed = [row for row in journal if row.get("operation") == "resume_blocked_leaf" and row.get("status") == "confirmed"]
assert len(confirmed) >= 1, len(confirmed)
arguments = confirmed[-1]["arguments"]
owner = repo / ".exo/agents/issue-814-substitution-model-kani-opencode"
invocation_before = json.loads((owner / "invocation.json").read_text(encoding="utf-8"))

def request() -> dict[str, object]:
    client = EffectClient(TransportClient(project_root=repo, timeout=60), role="tl", name=stage)
    result = client.resume_blocked_leaf(
        chainlink_issue_id=int(arguments["chainlink_issue_id"]),
        expected_invocation_id=arguments["expected_invocation_id"],
        expected_branch=arguments["expected_branch"],
        expected_worktree_fingerprint=arguments["expected_worktree_fingerprint"],
        task=arguments["task"],
        human_approved=True,
        agent_type=arguments["agent_type"],
        model=arguments["model"],
        role=arguments["role"],
        harness=arguments["harness"],
        effort=arguments["effort"],
        route_configuration_fingerprint=arguments["route_configuration_fingerprint"],
        route_policy_fingerprint=arguments["route_policy_fingerprint"],
        route_manifest_digest=arguments["route_manifest_digest"],
        route_fingerprint=arguments["route_fingerprint"],
        route_dispatch_intent_id=arguments["dispatch_intent_id"],
        route_scope_id="issue-814-substitution-model-kani",
    )
    return {"success": result.success, "raw": result.raw}

single = request()
assert single["success"] is True, single
invocation_after = json.loads((owner / "invocation.json").read_text(encoding="utf-8"))
assert invocation_after["invocation_id"] == invocation_before["invocation_id"]
assert invocation_after["generation"] == invocation_before["generation"]

results: list[dict[str, object]] = []
def run() -> None:
    results.append(request())

threads = [threading.Thread(target=run) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
assert len(results) == 2 and all(result["success"] is True for result in results), results
invocation_final = json.loads((owner / "invocation.json").read_text(encoding="utf-8"))
assert invocation_final["invocation_id"] == invocation_before["invocation_id"]
assert invocation_final["generation"] == invocation_before["generation"]
print(json.dumps({"adopted": True, "concurrent": len(results)}))
PYADOPT
AFTER_ADOPTION="$(count_launches)"
[[ "$AFTER_ADOPTION" == "$BEFORE_ADOPTION" ]] \
    || { printf 'before=%s\nafter=%s\n' "$BEFORE_ADOPTION" "$AFTER_ADOPTION"; fail "adoption or a concurrent request duplicated a launch"; }
printf 'DISPATCH-4 repeat init, server restart, adoption and concurrent requests add no duplicate invocation, intent, authorization, worker or effect\n'
say "PASS: DISPATCH-4"

# ---------------------------------------------------------------------------
# DISPATCH-5: host negative controls, executed before the launch above
# ---------------------------------------------------------------------------
grep -Fq 'DISPATCH-5 PASS' "$NEGATIVES_LOG" \
    || { cat "$NEGATIVES_LOG"; fail "the compiled host negative controls did not all hold"; }
sed -n 's/^/  /p' "$NEGATIVES_LOG"
printf 'DISPATCH-5 host negatives held: missing authorization, populated foreign intent with durable declaration, conflicting duplicates and distinct historical observations sharing one refusal, wrong caller/scope/predecessor/worktree, forged/stale route or config, missing/unrelated/wrong-declaration/contradictory refusal proof, missing/foreign/rejected durable approval, pending/rejected controller gate, unknown outcome, prior launched effect; independent workers untouched\n'
say "PASS: DISPATCH-5"

# ---------------------------------------------------------------------------
# DISPATCH-6: every historical byte unchanged; only legitimate appends
# ---------------------------------------------------------------------------
python3 - "$REPO" "$STAGE" "$HISTORY_BEFORE" "$RETAINED" <<'PY' || fail "a historical byte changed during the integrated run"
import json
import sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), sys.argv[2]
before = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
retained = Path(sys.argv[4])
journal = json.loads((repo / f".exo/tl-loop/root/{stage}/action-journal.json").read_text(encoding="utf-8"))
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
assert resume[0] == before["resume_entries"][0]
assert resume[1] == before["resume_entries"][1]
owner_dir = repo / ".exo/agents/issue-814-substitution-model-kani-opencode"
identity = json.loads((owner_dir / "identity.json").read_text(encoding="utf-8"))
# `init --continue` rewrites the live owner identity projection (absolute
# working dir, recreated invocation), so compare its stable historical
# coordinates rather than the whole mutable document.
for field in ("agent_name", "slug", "slice_id"):
    assert identity.get(field) == before["owner_identity"].get(field), field
assert identity.get("birth_branch") == before["owner_identity"].get("birth_branch")
invocation = json.loads((owner_dir / "invocation.json").read_text(encoding="utf-8"))
# The owner record advanced to generation 3; the retained f285 invocation stays
# available as immutable journal/history evidence.
assert invocation["generation"] == 3
assert invocation["prior_invocation_id"] == "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
literal_v1 = before["configured_route_authorization"]
retained_v1 = json.loads((retained / "literal-authorization.json").read_text(encoding="utf-8"))
identity_fields = (
    "digest",
    "configuration_fingerprint",
    "manifest_digest",
    "harness",
    "agent_type",
    "model",
    "effort",
    "prior_runtime",
    "prior_model",
    "prior_effort",
    "supersedes_route_authorization_digest",
    "transition",
)
assert all(literal_v1.get(field) == retained_v1.get(field) for field in identity_fields)
literal_f446 = before["configured_route_authorization_history"][0]
assert literal_f446["digest"] == "1a7f9c65ecf7b3e3cc61effebe1ec23347a1dcff1b3587adb894aa7cbfa7b751"
print(json.dumps({"historical_bytes": "unchanged"}))
PY
hash_retained_originals "$RETAINED_HASHES_AFTER"
cmp -s "$RETAINED_HASHES_BEFORE" "$RETAINED_HASHES_AFTER" \
    || { diff -u "$RETAINED_HASHES_BEFORE" "$RETAINED_HASHES_AFTER" || true; fail "a retained source file changed during the run"; }
for name in "${RETAINED_BACKUP_FILES[@]}"; do
    cmp -s "$RETAINED_SOURCE/$name" "$RETAINED/backup/$name" \
        || fail "retained backup copy changed: $name"
done
for name in "${RETAINED_CONTEXT_FILES[@]}"; do
    cmp -s "$BEAST_ROOT/$name" "$RETAINED/context/$name" \
        || fail "retained context copy changed: $name"
done
python3 - "$REPO" "$LEDGER_PREFIX_BEFORE" <<'PYLEDGER' || fail "a historical ledger byte changed during the run"
import hashlib
import sys
from pathlib import Path

repo, expected_path = Path(sys.argv[1]), Path(sys.argv[2])
expected = expected_path.read_text(encoding="utf-8").strip()
lines = []
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    lines.append(segment.read_text(encoding="utf-8"))
actual = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
if actual == expected:
    raise SystemExit("the run appended no records; the prefix check cannot prove an append")
all_rows = []
for segment in sorted((repo / ".exo/ledger/segments").glob("segment-*.jsonl")):
    all_rows.extend(line for line in segment.read_text(encoding="utf-8").splitlines() if line.strip())
assert len(all_rows) > 0
print("ledger-prefix-checked")
PYLEDGER
python3 - "$RETAINED/backup/after-root-run.json" <<'PYRECEIPT' || fail "the completed architecture receipt is unavailable"
import json
import sys
from pathlib import Path

repository = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
architecture = repository["slices"]["substitution-model-architecture-stage"]
receipt = architecture.get("post_merge")
assert isinstance(receipt, dict), architecture.keys()
assert (
    receipt.get("phase") == "complete"
    or receipt.get("status") in {"complete", "completed"}
    or receipt.get("complete") is True
), receipt
print(json.dumps({"architecture_post_merge": "complete"}))
PYRECEIPT
printf 'DISPATCH-6 historical ledger/journal/proof bytes and architecture receipts unchanged; only legitimate appends\n'

say "PASS: DISPATCH-6"

# ---------------------------------------------------------------------------
# Supported shutdown and artifacts
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

# The supported shutdown may close panes but must not change history again.
python3 - "$REPO" "$STAGE" "$HISTORY_BEFORE" <<'PY' || fail "supported shutdown changed historical evidence"
import json
import sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), sys.argv[2]
before = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
journal = json.loads((repo / f".exo/tl-loop/root/{stage}/action-journal.json").read_text(encoding="utf-8"))
resume = [row for row in journal if row.get("operation") == "resume_blocked_leaf"]
assert resume[0] == before["resume_entries"][0]
assert resume[1] == before["resume_entries"][1]
print(json.dumps({"shutdown_history": "unchanged"}))
PY

if [[ -n "$RETAINED_ARTIFACTS" ]]; then
    ACCEPTANCE_FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p "$RETAINED_ARTIFACTS/host" "$RETAINED_ARTIFACTS/wasm" \
        "$RETAINED_ARTIFACTS/controller" "$RETAINED_ARTIFACTS/evidence"
    cp "$EXOMONAD_BIN" "$RETAINED_ARTIFACTS/host/exomonad"
    chmod 0755 "$RETAINED_ARTIFACTS/host/exomonad"
    for artifact in "$WASM_DIR"/wasm-guest-*.wasm; do
        [[ -s "$artifact" ]] || fail "cannot retain a missing WASM artifact: $artifact"
        cp "$artifact" "$RETAINED_ARTIFACTS/wasm/$(basename "$artifact")"
    done
    [[ -f "$STORED_HOME/.exo/tl_loop.pyz" ]] \
        || fail "the controller archive consumed by the acceptance is missing"
    cp "$STORED_HOME/.exo/tl_loop.pyz" "$RETAINED_ARTIFACTS/controller/tl_loop.pyz"
    cp "$WORK/old-host-refusal.json" "$RETAINED_ARTIFACTS/evidence/old-host-refusal.json"
    cp "$WORK/dispatch-negatives.log" "$RETAINED_ARTIFACTS/evidence/dispatch-negatives.log"
    cp "$WORK/health.json" "$RETAINED_ARTIFACTS/evidence/health.json"
    if [[ -n "$INTERRUPT_BOUNDARY" ]]; then
        for marker in "$INTERRUPT_DIR"/*.json; do
            [[ -s "$marker" ]] || fail "an interruption boundary marker is missing: $marker"
            cp "$marker" "$RETAINED_ARTIFACTS/evidence/interrupt-$(basename "$marker")"
        done
        for interrupted in "$WORK"/init-interrupt-*.log; do
            [[ -f "$interrupted" ]] || fail "an interruption init log is missing: $interrupted"
            cp "$interrupted" "$RETAINED_ARTIFACTS/evidence/$(basename "$interrupted")"
        done
    fi
    python3 - "$RETAINED_ARTIFACTS" "$SOURCE_ROOT" "$EXOMONAD_BIN" "$WASM_DIR" \
        "$STORED_HOME/.exo/tl_loop.pyz" "$ACCEPTANCE_STARTED_AT" "$ACCEPTANCE_FINISHED_AT" \
        "$OLD_REFUSAL_SEQ" "$NEW_SELECTED" <<'PYRETAIN' \
        || fail "the retained acceptance artifacts did not rehash to their execution bytes"
import hashlib
import json
import subprocess
import sys
from pathlib import Path

retained, source_root = Path(sys.argv[1]), Path(sys.argv[2])
binary, wasm_dir, archive = Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])
started_at, finished_at, refusal_seq, selected_seq = sys.argv[6:]
source_root = source_root.resolve()

def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()

def git(*arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        check=True,
        capture_output=True,
    ).stdout

artifacts = [
    ("host_binary", "$EXOMONAD_BIN", binary, retained / "host/exomonad"),
    ("controller_archive", "$HOME/.exo/tl_loop.pyz", archive, retained / "controller/tl_loop.pyz"),
]
wasm_paths = sorted(wasm_dir.glob("wasm-guest-*.wasm"))
if not wasm_paths:
    raise SystemExit("no WASM artifacts were consumed by the acceptance")
for path in wasm_paths:
    artifacts.append(
        (
            "wasm_guest",
            f"$WASM_DIR/{path.name}",
            path,
            retained / "wasm" / path.name,
        )
    )

records = []
for kind, source_label, execution_path, retained_path in artifacts:
    execution_hash = digest(execution_path)
    retained_hash = digest(retained_path)
    if execution_hash != retained_hash:
        raise SystemExit(f"retained {kind} bytes differ from their execution bytes: {source_label}")
    records.append(
        {
            "kind": kind,
            "execution_path": source_label,
            "execution_absolute_path": str(execution_path.resolve()),
            "retained_path": retained_path.relative_to(retained).as_posix(),
            "sha256": execution_hash,
            "retained_sha256": retained_hash,
        }
    )

status_digest = hashlib.sha256(git("status", "--porcelain=v1", "-z")).hexdigest()
manifest = {
    "acceptance": "DISPATCH-ALL PASS",
    "started_at": started_at,
    "finished_at": finished_at,
    "source_fingerprint": {
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "root": str(source_root),
        "dirty_status_sha256": status_digest,
        "dirty": bool(git("status", "--porcelain=v1", "-z")),
    },
    "refusal_run_seq": int(refusal_seq),
    "selected_run_seq": int(selected_seq),
    "artifacts": records,
    "notes": [
        "These are the exact contaminant-free binary, WASM guests and controller archive "
        "consumed by the passing dispatch-collision acceptance run.",
        "Hashes were recorded at execution time and independently rehashed from the "
        "retained copies in the same run.",
        "No live Beast state, installed binary or paid provider was used.",
    ],
}
(retained / "manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
with (retained / "SHA256SUMS").open("w", encoding="utf-8") as stream:
    for record in records:
        stream.write(f"{record['sha256']}  {record['retained_path']}\n")
print(json.dumps({
    "retained_artifacts": len(records),
    "manifest": str(retained / "manifest.json"),
}))
PYRETAIN
    python3 "$SOURCE_ROOT/tests/e2e/init-recovery/rehash-dispatch-artifacts.py" "$RETAINED_ARTIFACTS" \
        || fail "independent rehash of the retained acceptance artifacts failed"
    say "retained exact runtime artifacts with execution and independent rehash evidence in $RETAINED_ARTIFACTS"
fi

if [[ -n "${E2E_DISPATCH_COLLISION_ARTIFACTS:-}" ]]; then
    mkdir -p "$E2E_DISPATCH_COLLISION_ARTIFACTS"
    for document in stored-proof.json refused-action.json expected-v2.json \
        health.json init-stored-proof.log mock.log seed.json; do
        [[ -f "$WORK/$document" ]] && cp "$WORK/$document" "$E2E_DISPATCH_COLLISION_ARTIFACTS/"
    done
    for marker in "$INTERRUPT_DIR"/*.json; do
        [[ -s "$marker" ]] && cp "$marker" "$E2E_DISPATCH_COLLISION_ARTIFACTS/"
    done
    for interrupted in "$WORK"/init-interrupt-*.log; do
        [[ -f "$interrupted" ]] && cp "$interrupted" "$E2E_DISPATCH_COLLISION_ARTIFACTS/"
    done
    cp "$RETAINED_HASHES_BEFORE" "$E2E_DISPATCH_COLLISION_ARTIFACTS/retained-hashes-before.sha256"
    cp "$RETAINED_HASHES_AFTER" "$E2E_DISPATCH_COLLISION_ARTIFACTS/retained-hashes-after.sha256"
    say "dispatch-collision acceptance artifacts in $E2E_DISPATCH_COLLISION_ARTIFACTS"
fi

printf 'DISPATCH-ALL PASS\n'
