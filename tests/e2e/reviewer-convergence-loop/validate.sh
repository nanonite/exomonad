#!/usr/bin/env bash
# Process companion for the reviewer-convergence-loop E2E test.
#
# Watches for the testrunner's verdict signal — a marker file the
# testrunner writes via Bash when it concludes — and translates it
# into the harness's RESULT_FILE format. The harness's run.sh
# inspects RESULT_FILE for "Failures: 0" to set its exit code.
#
# Args (positional):
#   $1  REPO_DIR     — the workspace root (so we can locate .exo/)
#   $2  SESSION      — tmux session name (for diagnostic dumps)
#   $3  RESULT_FILE  — file we must write the verdict to
#   $4  SERVER_LOG   — exomonad server log path (also dumped on failure)
#
# Lifecycle: runs from `exomonad init` as a process companion. Polls
# until either the marker file is present or the configured timeout
# elapses. Writes RESULT_FILE then exits 0 regardless of verdict — the
# verdict is communicated through RESULT_FILE, not through this
# script's exit code.

set -u  # -e intentionally OFF: we want to always write RESULT_FILE.

REPO_DIR="${1:?REPO_DIR required}"
SESSION="${2:?SESSION required}"
RESULT_FILE="${3:?RESULT_FILE required}"
SERVER_LOG="${4:-}"

MARKER_DIR="$REPO_DIR/.exo/e2e-reviewer-convergence"
SUCCESS_MARKER="$MARKER_DIR/success"
FAILURE_MARKER="$MARKER_DIR/failure"
TIMEOUT_SECS="${E2E_REVIEWER_TIMEOUT:-1500}"  # 25 min default
POLL_SECS=5
START=$(date +%s)

mkdir -p "$MARKER_DIR"

write_result() {
    local verdict="$1"
    local reason="${2:-}"
    {
        echo "Verdict: $verdict"
        echo "Failures: $([[ "$verdict" == "success" ]] && echo 0 || echo 1)"
        echo "Reason: $reason"
        if [[ -f "$SUCCESS_MARKER" ]]; then
            echo "Success marker content:"
            sed 's/^/  /' "$SUCCESS_MARKER"
        fi
        if [[ -f "$FAILURE_MARKER" ]]; then
            echo "Failure marker content:"
            sed 's/^/  /' "$FAILURE_MARKER"
        fi
        if [[ "$verdict" != "success" && -n "$SERVER_LOG" && -f "$SERVER_LOG" ]]; then
            echo "Last 100 server log lines:"
            tail -n 100 "$SERVER_LOG" | sed 's/^/  /'
        fi
    } > "$RESULT_FILE"
}

echo "[validate.sh] watching $MARKER_DIR (timeout ${TIMEOUT_SECS}s, poll ${POLL_SECS}s)"

while true; do
    if [[ -f "$SUCCESS_MARKER" ]]; then
        echo "[validate.sh] success marker observed"
        write_result success "testrunner reported convergence-loop verified"
        exit 0
    fi
    if [[ -f "$FAILURE_MARKER" ]]; then
        echo "[validate.sh] failure marker observed"
        write_result failure "testrunner reported convergence-loop FAILED — see marker content + server log"
        exit 0
    fi

    NOW=$(date +%s)
    if (( NOW - START >= TIMEOUT_SECS )); then
        echo "[validate.sh] timed out after ${TIMEOUT_SECS}s"
        write_result failure "validator timed out after ${TIMEOUT_SECS}s — testrunner never wrote success/failure marker"
        exit 0
    fi

    sleep "$POLL_SECS"
done
