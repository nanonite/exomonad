#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-}"
SESSION="${2:-}"
RESULT_FILE="${3:-}"

TIMEOUT_SECONDS="${TL_TO_WORKER_E2E_TIMEOUT_SECONDS:-420}"
POLL_SECONDS=5

failures=()

log() {
    printf '[tl-to-worker-validator] %s
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

config_has_role() {
    local config="$1"
    local expected_role="$2"

    python3 - "$config" "$expected_role" <<'PY'
import sys
import tomllib

config_path, expected_role = sys.argv[1:3]
with open(config_path, "rb") as config_file:
    config = tomllib.load(config_file)

args = config.get("mcp_servers", {}).get("exomonad", {}).get("args", [])
try:
    role = args[args.index("--role") + 1]
except (ValueError, IndexError):
    raise SystemExit(1)

raise SystemExit(0 if role == expected_role else 1)
PY
}

find_codex_config_by_role() {
    local role="$1"
    find "$REPO_DIR/.exo/worktrees" -path '*/.codex/config.toml' -print 2>/dev/null         | while IFS= read -r config; do
            if config_has_role "$config" "$role"; then
                printf '%s
' "$config"
                break
            fi
        done
}

find_worker_opencode_config() {
    find "$REPO_DIR/.exo/agents" -path '*/opencode.json' -print 2>/dev/null         | while IFS= read -r config; do
            if python3 - "$config" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as config_file:
    config = json.load(config_file)

command = config.get("mcp", {}).get("exomonad", {}).get("command", [])
instructions = "\n".join(config.get("instructions", []))
expected_command = ["exomonad", "mcp-stdio", "--role", "worker", "--name", "tl-to-worker-oc-worker-opencode"]
if command == expected_command and "ExoMonad Worker Agent Protocol" in instructions:
    raise SystemExit(0)
raise SystemExit(1)
PY
            then
                printf '%s
' "$config"
                break
            fi
        done
}

worker_routing_path() {
    find "$REPO_DIR/.exo/agents" -path '*/routing.json' -print 2>/dev/null         | grep '/tl-to-worker-oc-worker-opencode/routing.json'         | head -n 1
}

worker_pane_id() {
    local routing
    routing="$(worker_routing_path || true)"
    [[ -n "$routing" ]] || return 1
    python3 - "$routing" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    value = json.load(f)
pane = value.get("pane_id")
if not pane:
    raise SystemExit(1)
print(pane)
PY
}

pane_contains_injected_message() {
    local pane
    pane="$(worker_pane_id)" || return 1
    tmux capture-pane -t "$pane" -p -S -200 2>/dev/null | grep -Fq '[TL2WORKER-INJECTED]'
}

main() {
    : "${REPO_DIR:?repo dir required}"
    : "${SESSION:?tmux session required}"
    : "${RESULT_FILE:?result file required}"

    wait_for "root Codex config exists" "bash '$0' --root-config '$REPO_DIR'"
    wait_for "Codex TL worktree config exists" "bash '$0' --has-tl-config '$REPO_DIR'"
    wait_for "OpenCode worker config exists" "bash '$0' --has-worker-config '$REPO_DIR'"

    tl_config="$(find_codex_config_by_role tl || true)"
    [[ -n "$tl_config" ]] || record_failure "could not locate Codex TL config"

    worker_config="$(find_worker_opencode_config || true)"
    if [[ -z "$worker_config" ]]; then
        record_failure "could not locate OpenCode worker config with worker role"
    elif ! grep -Fq 'ExoMonad Worker Agent Protocol' "$worker_config"; then
        record_failure "OpenCode worker config missing worker protocol instructions"
    fi

    wait_for "worker routing has pane_id" "bash '$0' --has-worker-pane '$REPO_DIR'"
    wait_for "worker pane contains injected TL message" "bash '$0' --pane-has-message '$REPO_DIR'"
    wait_for "send_tmux_message delivery succeeded" "bash '$0' --worker-delivery-success '$REPO_DIR'"
    wait_for "worker notify_parent acknowledgement recorded" "bash '$0' --worker-ack-log '$REPO_DIR'"
    wait_for "worker notify_parent tmux delivery succeeded" "bash '$0' --worker-notify-delivery-success '$REPO_DIR'"
    wait_for "TL notify_parent completion recorded" "bash '$0' --tl-done-log '$REPO_DIR'"

    {
        printf 'TL-to-worker messaging E2E validation completed at %s
' "$(date -Iseconds)"
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

case "${1:-}" in
    --root-config)
        REPO_DIR="${2:?repo dir required}"
        [[ -f "$REPO_DIR/.codex/config.toml" ]]
        exit 0
        ;;
    --has-tl-config)
        REPO_DIR="${2:?repo dir required}"
        find_codex_config_by_role tl | grep -q .
        exit 0
        ;;
    --has-worker-config)
        REPO_DIR="${2:?repo dir required}"
        find_worker_opencode_config | grep -q .
        exit 0
        ;;
    --has-worker-pane)
        REPO_DIR="${2:?repo dir required}"
        worker_pane_id >/dev/null
        exit 0
        ;;
    --worker-delivery-success)
        REPO_DIR="${2:?repo dir required}"
        grep -R 'message.delivery' "$REPO_DIR/.exo/logs" 2>/dev/null | grep 'recipient=tl-to-worker-oc-worker-opencode' | grep 'method="agent_inbox_tmux"' | grep 'outcome="success"' | grep -q .
        exit 0
        ;;
    --worker-ack-log)
        REPO_DIR="${2:?repo dir required}"
        grep -R 'TL2WORKER-WORKER-ACK' "$REPO_DIR/.exo/logs" 2>/dev/null | grep -q .
        exit 0
        ;;
    --worker-notify-delivery-success)
        REPO_DIR="${2:?repo dir required}"
        grep -R 'message.delivery' "$REPO_DIR/.exo/logs" 2>/dev/null | grep 'agent_id=tl-to-worker-oc-worker-opencode' | grep 'recipient=tl-to-worker-messaging-tl-codex' | grep 'method="agent_inbox_tmux"' | grep 'outcome="success"' | grep -q .
        exit 0
        ;;
    --tl-done-log)
        REPO_DIR="${2:?repo dir required}"
        grep -R 'TL2WORKER-TL-DONE' "$REPO_DIR/.exo/logs" 2>/dev/null | grep -q .
        exit 0
        ;;
    --find-tl)
        REPO_DIR="${2:?repo dir required}"
        find_codex_config_by_role tl
        exit 0
        ;;
    --worker-pane)
        REPO_DIR="${2:?repo dir required}"
        worker_pane_id
        exit 0
        ;;
    --pane-has-message)
        REPO_DIR="${2:?repo dir required}"
        pane_contains_injected_message
        exit 0
        ;;
esac

main "$@"
