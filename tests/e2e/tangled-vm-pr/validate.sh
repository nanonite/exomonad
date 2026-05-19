#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMEOUT_SECONDS="${TANGLED_VM_PR_E2E_TIMEOUT_SECONDS:-900}"
POLL_SECONDS=5
failures=()
completed=0

log() { printf '[tangled-vm-pr-validator] %s\n' "$*"; }
record_failure() { failures+=("$*"); log "FAIL: $*"; }

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

finish() {
    if (( completed == 0 )) && (( ${#failures[@]} == 0 )); then
        failures+=("validator exited before completing checks")
    fi
    {
        printf 'Tangled VM PR E2E validation completed at %s\n' "$(date -Iseconds)"
        printf 'Session: %s\n' "$SESSION"
        printf 'Repo: %s\n' "$REPO_DIR"
        printf 'Failures: %s\n' "${#failures[@]}"
        for failure in "${failures[@]}"; do
            printf -- '- %s\n' "$failure"
        done
    } > "$RESULT_FILE"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    (( ${#failures[@]} == 0 ))
}
trap finish EXIT

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"
SERVER_LOG="${4:?server log required}"
APPVIEW_URL="${5:-}"

wait_for "local PR registry created" "[[ -f '$REPO_DIR/.exo/prs.json' ]]"
branch="$(python3 "$SCRIPT_DIR/pr-field.py" "$REPO_DIR/.exo/prs.json" head_branch)"
approved_sha_cmd="python3 '$SCRIPT_DIR/pr-field.py' '$REPO_DIR/.exo/prs.json' approved_at_sha"
last_sha_cmd="python3 '$SCRIPT_DIR/pr-field.py' '$REPO_DIR/.exo/prs.json' last_head_sha"

wait_for "dev output exists in dev worktree" "find '$REPO_DIR/.exo/worktrees' -name tangled-vm-pr-dev-output.txt -print 2>/dev/null | grep -q ."
wait_for "dev branch pushed to Tangled VM remote" "cd '$REPO_DIR' && GIT_SSH_COMMAND='${GIT_SSH_COMMAND:-ssh -o StrictHostKeyChecking=no}' git ls-remote tangled 'refs/heads/$branch' | grep -q ."
wait_for "reviewer approval recorded" "[[ -f '$REPO_DIR/.exo/reviews/pr_1.json' ]] && grep -q 'approved' '$REPO_DIR/.exo/reviews/pr_1.json'"
wait_for "approved_at_sha recorded" "$approved_sha_cmd >/dev/null 2>&1"
wait_for "last_head_sha recorded" "$last_sha_cmd >/dev/null 2>&1"

approved_sha="$($approved_sha_cmd)"
last_sha="$($last_sha_cmd)"
if [[ "$approved_sha" != "$last_sha" ]]; then
    record_failure "approved_at_sha ($approved_sha) does not match last_head_sha ($last_sha)"
fi

wait_for "ExoMonad mapped VM Tangled pipeline to PR branch" "grep -R 'Spindle: CI initiated for worktree' '$REPO_DIR/.exo/logs' '$SERVER_LOG' 2>/dev/null | grep '$branch' | grep '$approved_sha' | grep -q ."
wait_for "ExoMonad ingested VM spindle success status for approved SHA" "grep -R 'Spindle: CI status updated' '$REPO_DIR/.exo/logs' '$SERVER_LOG' 2>/dev/null | grep '$branch' | grep '$approved_sha' | grep 'success' | grep -q ."
wait_for "merge-ready notification recorded" "grep -R '\[MERGE READY\]' '$REPO_DIR/.exo/logs' '$SERVER_LOG' 2>/dev/null | grep 'PR #1' | grep -q ."

if [[ -n "$APPVIEW_URL" ]]; then
    wait_for "Tangled VM appview is reachable" "curl -sf '$APPVIEW_URL' >/dev/null 2>&1"
fi

completed=1
