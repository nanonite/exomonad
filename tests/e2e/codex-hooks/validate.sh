#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"

TIMEOUT_SECONDS="${CODEX_HOOKS_E2E_TIMEOUT_SECONDS:-420}"
POLL_SECONDS=5

failures=()

log() {
    printf '[codex-hooks-validator] %s\n' "$*"
}

record_failure() {
    failures+=("$*")
    log "FAIL: $*"
}

contains() {
    local file="$1"
    local needle="$2"
    [[ -f "$file" ]] && grep -Fq "$needle" "$file"
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

logs_contain() {
    local pattern="$1"
    grep -R "$pattern" "$REPO_DIR/.exo/logs" 2>/dev/null | grep -q .
}

codex_gh_command_is_blocked() {
    local payload
    local output
    payload='{"session_id":"codex-hooks-validator","hook_event_name":"PreToolUse","tool":"bash","args":{"command":"gh auth status"}}'

    output="$(
        cd "$REPO_DIR" && {
            printf '%s' "$payload" | \
                EXOMONAD_ROLE=root \
                EXOMONAD_AGENT_ID=root \
                EXOMONAD_SESSION_ID=main \
                exomonad hook pre-tool-use --runtime codex 2>/dev/null
        } || true
    )"

    grep -Fq '"permissionDecision":"deny"' <<<"$output" \
        && grep -Fq 'Do not run gh commands' <<<"$output"
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

wait_for_gh_command_blocked() {
    local deadline=$((SECONDS + TIMEOUT_SECONDS))

    while (( SECONDS < deadline )); do
        if codex_gh_command_is_blocked; then
            log "OK: Codex gh command hook blocks gh auth status"
            return 0
        fi
        sleep "$POLL_SECONDS"
    done

    record_failure "Codex gh command hook blocks gh auth status timed out after ${TIMEOUT_SECONDS}s"
    return 1
}

validate_codex_config() {
    local label="$1"
    local config="$2"
    local role="$3"
    local agent_name="$4"
    local instruction_marker="$5"
    local hooks_file

    hooks_file="$(dirname "$config")/hooks.json"

    contains "$config" 'approval_policy = "never"' \
        || record_failure "$label config missing approval_policy"
    contains "$config" 'hooks = true' \
        || record_failure "$label config missing hooks feature"
    contains "$config" '"mcp-stdio"' \
        || record_failure "$label config missing mcp-stdio"
    config_has_mcp_arg "$config" "$role" "$agent_name" \
        || record_failure "$label config missing MCP identity role=$role name=$agent_name"
    contains "$config" "$instruction_marker" \
        || record_failure "$label config missing instruction marker"

    contains "$hooks_file" 'exomonad hook pre-tool-use --runtime codex' \
        || record_failure "$label hooks missing PreToolUse command"
    contains "$hooks_file" 'exomonad hook post-tool-use --runtime codex' \
        || record_failure "$label hooks missing PostToolUse command"
    contains "$hooks_file" 'exomonad hook stop --runtime codex' \
        || record_failure "$label hooks missing Stop command"
}

main() {
    log "waiting for root Codex config"
    wait_for "root Codex config exists" "[[ -f '$REPO_DIR/.codex/config.toml' ]]"
    validate_codex_config "root" "$REPO_DIR/.codex/config.toml" "root" "root" "ExoMonad Root TL Protocol"
    wait_for_gh_command_blocked

    wait_for_config_role "Codex TL worktree config exists" "tl"
    tl_config="$(find_config_by_role tl || true)"
    if [[ -z "$tl_config" ]]; then
        record_failure "could not locate TL config after wait"
    else
        tl_agent="$(basename "$(dirname "$(dirname "$tl_config")")")"
        validate_codex_config "tl" "$tl_config" "tl" "$tl_agent" "ExoMonad Root TL Protocol"
    fi

    wait_for_config_role "Codex dev worktree config exists" "dev"
    dev_config="$(find_config_by_role dev || true)"
    if [[ -z "$dev_config" ]]; then
        record_failure "could not locate dev config after wait"
    else
        dev_agent="$(basename "$(dirname "$(dirname "$dev_config")")")"
        validate_codex_config "dev" "$dev_config" "dev" "$dev_agent" "ExoMonad Dev Agent Protocol"
    fi

    wait_for "dev output file exists" "find '$REPO_DIR/.exo/worktrees' -name codex-hooks-dev-output.txt -print 2>/dev/null | grep -q ."
    if dev_output="$(find "$REPO_DIR/.exo/worktrees" -name codex-hooks-dev-output.txt -print -quit 2>/dev/null)"; then
        if [[ -n "$dev_output" ]]; then
            grep -Fxq 'Codex dev hook test passed' "$dev_output" \
                || record_failure "dev output content mismatch"
        fi
    fi

    wait_for "dev notify_parent event recorded" "grep -R 'CODEX-HOOKS-DEV-DONE' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "dev notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'codex-hooks-dev-codex' | grep 'main.codex-hooks-tl-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."

    wait_for "local PR registry created" "[[ -f '$REPO_DIR/.exo/prs.json' ]] && grep -q 'codex-hooks' '$REPO_DIR/.exo/prs.json'"

    wait_for_config_role "Codex reviewer worktree config exists" "reviewer"
    reviewer_config="$(find_config_by_role reviewer || true)"
    if [[ -z "$reviewer_config" ]]; then
        record_failure "could not locate reviewer config after wait"
    else
        reviewer_agent="$(basename "$(dirname "$(dirname "$reviewer_config")")")"
        validate_codex_config "reviewer" "$reviewer_config" "reviewer" "$reviewer_agent" "ExoMonad Reviewer Agent Protocol"
    fi

    wait_for "reviewer approval recorded" "[[ -f '$REPO_DIR/.exo/reviews/pr_1.json' ]] && grep -q 'approved' '$REPO_DIR/.exo/reviews/pr_1.json'"
    wait_for "reviewer notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'review-pr-1-codex' | grep 'main.codex-hooks-tl-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."

    {
        printf 'Codex hooks E2E validation completed at %s\n' "$(date -Iseconds)"
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
