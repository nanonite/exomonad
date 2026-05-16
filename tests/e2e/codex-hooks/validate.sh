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

tmux_contains_hook_review_prompt() {
    tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null \
        | grep "^$SESSION:" \
        | while IFS= read -r pane; do
            tmux capture-pane -p -t "$pane" -S -3000 2>/dev/null || true
        done \
        | grep -Eiq 'hooks need review|need review before they can run|run /hooks to review|review hooks before'
}

assert_no_hook_review_prompt() {
    if tmux_contains_hook_review_prompt; then
        record_failure "Codex displayed an interactive hook review prompt"
    else
        log "OK: no Codex hook review prompt observed"
    fi
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

    [[ ! -f "$(dirname "$config")/hooks.json" ]] \
        || record_failure "$label should not have per-agent hooks.json"
}

validate_shared_codex_hooks() {
    local config="${CODEX_HOME:?CODEX_HOME required}/config.toml"

    [[ ! -f "$config" ]] && record_failure "shared Codex config missing"
    contains "$config" '# BEGIN EXOMONAD CODEX HOOKS' \
        && record_failure "shared Codex config still contains legacy ExoMonad hooks block"
    contains "$config" 'exomonad hook pre-tool-use --runtime codex' \
        && record_failure "shared Codex config should not define PreToolUse command hooks"
    contains "$config" 'exomonad hook post-tool-use --runtime codex' \
        && record_failure "shared Codex config should not define PostToolUse command hooks"
    contains "$config" 'exomonad hook stop --runtime codex' \
        && record_failure "shared Codex config should not define Stop command hooks"

    return 0
}

validate_hook_trust_for_config() {
    local label="$1"
    local codex_config="$2"
    local user_config="${CODEX_HOME:?CODEX_HOME required}/config.toml"

    contains "$codex_config" 'hook pre-tool-use --runtime codex' \
        || record_failure "$label config missing PreToolUse command hook"
    contains "$codex_config" 'hook post-tool-use --runtime codex' \
        || record_failure "$label config missing PostToolUse command hook"
    contains "$codex_config" 'hook stop --runtime codex' \
        || record_failure "$label config missing Stop command hook"
    contains "$codex_config" 'timeout = 600' \
        || record_failure "$label config missing explicit timeout for trust hash stability"
    contains "$codex_config" 'async = false' \
        || record_failure "$label config missing explicit async=false for trust hash stability"

    if ! python3 - "$user_config" "$codex_config" <<'PY'; then
import sys
import tomllib

user_config_path, codex_config_path = sys.argv[1:3]
with open(user_config_path, "rb") as user_config_file:
    user_config = tomllib.load(user_config_file)

state = user_config.get("hooks", {}).get("state", {})
missing = []
for event in ("pre_tool_use", "post_tool_use", "stop"):
    key = f"{codex_config_path}:{event}:0:0"
    trusted_hash = state.get(key, {}).get("trusted_hash")
    if not isinstance(trusted_hash, str) or not trusted_hash.startswith("sha256:") or len(trusted_hash) != 71:
        missing.append(key)

if missing:
    print("\n".join(missing))
    raise SystemExit(1)
PY
        record_failure "$label config missing trusted hook state entries"
    fi
}

main() {
    log "waiting for shared Codex config"
    wait_for "shared Codex config exists" "[[ -f '${CODEX_HOME:?CODEX_HOME required}/config.toml' ]]"
    validate_shared_codex_hooks

    log "waiting for root Codex config"
    wait_for "root Codex config exists" "[[ -f '$REPO_DIR/.codex/config.toml' ]]"
    validate_codex_config "root" "$REPO_DIR/.codex/config.toml" "root" "root" "ExoMonad Root TL Protocol"
    validate_hook_trust_for_config "root" "$REPO_DIR/.codex/config.toml"
    wait_for_gh_command_blocked
    assert_no_hook_review_prompt

    wait_for_config_role "Codex TL worktree config exists" "tl"
    tl_config="$(find_config_by_role tl || true)"
    if [[ -z "$tl_config" ]]; then
        record_failure "could not locate TL config after wait"
    else
        tl_agent="$(basename "$(dirname "$(dirname "$tl_config")")")"
        validate_codex_config "tl" "$tl_config" "tl" "$tl_agent" "ExoMonad Root TL Protocol"
        validate_hook_trust_for_config "tl" "$tl_config"
    fi
    assert_no_hook_review_prompt

    wait_for_config_role "Codex dev worktree config exists" "dev"
    dev_config="$(find_config_by_role dev || true)"
    if [[ -z "$dev_config" ]]; then
        record_failure "could not locate dev config after wait"
    else
        dev_agent="$(basename "$(dirname "$(dirname "$dev_config")")")"
        validate_codex_config "dev" "$dev_config" "dev" "$dev_agent" "ExoMonad Dev Agent Protocol"
        validate_hook_trust_for_config "dev" "$dev_config"
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
        validate_hook_trust_for_config "reviewer" "$reviewer_config"
    fi

    wait_for "reviewer approval recorded" "[[ -f '$REPO_DIR/.exo/reviews/pr_1.json' ]] && grep -q 'approved' '$REPO_DIR/.exo/reviews/pr_1.json'"
    wait_for "reviewer notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'review-pr-1-codex' | grep 'main.codex-hooks-tl-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    assert_no_hook_review_prompt

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
