#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
EXOMONAD_BIN="${2:?exomonad binary required}"
RESULT_FILE="${3:?result file required}"

WORKER_EMAIL="authorship-worker@example.com"
TL_EMAIL="tl@example.com"
failures=()

log() {
    printf '[authorship-validator] %s
' "$*"
}

record_failure() {
    failures+=("$*")
    log "FAIL: $*"
}

write_result_and_exit() {
    {
        printf 'Reviewer authorship E2E validation completed at %s
' "$(date -Iseconds)"
        printf 'Repo: %s
' "$REPO_DIR"
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

payload_for_tool() {
    local tool_name="$1"
    local command="${2:-}"
    python3 - "$REPO_DIR" "$tool_name" "$command" <<'PY'
import json
import sys

repo, tool_name, command = sys.argv[1:4]
tool_input = {}
if tool_name == "Bash":
    tool_input = {"command": command}
elif tool_name == "Write":
    tool_input = {"file_path": "reviewer-denied.txt", "content": "should not be written"}
elif tool_name == "Edit":
    tool_input = {"file_path": "README.md", "old_string": "missing", "new_string": "denied"}

print(json.dumps({
    "session_id": "authorship-reviewer-session",
    "hook_event_name": "PreToolUse",
    "tool_name": tool_name,
    "tool_input": tool_input,
    "transcript_path": "/tmp/authorship-reviewer.jsonl",
    "cwd": repo,
    "permission_mode": "default",
}))
PY
}

run_reviewer_hook() {
    local payload="$1"
    local output
    local status

    set +e
    output="$(
        printf '%s' "$payload" |             EXOMONAD_ROLE=reviewer             EXOMONAD_AGENT_ID="authorship-reviewer"             EXOMONAD_SESSION_ID=main             "$EXOMONAD_BIN" hook pre-tool-use --runtime claude 2>/dev/null
    )"
    status=$?
    set -e

    printf '%s
' "$status"
    printf '%s
' "$output"
}

assert_decision() {
    local label="$1"
    local expected_decision="$2"
    local expected_reason="$3"
    local expected_status="$4"
    local payload="$5"
    local raw
    local status
    local output

    raw="$(run_reviewer_hook "$payload")"
    status="$(printf '%s
' "$raw" | sed -n '1p')"
    output="$(printf '%s
' "$raw" | sed '1d')"

    if [[ "$status" != "$expected_status" ]]; then
        record_failure "$label expected hook exit $expected_status, got $status"
        return 0
    fi

    if ! python3 - "$expected_decision" "$expected_reason" "$output" <<'PY'
import json
import sys

expected_decision, expected_reason, raw = sys.argv[1:4]
data = json.loads(raw)
hook = data.get("hookSpecificOutput", {})
decision = hook.get("permissionDecision")
reason = data.get("stopReason", "") or hook.get("permissionDecisionReason", "")
if decision != expected_decision:
    raise SystemExit(1)
if expected_reason and expected_reason not in reason:
    raise SystemExit(2)
PY
    then
        record_failure "$label returned unexpected hook output: $output"
        return 0
    fi

    log "OK: $label"
}

assert_command_runs() {
    local label="$1"
    local command="$2"
    if (cd "$REPO_DIR" && bash -c "$command" >/dev/null); then
        log "OK: $label"
    else
        record_failure "$label command failed after hook allow"
    fi
}

prepare_worker_commit() {
    cd "$REPO_DIR"
    git checkout -q -b authorship-dev
    git config user.name "Authorship Worker"
    git config user.email "$WORKER_EMAIL"
    printf 'worker authored change
' > authorship-marker.txt
    git add authorship-marker.txt
    git commit -q -m "Add authorship marker"
    git checkout -q main
    git config user.name "Exomonad TL"
    git config user.email "$TL_EMAIL"
}

validate_authorship_preservation() {
    cd "$REPO_DIR"
    git merge --ff-only authorship-dev >/dev/null
    local last_email
    last_email="$(git log -1 main --format='%ae')"
    if [[ "$last_email" == "$WORKER_EMAIL" ]]; then
        log "OK: merged head preserves worker author email"
    else
        record_failure "expected main head author $WORKER_EMAIL, got $last_email"
    fi
    if [[ "$last_email" == "$TL_EMAIL" ]]; then
        record_failure "main head author was rewritten to TL email"
    fi
}

main() {
    prepare_worker_commit

    assert_decision "reviewer Write denied" "deny" "Reviewers do not edit code" "2" "$(payload_for_tool Write)"
    assert_decision "reviewer Edit denied" "deny" "Reviewers do not edit code" "2" "$(payload_for_tool Edit)"
    assert_decision "reviewer git commit denied" "deny" "Reviewer cannot author or rewrite commits" "2" "$(payload_for_tool Bash "git commit -am 'fix'")"
    assert_decision "reviewer git status allowed" "allow" "" "0" "$(payload_for_tool Bash "git status")"
    assert_command_runs "git status executes read-only" "git status --short"
    assert_decision "reviewer git rev-parse allowed" "allow" "" "0" "$(payload_for_tool Bash "git rev-parse HEAD")"
    assert_command_runs "git rev-parse executes read-only" "git rev-parse HEAD"

    if [[ -f "$REPO_DIR/reviewer-denied.txt" ]]; then
        record_failure "reviewer-denied.txt was created despite Write denial"
    fi

    validate_authorship_preservation
    write_result_and_exit
}

main "$@"
