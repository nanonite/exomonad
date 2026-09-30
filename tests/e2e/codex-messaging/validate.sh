#!/usr/bin/env bash
set -euo pipefail

# Validator for the Python-TL-driven Codex messaging scenario.
#
# Everything asserted here belongs to a dispatched Codex child, a Codex
# companion, or the controller's own durable checkpoint. The retired
# interactive Codex root TL is asserted to be absent, because normal
# Python-controller startup provisions no Codex agent in the project root.

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"
CODEX_HOME="${4:?isolated codex home required}"

TIMEOUT_SECONDS="${CODEX_MESSAGING_E2E_TIMEOUT_SECONDS:-600}"
POLL_SECONDS=5
TL_WINDOW="TL"
SEND_MARKER="[CODEX-MSG-WORKER-TO-PEER]"
NOTIFY_MARKER="[CODEX-MSG-SENDER-DONE]"
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
# The peer is a companion declared in .exo/config.toml, not a plan slice,
# so its agent identity is exactly its configured name.
PEER_AGENT="codex-messaging-peer"

failures=()

log() {
    printf '[codex-messaging-validator] %s\n' "$*"
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

agent_config() {
    printf '%s/.exo/agents/%s/.codex/config.toml\n' "$REPO_DIR" "$1"
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

    # --- The dispatched Codex child and the Codex peer it messages ---
    # Both are provisioned through provision_codex_agent, so both must carry the
    # same role-correct config shape and both must have trust in the isolated
    # home; a config written by a superseded path would only land for one of
    # them.
    wait_for "dispatched worker Codex config exists" "test -f '$(agent_config "$WORKER_AGENT")'"
    wait_for "Codex peer companion config exists" "test -f '$(agent_config "$PEER_AGENT")'"
    check "dispatched worker Codex config is role-correct" \
        e2e_python_tl_assert_codex_child_config \
        "$(agent_config "$WORKER_AGENT")" "worker" "worker" "$WORKER_AGENT" "$WORKER_PROTOCOL"
    check "Codex peer companion config is role-correct" \
        e2e_python_tl_assert_codex_child_config \
        "$(agent_config "$PEER_AGENT")" "peer companion" "worker" "$PEER_AGENT" "$WORKER_PROTOCOL"

    check "dispatched worker Codex trust is in the isolated home" \
        e2e_python_tl_assert_codex_trust "$CODEX_HOME" "$(agent_config "$WORKER_AGENT")"
    check "Codex peer companion trust is in the isolated home" \
        e2e_python_tl_assert_codex_trust "$CODEX_HOME" "$(agent_config "$PEER_AGENT")"

    # --- Messaging: the peer, then the controller ---
    wait_for "worker send_tmux_message recorded the marker" \
        "grep -R '$SEND_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "send_tmux_message reached the Codex peer" \
        "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '\"recipient\":\"$PEER_AGENT\"' | grep '\"outcome\":\"success\"' | grep -q ."
    wait_for "worker notify_parent recorded the marker" \
        "grep -R '$NOTIFY_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "notify_parent reached the controller" \
        "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '\"recipient\":\"root\"' | grep '\"outcome\":\"success\"' | grep -q ."

    # --- Durable controller state ---
    wait_for "controller reached a terminal phase" \
        "python3 -c \"import json, sys; sys.exit(0 if json.load(open('$(e2e_python_tl_run_state "$REPO_DIR")'))['fsm']['phase'] in ('tl_done', 'tl_parked', 'tl_failed') else 1)\""
    check "plan slice is present in the checkpoint" \
        e2e_python_tl_assert_slices "$REPO_DIR" "$PLAN_SLICE"
    check "controller reached the expected terminal phase" \
        e2e_python_tl_assert_phase "$REPO_DIR" "tl_done"

    {
        printf 'Codex messaging E2E validation completed at %s\n' "$(date -Iseconds)"
        printf 'Session: %s\n' "$SESSION"
        printf 'Repo: %s\n' "$REPO_DIR"
        printf 'Codex home: %s\n' "$CODEX_HOME"
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
