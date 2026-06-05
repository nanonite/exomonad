#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"

TIMEOUT_SECONDS="${SUBTL_WORKER_NOTIFY_E2E_TIMEOUT_SECONDS:-360}"
POLL_SECONDS=5
SUBTL_WINDOW="main.subtl-worker-notify-tl-codex"
WORKER_AGENT="subtl-worker-notify-worker-codex"
MESSAGE_MARKER="[SUBTL-WORKER-NOTIFY]"

failures=()

log() {
    printf '[subtl-worker-notify-validator] %s
' "$*"
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

active_subtl_pane() {
    tmux list-panes -t "$SESSION:$SUBTL_WINDOW" -F '#{pane_index}:#{pane_active}' 2>/dev/null         | awk -F: '$2 == 1 { print $1; exit }'
}

main() {
    wait_for "sub-TL Codex config exists" "[[ -f '$REPO_DIR/.exo/worktrees/subtl-worker-notify-tl-codex/.codex/config.toml' ]]"
    wait_for "worker routing metadata exists" "[[ -f '$REPO_DIR/.exo/agents/$WORKER_AGENT/routing.json' ]]"
    wait_for "sub-TL tmux window exists" "tmux list-windows -t '$SESSION' -F '#{window_name}' 2>/dev/null | grep -Fxq '$SUBTL_WINDOW'"
    wait_for "worker notify_parent event recorded" "grep -R '$MESSAGE_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "worker notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '$WORKER_AGENT' | grep 'agent_inbox_tmux' | grep 'outcome="success"' | grep -q ."
    wait_for "notification reached sub-TL pane zero" "tmux capture-pane -p -t '$SESSION:$SUBTL_WINDOW.0' -S -2000 2>/dev/null | grep -Fq '$MESSAGE_MARKER'"

    pane_count="$(tmux list-panes -t "$SESSION:$SUBTL_WINDOW" 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$pane_count" -lt 2 ]]; then
        record_failure "expected sub-TL window to contain a worker pane"
    fi

    active_pane="$(active_subtl_pane || true)"
    if [[ -n "$active_pane" && "$active_pane" != "0" ]]; then
        log "OK: worker pane was active while pane-zero delivery was observed"
    else
        record_failure "expected active sub-TL pane to be a worker pane, got '${active_pane:-unknown}'"
    fi

    {
        printf 'Sub-TL worker notify E2E validation completed at %s
' "$(date -Iseconds)"
        printf 'Session: %s
' "$SESSION"
        printf 'Repo: %s
' "$REPO_DIR"
        printf 'Sub-TL window: %s
' "$SUBTL_WINDOW"
        printf 'Failures: %s
' "${#failures[@]}"
        for failure in "${failures[@]}"; do
            printf -- '- %s
' "$failure"
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
