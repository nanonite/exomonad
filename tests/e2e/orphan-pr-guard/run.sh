#!/usr/bin/env bash
set -euo pipefail

# Opt-in live smoke only. The correctness boundary is the deterministic host,
# service, and WASM coverage from #563 and #564.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
EXOMONAD_BIN="${EXOMONAD_BIN:-$PROJECT_ROOT/target/debug/exomonad}"

for command_name in curl git python3 tmux; do
    command -v "$command_name" >/dev/null || {
        echo "ERROR: required command '$command_name' is missing" >&2
        exit 1
    }
done
if [[ ! -x "$EXOMONAD_BIN" ]]; then
    echo "ERROR: exomonad binary not found at $EXOMONAD_BIN" >&2
    exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/exomonad-orphan-pr-guard.XXXXXXXX")"
REMOTE_DIR="$WORK_DIR/remote.git"
REPO_DIR="$WORK_DIR/repo"
CODEX_HOME_DIR="$WORK_DIR/codex-home"
MOCK_LOG="$WORK_DIR/mock.log"
MOCK_STDERR="$WORK_DIR/mock.stderr"
INIT_LOG="$WORK_DIR/init.log"
SESSION="e2e-orphan-pr-guard-codex"
OWNER_NAME="m7-3a-fixture-oracle-opencode"
OWNER_SLUG="m7-3a-fixture-oracle"
PR_BRANCH="main.$OWNER_NAME"
MOCK_PORT="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
MOCK_URL="http://127.0.0.1:$MOCK_PORT"
MOCK_PID=""
INIT_PID=""

cleanup() {
    local status=$?
    set +e
    if [[ -n "$INIT_PID" ]]; then
        kill "$INIT_PID" 2>/dev/null || true
    fi
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [[ -n "$MOCK_PID" ]]; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
    fi
    echo "Fixture logs: $WORK_DIR"
    if [[ -f "$INIT_LOG" ]]; then
        tail -80 "$INIT_LOG" || true
    fi
    rm -rf "$WORK_DIR"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

mkdir -p "$REPO_DIR" "$CODEX_HOME_DIR" "$REPO_DIR/.exo/worktrees"
git init --bare "$REMOTE_DIR" -q
git init "$REPO_DIR" -q -b main
git -C "$REPO_DIR" config user.name "Exomonad orphan-PR E2E"
git -C "$REPO_DIR" config user.email "orphan-pr-e2e@example.invalid"
git -C "$REPO_DIR" remote add origin "https://github.com/test-owner/orphan-pr-fixture.git"
git -C "$REPO_DIR" remote set-url --push origin "$REMOTE_DIR"

printf '%s\n' 'orphan-pr fixture' > "$REPO_DIR/README.md"
git -C "$REPO_DIR" add README.md
git -C "$REPO_DIR" commit -m 'seed orphan PR fixture' -q
git -C "$REPO_DIR" push origin main -q

git -C "$REPO_DIR" checkout -b "$PR_BRANCH" -q
printf '%s\n' 'review requested' > "$REPO_DIR/review-fix.txt"
git -C "$REPO_DIR" add review-fix.txt
git -C "$REPO_DIR" commit -m 'seed review-requested PR head' -q
git -C "$REPO_DIR" push origin "$PR_BRANCH" -q
git -C "$REPO_DIR" checkout main -q

# Seed the historical owner as fixture data. The live root harness is Codex;
# the OpenCode suffix is deliberately preserved only to model the old branch.
mkdir -p "$REPO_DIR/.exo/agents/$OWNER_NAME" "$REPO_DIR/.exo/roles/devswarm/context" "$REPO_DIR/.exo/wasm"
cat > "$REPO_DIR/.exo/agents/$OWNER_NAME/identity.json" <<EOF
{
  "agent_name": "$OWNER_NAME",
  "slug": "$OWNER_SLUG",
  "agent_type": "opencode",
  "birth_branch": "$PR_BRANCH",
  "parent_branch": "main",
  "working_dir": ".exo/worktrees/$OWNER_NAME",
  "display_name": "$OWNER_NAME",
  "topology": "worktree_per_agent"
}
EOF
git -C "$REPO_DIR" worktree add "$REPO_DIR/.exo/worktrees/$OWNER_NAME" "$PR_BRANCH" -q

cp -R "$PROJECT_ROOT/.exo/roles/devswarm/context/." "$REPO_DIR/.exo/roles/devswarm/context/"
for wasm_file in "$PROJECT_ROOT/.exo/wasm/"wasm-guest-*.wasm; do
    [[ -e "$wasm_file" ]] || continue
    ln -s "$wasm_file" "$REPO_DIR/.exo/wasm/$(basename "$wasm_file")"
done

REMOTE_DIR="$REMOTE_DIR" MOCK_LOG="$MOCK_LOG" \
    python3 "$PROJECT_ROOT/tests/e2e/mock_github.py" --port "$MOCK_PORT" \
    >"$MOCK_STDERR" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do
    if curl -fsS "$MOCK_URL/admin/actions/runners" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
curl -fsS "$MOCK_URL/admin/actions/runners" >/dev/null

PR_PAYLOAD="$(python3 - <<PY
import json

print(json.dumps({
    "title": "Fixture PR with requested review changes",
    "head": "$PR_BRANCH",
    "base": "main",
    "body": "Fixture PR for orphan recovery.\\n\\nBirth-Branch: $PR_BRANCH\\nAuthoring-Agent: $OWNER_NAME",
}))
PY
)"
PR_JSON="$(curl -fsS -X POST "$MOCK_URL/api/v1/repos/test-owner/orphan-pr-fixture/pulls" \
    -H 'Content-Type: application/json' --data "$PR_PAYLOAD")"
PR_NUMBER="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])' <<<"$PR_JSON")"
curl -fsS -X POST "$MOCK_URL/_control/reviews" \
    -H 'Content-Type: application/json' \
    --data "$(python3 - <<PY
import json

print(json.dumps({
    "pr_number": int("$PR_NUMBER"),
    "state": "CHANGES_REQUESTED",
    "body": "Please address the review changes on this existing PR.",
}))
PY
)" >/dev/null

cat > "$REPO_DIR/.exo/config.toml" <<EOF
default_role = "devswarm"
wasm_name = "devswarm"
shell_command = "bash"
tmux_session = "$SESSION"
root_agent_type = "codex"
spawn_agent_type = "codex"
forgejo_url = "$MOCK_URL"
forgejo_token = "orphan-pr-e2e-token"
yolo = true
poll_interval = 5
initial_prompt = """
Address the requested review changes on the existing pull request. First inspect its state,
then resume the existing PR by number with the complete task. Do not create a replacement
branch or a second pull request.
"""
EOF

cat > "$CODEX_HOME_DIR/config.toml" <<EOF
[projects."$REPO_DIR"]
trust_level = "trusted"
EOF

export CODEX_HOME="$CODEX_HOME_DIR"
export FORGEJO_API_URL="$MOCK_URL"
export FORGEJO_URL="$MOCK_URL"
export FORGEJO_TOKEN="orphan-pr-e2e-token"

echo "Running opt-in Codex orphan-PR smoke for PR #$PR_NUMBER"
timeout 300 "$EXOMONAD_BIN" init --verbose --session "$SESSION" --tl codex \
    >"$INIT_LOG" 2>&1 &
INIT_PID=$!
wait "$INIT_PID" || true

ALL_LOGS="$WORK_DIR/all-logs.txt"
{
    cat "$INIT_LOG" "$MOCK_LOG" "$MOCK_STDERR" 2>/dev/null || true
    tmux capture-pane -p -t "$SESSION:TL" 2>/dev/null || true
} > "$ALL_LOGS"

grep -q 'resume_pr' "$ALL_LOGS" || {
    echo "ERROR: Codex TL did not expose an observable resume_pr call" >&2
    exit 1
}
grep -q "$PR_BRANCH" "$ALL_LOGS" || {
    echo "ERROR: exact historical PR branch was not observed" >&2
    exit 1
}

mapfile -t child_branches < <(git -C "$REPO_DIR" for-each-ref --format='%(refname:short)' refs/heads/main.*)
[[ "${#child_branches[@]}" -eq 1 && "${child_branches[0]}" == "$PR_BRANCH" ]] || {
    printf 'ERROR: unexpected child branches: %s\n' "${child_branches[*]}" >&2
    exit 1
}
! printf '%s\n' "${child_branches[@]}" | grep -Eq -- '-(opencode|codex)-(opencode|codex)|-2($|[-.])' || {
    echo "ERROR: double runtime suffix or -2 branch created" >&2
    exit 1
}

worktree_count="$(git -C "$REPO_DIR" worktree list --porcelain | grep -c "branch refs/heads/$PR_BRANCH" || true)"
[[ "$worktree_count" -eq 1 ]] || {
    echo "ERROR: expected exactly one worktree for $PR_BRANCH, found $worktree_count" >&2
    exit 1
}

open_pr_summary="$(curl -fsS "$MOCK_URL/api/v1/repos/test-owner/orphan-pr-fixture/pulls" | \
    python3 -c '
import json
import sys

prs = json.load(sys.stdin)
summary = str(len(prs))
if len(prs) == 1:
    summary += ":{}:{}".format(prs[0]["head"]["ref"], prs[0]["merged"])
print(summary)
')"
[[ "$open_pr_summary" == "1:$PR_BRANCH:False" ]] || {
    echo "ERROR: expected one open, unmerged PR on $PR_BRANCH; found $open_pr_summary" >&2
    exit 1
}

echo "Codex orphan-PR smoke passed for PR #$PR_NUMBER"
