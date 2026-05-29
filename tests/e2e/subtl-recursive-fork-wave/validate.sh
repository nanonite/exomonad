#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"
RUNTIME="${4:?runtime required}"

TIMEOUT_SECONDS="${RECURSIVE_FORK_E2E_TIMEOUT_SECONDS:-420}"
POLL_SECONDS=5
SUBTL_AGENT="recursive-subtl-$RUNTIME"
WORKER_AGENT="recursive-worker-$RUNTIME"
SUBTL_WINDOW="main.$SUBTL_AGENT"
WORKER_MARKER="[RECURSIVE-WORKER-DONE]"
SUBTL_MARKER="[RECURSIVE-SUBTL-DONE]"

failures=()

log() {
    printf '[recursive-fork-validator:%s] %s
' "$RUNTIME" "$*"
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

runtime_config_exists() {
    case "$RUNTIME" in
        claude) [[ -f "$REPO_DIR/.exo/worktrees/$SUBTL_AGENT/.claude/settings.local.json" ]] ;;
        codex) [[ -f "$REPO_DIR/.exo/worktrees/$SUBTL_AGENT/.codex/config.toml" ]] ;;
        opencode) [[ -f "$REPO_DIR/.exo/agents/$SUBTL_AGENT/opencode.json" || -f "$REPO_DIR/.exo/worktrees/$SUBTL_AGENT/.exo/agents/root/opencode.json" ]] ;;
    esac
}

main() {
    wait_for "sub-TL runtime config exists" "$(declare -f runtime_config_exists); RUNTIME='$RUNTIME'; REPO_DIR='$REPO_DIR'; SUBTL_AGENT='$SUBTL_AGENT'; runtime_config_exists"
    wait_for "worker routing metadata exists" "[[ -f '$REPO_DIR/.exo/agents/$WORKER_AGENT/routing.json' ]]"
    wait_for "sub-TL tmux window exists" "tmux list-windows -t '$SESSION' -F '#{window_name}' 2>/dev/null | grep -Fxq '$SUBTL_WINDOW'"
    wait_for "worker notify_parent event recorded" "grep -R '$WORKER_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "worker-to-sub-TL delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '$WORKER_AGENT' | grep 'outcome="success"' | grep -q ."
    wait_for "sub-TL received worker notification" "tmux capture-pane -p -t '$SESSION:$SUBTL_WINDOW.0' -S -2000 2>/dev/null | grep -Fq '$WORKER_MARKER'"
    wait_for "sub-TL notify_parent event recorded" "grep -R '$SUBTL_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "sub-TL-to-root delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '$SUBTL_AGENT' | grep 'outcome="success"' | grep -q ."
    wait_for "TL ChildSpawned phase transition logged" "grep -R '\[tl\].*ChildHandle' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."

    if grep -R '\[tl\] Invalid transition' "$REPO_DIR/.exo/logs" 2>/dev/null | grep -q .; then
        record_failure "TL phase invalid transition logged"
    fi

    {
        printf 'Recursive fork_wave E2E validation completed at %s
' "$(date -Iseconds)"
        printf 'Runtime: %s
' "$RUNTIME"
        printf 'Session: %s
' "$SESSION"
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
        tmux kill-session -t "$SESSION" 2>/dev/null || true
        exit 0
    fi

    log "FAIL (${#failures[@]} failures)"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    exit 1
}

main "$@"
