#!/usr/bin/env bash
set -euo pipefail

# Validator for the Python-TL-driven worker notify scenario.
#
# The controller runs in the `TL` window and dispatches its Codex worker into a
# pane of that same window, so the notification has to be injected into the
# controller's window rather than wherever focus happens to be.

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"
CODEX_HOME="${4:?isolated codex home required}"

TIMEOUT_SECONDS="${PYTHON_TL_WORKER_NOTIFY_E2E_TIMEOUT_SECONDS:-600}"
POLL_SECONDS=5
TL_WINDOW="TL"
MESSAGE_MARKER="[PYTHON-TL-WORKER-NOTIFY]"
WORKER_PROTOCOL="ExoMonad Worker Agent Protocol"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../lib/python-tl.sh
source "$PROJECT_ROOT/tests/e2e/lib/python-tl.sh"

# The plan slice name is what the controller keys `run.json`'s `slices` by; the
# agent identity adds the harness suffix and names the agent directory. Deriving
# both from the shipped plan keeps the two from being confused for each other.
PLAN_SLICE="$(e2e_python_tl_plan_slices "$SCRIPT_DIR/plan.json")"
WORKER_AGENT="$(e2e_python_tl_agent_identity "$PLAN_SLICE")"

failures=()

log() {
    printf '[python-tl-worker-notify-validator] %s\n' "$*"
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

# True when the notification is visible in any pane of the controller's window.
# Scanning the window rather than a fixed pane index is deliberate: delivery to a
# recipient with no routing.json resolves through the window's current pane, so
# the pane index is not a contract. The window is.
# shellcheck disable=SC2329  # invoked through wait_for(), which re-runs it in a subshell
marker_reached_controller_window() {
    local pane
    while IFS= read -r pane; do
        if tmux capture-pane -p -t "$pane" -S -2000 2>/dev/null | grep -Fq "$MESSAGE_MARKER"; then
            return 0
        fi
    done < <(tmux list-panes -t "$SESSION:$TL_WINDOW" -F '#{pane_id}' 2>/dev/null)
    return 1
}

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

    # --- The controller dispatched a Codex worker into its own window ---
    wait_for "worker routing metadata exists" \
        "[[ -f '$REPO_DIR/.exo/agents/$WORKER_AGENT/routing.json' ]]"
    wait_for "worker Codex config exists" "test -f '$(worker_config)'"
    check "Codex worker config is role-correct" \
        e2e_python_tl_assert_codex_child_config \
        "$(worker_config)" "worker" "worker" "$WORKER_AGENT" "$WORKER_PROTOCOL"
    check "Codex worker trust is in the isolated home" \
        e2e_python_tl_assert_codex_trust "$CODEX_HOME" "$(worker_config)"

    pane_count="$(tmux list-panes -t "$SESSION:$TL_WINDOW" 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$pane_count" -lt 2 ]]; then
        record_failure "expected the controller window to also hold a dispatched worker pane, found $pane_count pane(s)"
    else
        log "OK: controller window holds the controller plus a dispatched worker pane"
    fi

    # --- Delivery to the controller's window ---
    wait_for "worker notify_parent event recorded" \
        "grep -R '$MESSAGE_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "worker notify_parent tmux delivery succeeded" \
        "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '$WORKER_AGENT' | grep 'agent_inbox_tmux' | grep '\"outcome\":\"success\"' | grep -q ."
    wait_for "notification reached the controller window" marker_reached_controller_window

    # --- Durable controller state ---
    wait_for "controller reached a terminal phase" \
        "python3 -c \"import json, sys; sys.exit(0 if json.load(open('$(e2e_python_tl_run_state "$REPO_DIR")'))['fsm']['phase'] in ('tl_done', 'tl_parked', 'tl_failed') else 1)\""
    check "plan slice is present in the checkpoint" \
        e2e_python_tl_assert_slices "$REPO_DIR" "$PLAN_SLICE"
    check "controller reached the expected terminal phase" \
        e2e_python_tl_assert_phase "$REPO_DIR" "tl_done"

    {
        printf 'Python TL worker notify E2E validation completed at %s\n' "$(date -Iseconds)"
        printf 'Session: %s\n' "$SESSION"
        printf 'Repo: %s\n' "$REPO_DIR"
        printf 'Codex home: %s\n' "$CODEX_HOME"
        printf 'Controller window: %s\n' "$TL_WINDOW"
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
