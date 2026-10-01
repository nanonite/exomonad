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

# The plan slice name is what the controller keys `run.json`'s `slices` by; the
# agent identity adds the harness suffix and names the agent directory. Deriving
# both from the shipped plan keeps the two from being confused for each other.
PLAN_SLICE="$(e2e_python_tl_plan_slices "$SCRIPT_DIR/plan.json")"
WORKER_AGENT="$(e2e_python_tl_agent_identity "$PLAN_SLICE")"

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

# `wait_for` returns 0 even when it times out, and that is deliberate.
#
# This file runs under `set -euo pipefail`, and the callers are bare statements,
# so a non-zero return aborts the whole validator at the first timed-out
# assertion -- before the durable-state checks, before the summary, and above all
# before `$RESULT_FILE` is written. `run.sh` then reports `validator wrote no
# result file`, which is the *opposite* of what happened: the validator had
# already recorded real failures, and every one of them was discarded. The
# 2026-09-30 live run lost twelve passing assertions and a named failure this
# way.
#
# The failure is already recorded by `record_failure`, and `run.sh` keys on the
# `Failures: N` line rather than on the exit status, so returning 0 loses
# nothing and lets the remaining assertions run and report. A timed-out
# assertion still fails the scenario, through the count.
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
    return 0
}

# Same reasoning as `wait_for`, and the same original defect: `record_failure`
# ends in `log`, which returns 0, so this function returns 0 either way. Kept
# explicit because the invariant is the whole point -- a failing assertion must
# never abort the validator before it writes its verdict.
check() {
    local label="$1"
    shift
    if "$@"; then
        log "OK: $label"
    else
        record_failure "$label"
    fi
    return 0
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
    # `-F` on the marker grep, and only there. `DONE_MARKER` is
    # `[CHAINLINK-CODEX-WORKER-DONE]`, and in a basic regular expression the
    # brackets are a bracket expression over `CHAINLINK-CODEX-WORKER-DONE`, whose
    # `-` characters are ranges. GNU grep rejects the whole pattern with
    # `Invalid range end` and exits 2, so the probe can never succeed: the
    # assertion timed out after its full 600s in the 2026-09-30 live run even
    # though the worker's `chainlink_session_end` notes carry the marker in
    # `.exo/logs`. The same defect is in `codex-messaging` and
    # `python-tl-worker-notify`; those belong to #1152 and #1154, and are
    # tracked as `PENDING_MARKER_FIX` in `test_contract.py`.
    wait_for "worker Chainlink session completion recorded" \
        "grep -RF '$DONE_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
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
        e2e_python_tl_assert_slices "$REPO_DIR" "$PLAN_SLICE"
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
