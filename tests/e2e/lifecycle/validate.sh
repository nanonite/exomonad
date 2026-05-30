#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
EXOMONAD_BIN="${2:?exomonad binary required}"
RESULT_FILE="${3:?result file required}"
SERVER_LOG="${4:-}"

WORKER_NAME="lifecycle-worker"
WORKER_AUTHOR="exomonad-$WORKER_NAME <$WORKER_NAME@exomonad.local>"
failures=()

log() {
    printf '[lifecycle-validator] %s
' "$*"
}

record_failure() {
    failures+=("$*")
    log "FAIL: $*"
}

write_result_and_exit() {
    {
        printf 'Agent lifecycle E2E validation completed at %s
' "$(date -Iseconds)"
        printf 'Repo: %s
' "$REPO_DIR"
        printf 'Worker: %s
' "$WORKER_NAME"
        printf 'Failures: %s
' "${#failures[@]}"
        for failure in "${failures[@]}"; do
            printf -- '- %s
' "$failure"
        done
    } > "$RESULT_FILE"

    if (( ${#failures[@]} == 0 )); then
        log "PASS"
        exit 0
    fi

    log "FAIL (${#failures[@]} failures)"
    exit 1
}

stop_payload() {
    local session_id="$1"
    python3 - "$REPO_DIR" "$session_id" <<'PY'
import json
import sys

repo, session_id = sys.argv[1:3]
print(json.dumps({
    "session_id": session_id,
    "hook_event_name": "Stop",
    "transcript_path": f"/tmp/{session_id}.jsonl",
    "cwd": repo,
    "permission_mode": "default",
}))
PY
}

run_stop_hook() {
    local role="$1"
    local agent_id="$2"
    local payload="$3"
    local output
    local status

    set +e
    output="$(
        printf '%s' "$payload" |             EXOMONAD_ROLE="$role"             EXOMONAD_AGENT_ID="$agent_id"             EXOMONAD_SESSION_ID=main             "$EXOMONAD_BIN" hook stop --runtime claude 2>/dev/null
    )"
    status=$?
    set -e

    printf '%s
' "$status"
    printf '%s
' "$output"
}

assert_stop() {
    local label="$1"
    local role="$2"
    local agent_id="$3"
    local expected_decision="$4"
    local expected_reason="$5"
    local raw
    local status
    local output

    raw="$(run_stop_hook "$role" "$agent_id" "$(stop_payload "$agent_id-session")")"
    status="$(printf '%s
' "$raw" | sed -n '1p')"
    output="$(printf '%s
' "$raw" | sed '1d')"

    if ! python3 - "$expected_decision" "$expected_reason" "$output" <<'PY'
import json
import sys

expected_decision, expected_reason, raw = sys.argv[1:4]
data = json.loads(raw)
decision = data.get("decision")
if decision is None:
    cont = data.get("continue")
    if cont is False:
        decision = "block"
    elif cont is True:
        decision = "allow"
reason = data.get("reason") or data.get("stopReason") or ""
if decision != expected_decision:
    raise SystemExit(1)
if expected_reason and expected_reason not in reason:
    raise SystemExit(2)
PY
    then
        record_failure "$label returned unexpected stop output with exit $status: $output"
        return 0
    fi

    log "OK: $label"
}

assert_clean_worktree() {
    local label="$1"
    local status
    status="$(cd "$REPO_DIR" && git status --porcelain)"
    if [[ -z "$status" ]]; then
        log "OK: $label"
    else
        record_failure "$label expected clean worktree, got: $status"
    fi
}

main() {
    cd "$REPO_DIR"

    printf 'baseline worker output
' > lifecycle-worker-output.txt
    git add lifecycle-worker-output.txt
    git commit -q -m "Add worker lifecycle fixture"
    printf 'worker output
' >> lifecycle-worker-output.txt
    assert_stop "dirty worker stop blocks with file list" "worker" "$WORKER_NAME" "block" "lifecycle-worker-output.txt"

    GIT_AUTHOR_NAME="exomonad-$WORKER_NAME"     GIT_AUTHOR_EMAIL="$WORKER_NAME@exomonad.local"     GIT_COMMITTER_NAME="exomonad-$WORKER_NAME"     GIT_COMMITTER_EMAIL="$WORKER_NAME@exomonad.local"         git add lifecycle-worker-output.txt
    GIT_AUTHOR_NAME="exomonad-$WORKER_NAME"     GIT_AUTHOR_EMAIL="$WORKER_NAME@exomonad.local"     GIT_COMMITTER_NAME="exomonad-$WORKER_NAME"     GIT_COMMITTER_EMAIL="$WORKER_NAME@exomonad.local"         git commit -q -m "Worker lifecycle output"

    actual_author="$(git log -1 --format='%an <%ae>')"
    if [[ "$actual_author" == "$WORKER_AUTHOR" ]]; then
        log "OK: worker commit carries deterministic worker identity"
    else
        record_failure "expected worker author $WORKER_AUTHOR, got $actual_author"
    fi

    assert_stop "clean worker stop allows" "worker" "$WORKER_NAME" "allow" ""

    git checkout -q -b lifecycle-tl
    printf 'baseline tl output
' > lifecycle-tl-dirty.txt
    git add lifecycle-tl-dirty.txt
    git commit -q -m "Add TL lifecycle fixture"
    printf 'throwaway tl output
' >> lifecycle-tl-dirty.txt
    assert_stop "dirty TL feature branch allows stop response" "tl" "lifecycle-tl" "allow" ""
    if [[ -n "$SERVER_LOG" ]] && grep -Fq 'reason=Some("You have uncommitted changes' "$SERVER_LOG"; then
        log "OK: dirty TL feature branch records lifecycle nudge"
    else
        record_failure "dirty TL feature branch did not record lifecycle nudge in server log"
    fi

    git restore --worktree lifecycle-tl-dirty.txt
    assert_clean_worktree "discarded TL throwaway output leaves worktree clean"

    write_result_and_exit
}

main "$@"
