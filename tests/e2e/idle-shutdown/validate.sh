#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
RESULT_FILE="${2:?result file required}"
TIMEOUT_SECONDS="${IDLE_SHUTDOWN_E2E_TIMEOUT_SECONDS:-420}"
POLL_SECONDS=5

failures=()

log() {
    printf '[idle-shutdown-validator] %s\n' "$*"
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

open_issue_count() {
    (
        cd "$REPO_DIR"
        chainlink issue list --json
    ) | python3 -c 'import json, sys; issues = json.load(sys.stdin); print(sum(1 for issue in issues if issue.get("status") == "open"))'
}

tool_called() {
    local tool_name="$1"
    grep -R "tool_name.*$tool_name\|Executing tool.*$tool_name\|$tool_name" "$REPO_DIR/.exo/logs" 2>/dev/null | grep -q .
}

write_result_and_exit() {
    {
        printf 'Idle shutdown E2E validation completed at %s\n' "$(date -Iseconds)"
        printf 'Repo: %s\n' "$REPO_DIR"
        printf 'Failures: %s\n' "${#failures[@]}"
        for failure in "${failures[@]}"; do
            printf -- '- %s\n' "$failure"
        done
    } > "$RESULT_FILE"

    if (( ${#failures[@]} == 0 )); then
        log "PASS"
        exit 0
    fi

    log "FAIL (${#failures[@]} failures)"
    exit 1
}

main() {
    wait_for "Chainlink backlog reaches zero open issues" \
        "[[ \"\$(bash '$0' --open-count '$REPO_DIR')\" == \"0\" ]]"
    wait_for "has_pending_work tool call is logged" \
        "grep -R 'has_pending_work' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "shutdown_server tool call is logged" \
        "grep -R 'shutdown_server' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "ExoMonad server socket is gone" \
        "[[ ! -S '$REPO_DIR/.exo/server.sock' ]]"

    write_result_and_exit
}

if [[ "${1:-}" == "--open-count" ]]; then
    REPO_DIR="${2:?repo dir required}"
    open_issue_count
    exit 0
fi

main "$@"
