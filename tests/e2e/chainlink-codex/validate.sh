#!/usr/bin/env bash
set -euo pipefail

TIMEOUT_SECONDS="${CHAINLINK_CODEX_E2E_TIMEOUT_SECONDS:-480}"
POLL_SECONDS=5

failures=()

log() {
    printf '[chainlink-codex-validator] %s\n' "$*"
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

config_has_mcp_arg() {
    local config="$1"
    local expected_role="$2"
    local expected_name="$3"

    python3 - "$config" "$expected_role" "$expected_name" <<'PY'
import sys
import tomllib

config_path, expected_role, expected_name = sys.argv[1:4]
with open(config_path, "rb") as config_file:
    config = tomllib.load(config_file)

args = config.get("mcp_servers", {}).get("exomonad", {}).get("args", [])
expected = ["mcp-stdio", "--role", expected_role, "--name", expected_name]
raise SystemExit(0 if args == expected else 1)
PY
}

find_worktree_config_by_role() {
    local role="$1"
    find "$REPO_DIR/.exo/worktrees" -path '*/.codex/config.toml' -print 2>/dev/null \
        | while IFS= read -r config; do
            if config_has_role "$config" "$role"; then
                printf '%s\n' "$config"
                break
            fi
        done
}

find_agent_config_by_role() {
    local role="$1"
    find "$REPO_DIR/.exo/agents" -path '*/.codex/config.toml' -print 2>/dev/null \
        | while IFS= read -r config; do
            if config_has_role "$config" "$role"; then
                printf '%s\n' "$config"
                break
            fi
        done
}

if [[ "${1:-}" == "--find-tl" ]]; then
    REPO_DIR="${2:?repo dir required}"
    find_worktree_config_by_role tl
    exit 0
fi

if [[ "${1:-}" == "--find-worker" ]]; then
    REPO_DIR="${2:?repo dir required}"
    find_agent_config_by_role worker
    exit 0
fi

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"

validate_codex_config() {
    local label="$1"
    local config="$2"
    local role="$3"
    local agent_name="$4"
    local instruction_marker="$5"
    local hooks_file

    hooks_file="$(dirname "$config")/hooks.json"

    grep -Fq 'approval_policy = "never"' "$config" \
        || record_failure "$label config missing approval_policy"
    grep -Fq 'hooks = true' "$config" \
        || record_failure "$label config missing hooks feature"
    grep -Fq '"mcp-stdio"' "$config" \
        || record_failure "$label config missing mcp-stdio"
    config_has_mcp_arg "$config" "$role" "$agent_name" \
        || record_failure "$label config missing MCP identity role=$role name=$agent_name"
    grep -Fq "$instruction_marker" "$config" \
        || record_failure "$label config missing instruction marker"

    grep -Fq 'exomonad hook pre-tool-use --runtime codex' "$hooks_file" \
        || record_failure "$label hooks missing PreToolUse command"
    grep -Fq 'exomonad hook post-tool-use --runtime codex' "$hooks_file" \
        || record_failure "$label hooks missing PostToolUse command"
    grep -Fq 'exomonad hook stop --runtime codex' "$hooks_file" \
        || record_failure "$label hooks missing Stop command"
}

main() {
    wait_for "root Codex config exists" "[[ -f '$REPO_DIR/.codex/config.toml' ]]"
    validate_codex_config "root" "$REPO_DIR/.codex/config.toml" "root" "root" "ExoMonad Root TL Protocol"

    wait_for "Codex TL worktree config exists" "[[ -n \"\$(find '$REPO_DIR/.exo/worktrees' -path '*/.codex/config.toml' -print 2>/dev/null)\" ]] && [[ -n \"\$(bash '$0' --find-tl '$REPO_DIR')\" ]]"
    tl_config="$(find_worktree_config_by_role tl || true)"
    if [[ -z "$tl_config" ]]; then
        record_failure "could not locate TL config after wait"
    else
        tl_agent="$(basename "$(dirname "$(dirname "$tl_config")")")"
        validate_codex_config "tl" "$tl_config" "tl" "$tl_agent" "ExoMonad Root TL Protocol"
    fi

    wait_for "Codex worker agent config exists" "[[ -n \"\$(bash '$0' --find-worker '$REPO_DIR')\" ]]"
    worker_config="$(find_agent_config_by_role worker || true)"
    if [[ -z "$worker_config" ]]; then
        record_failure "could not locate worker config after wait"
    else
        worker_agent="$(basename "$(dirname "$(dirname "$worker_config")")")"
        validate_codex_config "worker" "$worker_config" "worker" "$worker_agent" "ExoMonad Worker Agent Protocol"
    fi

    wait_for "worker Chainlink comment recorded" "grep -R 'CHAINLINK-CODEX-WORKER-COMMENT' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q . || (cd '$REPO_DIR' && chainlink list --json --status all | grep -Fq 'CHAINLINK-CODEX-WORKER-COMMENT')"
    wait_for "worker Chainlink issue closed" "cd '$REPO_DIR' && chainlink list --json --status closed | grep -Fq 'E2E chainlink codex worker'"
    wait_for "worker session completion notification recorded" "grep -R 'CHAINLINK-CODEX-WORKER-DONE' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "TL Chainlink issue close recorded" "grep -R 'CHAINLINK-CODEX-TL-CLOSE' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q . || (cd '$REPO_DIR' && chainlink list --json --status closed | grep -Fq 'E2E chainlink codex worker')"
    wait_for "worker notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'chainlink-codex-worker-codex' | grep 'main.chainlink-codex-tl-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    wait_for "Chainlink lock worktree not created" "cd '$REPO_DIR' && ! git worktree list --porcelain | grep -Fq '.chainlink/.locks-cache' && [[ ! -e '$REPO_DIR/.chainlink/.locks-cache' ]]"

    {
        printf 'Chainlink Codex E2E validation completed at %s\n' "$(date -Iseconds)"
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
