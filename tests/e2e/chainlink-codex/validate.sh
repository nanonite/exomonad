#!/usr/bin/env bash
set -euo pipefail

# Validator for the Python-TL-driven Chainlink Codex scenario.
#
# The Chainlink issue belongs to the harness, not to the dispatched worker: the
# Python controller holds no Chainlink authority and the worker role is granted
# neither `chainlink_issue_create` nor `chainlink_issue_close`. So the ownership
# assertions are negative by design -- the worker comments on the issue and owns
# a session for it, and the issue is still open afterwards.
#
# `--assert-comment <repo> <id>` and `--assert-issue-open <repo> <id>` are
# re-entry points: a `wait_for` probe runs in a subshell, so the Chainlink reads
# have to be restartable on their own rather than closing over this shell's
# locals.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../lib/git-fixture.sh
source "$PROJECT_ROOT/tests/e2e/lib/git-fixture.sh"
# shellcheck source=../lib/python-tl.sh
source "$PROJECT_ROOT/tests/e2e/lib/python-tl.sh"

COMMENT_MARKER="[CHAINLINK-CODEX-WORKER-COMMENT]"

# Read the fixture's issue once, as JSON. `--db` is passed explicitly so the
# assertion never depends on an inherited CHAINLINK_DB from the operator's
# shell; a validator pointed at the wrong database would pass vacuously.
issue_document() {
    chainlink --db "$REPO_DIR/.chainlink" --json issue show "$1" 2>/dev/null
}

issue_status_is_open() {
    issue_document "$1" | python3 -c \
        'import json, sys; raise SystemExit(0 if json.load(sys.stdin)["status"] == "open" else 1)'
}

issue_has_comment() {
    issue_document "$1" | python3 -c '
import json
import sys

marker = sys.argv[1]
comments = json.load(sys.stdin).get("comments", [])
raise SystemExit(0 if any(marker in str(c.get("content", "")) for c in comments) else 1)
' "$COMMENT_MARKER"
}

case "${1:-}" in
    --assert-comment)
        REPO_DIR="${2:?repo dir required}"
        issue_has_comment "${3:?issue id required}"
        exit $?
        ;;
    --assert-issue-open)
        REPO_DIR="${2:?repo dir required}"
        issue_status_is_open "${3:?issue id required}"
        exit $?
        ;;
esac

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"
CODEX_HOME="${4:?isolated codex home required}"
ISSUE_ID="${5:?chainlink issue id required}"

TIMEOUT_SECONDS="${CHAINLINK_CODEX_E2E_TIMEOUT_SECONDS:-600}"
POLL_SECONDS=5
TL_WINDOW="TL"
WORKER_AGENT="chainlink-codex-worker-codex"
DONE_MARKER="[CHAINLINK-CODEX-WORKER-DONE]"
WORKER_PROTOCOL="ExoMonad Worker Agent Protocol"

failures=()

log() {
    printf '[chainlink-codex-validator] %s\n' "$*"
}

record_failure() {
    failures+=("$*")
    log "FAIL: $*"
}

wait_for() {
    local label="$1"
    local command="$2"
    local deadline=$((SECONDS + TIMEOUT_SECONDS))

    while (( SECONDS < deadline )); do
        if bash -c "$command"; then
            log "OK: $label"
            return 0
        fi
        sleep "$POLL_SECONDS"
    done

    record_failure "$label timed out after ${TIMEOUT_SECONDS}s"
    return 1
}

check() {
    local label="$1"
    shift
    if "$@"; then
        log "OK: $label"
    else
        record_failure "$label"
    fi
}

worker_config() {
    printf '%s/.exo/agents/%s/.codex/config.toml\n' "$REPO_DIR" "$WORKER_AGENT"
}

# Chainlink agents run this scenario's issue in the fixture's own worktree, not
# in a shared lock checkout. A lock worktree would mean the dispatched worker
# escaped its own worktree, so its absence is an ownership assertion.
# shellcheck disable=SC2329  # invoked through check(), which forwards via "$@"
no_chainlink_lock_worktree() {
    if git -C "$REPO_DIR" worktree list --porcelain | grep -Fq '.chainlink/.locks-cache'; then
        return 1
    fi
    [[ ! -e "$REPO_DIR/.chainlink/.locks-cache" ]]
}

e2e_git_use_fixture_root "$REPO_DIR"

main() {
    # --- The controller, not an interactive Codex root TL ---
    wait_for "Python TL controller window exists" \
        "tmux list-windows -t '$SESSION' -F '#{window_name}' 2>/dev/null | grep -Fxq '$TL_WINDOW'"
    wait_for "TL plan was consumed into a controller checkpoint" \
        "test -f '$(e2e_python_tl_run_state "$REPO_DIR")'"
    check "no retired interactive Codex root TL config" \
        e2e_python_tl_assert_no_codex_root_tl "$REPO_DIR"
    check "CODEX_HOME propagated into the tmux session" \
        e2e_python_tl_assert_session_codex_home "$SESSION" "$CODEX_HOME"

    # --- The dispatched Codex child ---
    wait_for "Codex worker config exists" "test -f '$(worker_config)'"
    check "Codex worker config is role-correct" \
        e2e_python_tl_assert_codex_child_config \
        "$(worker_config)" "chainlink worker" "worker" "$WORKER_AGENT" "$WORKER_PROTOCOL"
    check "Codex worker trust is in the isolated home" \
        e2e_python_tl_assert_codex_trust "$CODEX_HOME" "$(worker_config)"

    # --- Chainlink role workflow on a foreign issue ---
    wait_for "worker Chainlink comment landed on the issue" \
        "bash '$0' --assert-comment '$REPO_DIR' '$ISSUE_ID'"
    wait_for "worker Chainlink session completion recorded" \
        "grep -R '$DONE_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "worker notify_parent reached the controller" \
        "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '\"recipient\":\"root\"' | grep '\"outcome\":\"success\"' | grep -q ."

    # --- Chainlink ownership: the worker did not take the issue over ---
    # Checked after the workflow above, so a worker that closed the issue on its
    # way out is caught rather than being masked by the issue's initial state.
    check "Chainlink issue is still open" \
        bash "$0" --assert-issue-open "$REPO_DIR" "$ISSUE_ID"
    check "no Chainlink lock worktree was created" no_chainlink_lock_worktree

    # --- Durable controller state ---
    wait_for "controller reached a terminal phase" \
        "python3 -c \"import json, sys; sys.exit(0 if json.load(open('$(e2e_python_tl_run_state "$REPO_DIR")'))['fsm']['phase'] in ('tl_done', 'tl_parked', 'tl_failed') else 1)\""
    check "plan slice is present in the checkpoint" \
        e2e_python_tl_assert_slices "$REPO_DIR" "$WORKER_AGENT"
    check "controller reached the expected terminal phase" \
        e2e_python_tl_assert_phase "$REPO_DIR" "tl_done"

    {
        printf 'Chainlink Codex E2E validation completed at %s\n' "$(date -Iseconds)"
        printf 'Session: %s\n' "$SESSION"
        printf 'Repo: %s\n' "$REPO_DIR"
        printf 'Codex home: %s\n' "$CODEX_HOME"
        printf 'Issue: #%s\n' "$ISSUE_ID"
        printf 'Failures: %s\n' "${#failures[@]}"
        for failure in "${failures[@]}"; do
            printf -- '- %s\n' "$failure"
        done
    } > "$RESULT_FILE"

    if (( ${#failures[@]} == 0 )); then
        log "PASS"
        tmux kill-session -t "$SESSION" 2>/dev/null || true
        exit 0
    fi

    log "FAIL (${#failures[@]} failures)"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    exit 1
}

main "$@"
