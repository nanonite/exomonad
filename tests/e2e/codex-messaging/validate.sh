#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"

TIMEOUT_SECONDS="${CODEX_MESSAGING_E2E_TIMEOUT_SECONDS:-360}"
POLL_SECONDS=5

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

find_config_by_role() {
    local role="$1"
    find "$REPO_DIR/.exo/worktrees" -path '*/.codex/config.toml' -print 2>/dev/null \
        | while IFS= read -r config; do
            if config_has_role "$config" "$role"; then
                printf '%s\n' "$config"
                break
            fi
        done
}

wait_for_config_role() {
    local label="$1"
    local role="$2"
    local deadline=$((SECONDS + TIMEOUT_SECONDS))

    while (( SECONDS < deadline )); do
        if [[ -n "$(find_config_by_role "$role")" ]]; then
            log "OK: $label"
            return 0
        fi
        sleep "$POLL_SECONDS"
    done

    record_failure "$label timed out after ${TIMEOUT_SECONDS}s"
    return 1
}

validate_root_config() {
    if [[ ! -f "$REPO_DIR/.codex/config.toml" ]]; then
        record_failure "root Codex config missing"
        return
    fi

    grep -Fq 'approval_policy = "never"' "$REPO_DIR/.codex/config.toml" \
        || record_failure "root config missing approval_policy"
    grep -Fq '"mcp-stdio"' "$REPO_DIR/.codex/config.toml" \
        || record_failure "root config missing mcp-stdio"
    grep -Fq 'ExoMonad Root TL Protocol' "$REPO_DIR/.codex/config.toml" \
        || record_failure "root config missing root protocol marker"
}

main() {
    wait_for "root Codex config exists" "[[ -f '$REPO_DIR/.codex/config.toml' ]]"
    validate_root_config

    wait_for_config_role "Codex TL worktree config exists" "tl"
    wait_for_config_role "Codex dev worktree config exists" "dev"

    wait_for "send_message agent event recorded" "grep -R 'agent.message_sent' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'codex-messaging-dev-codex' | grep 'success=true' | grep -q ."
    wait_for "send_message tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'codex-messaging-dev-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    wait_for "TL notify_parent event recorded" "grep -R 'CODEX-MSG-TL-NOTIFY' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "TL notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'codex-messaging-tl-codex' | grep 'main' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    wait_for "dev notify_parent event recorded" "grep -R 'CODEX-MSG-DEV-NOTIFY' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "dev notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'codex-messaging-dev-codex' | grep 'main.codex-messaging-tl-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."

    {
        printf 'Codex messaging E2E validation completed at %s\n' "$(date -Iseconds)"
        printf 'Session: %s\n' "$SESSION"
        printf 'Repo: %s\n' "$REPO_DIR"
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
