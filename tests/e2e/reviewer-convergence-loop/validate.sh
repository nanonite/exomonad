#!/usr/bin/env bash
# Process companion for the reviewer-convergence-loop E2E test.
#
# Watches for the testrunner's verdict signal, then independently validates
# objective convergence and MCP transport evidence before writing RESULT_FILE.
# The harness's run.sh inspects RESULT_FILE for "Failures: 0" to set its exit
# code.
#
# Args (positional):
#   $1  REPO_DIR     — the workspace root (so we can locate .exo/)
#   $2  SESSION      — tmux session name (for diagnostic dumps)
#   $3  RESULT_FILE  — file we must write the verdict to
#   $4  SERVER_LOG   — exomonad server log path (also dumped on failure)
#
# Lifecycle: runs from `exomonad init` as a process companion. Polls
# until either the marker file is present or the configured timeout
# elapses. Writes RESULT_FILE then exits 0 regardless of verdict — the
# verdict is communicated through RESULT_FILE, not through this
# script's exit code.

set -u  # -e intentionally OFF: we want to always write RESULT_FILE.

REPO_DIR="${1:?REPO_DIR required}"
SESSION="${2:?SESSION required}"
RESULT_FILE="${3:?RESULT_FILE required}"
SERVER_LOG="${4:-}"

MARKER_DIR="$REPO_DIR/.exo/e2e-reviewer-convergence"
SUCCESS_MARKER="$MARKER_DIR/success"
FAILURE_MARKER="$MARKER_DIR/failure"
TIMEOUT_SECS="${E2E_REVIEWER_TIMEOUT:-1500}"  # 25 min default
POLL_SECS=5
START=$(date +%s)
FAILURES=()
EVIDENCE=()

mkdir -p "$MARKER_DIR"

log() {
    printf '[validate.sh] %s\n' "$*"
}

record_failure() {
    FAILURES+=("$*")
    log "FAIL: $*"
}

record_evidence() {
    EVIDENCE+=("$*")
    log "OK: $*"
}

contains_fixed() {
    local file="$1"
    local needle="$2"

    [[ -f "$file" ]] && grep -Fq "$needle" "$file" 2>/dev/null
}

grep_fixed_tree() {
    local root="$1"
    local needle="$2"

    [[ -d "$root" ]] && grep -R -F "$needle" "$root" 2>/dev/null
}

capture_session_panes() {
    tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null \
        | while IFS= read -r pane; do
            case "$pane" in
                "$SESSION":*)
                    printf -- '--- pane %s ---\n' "$pane"
                    tmux capture-pane -p -t "$pane" -S -5000 2>/dev/null || true
                    ;;
            esac
        done
}

pr_field() {
    local field="$1"

    python3 - "$REPO_DIR/.exo/prs.json" "$field" <<'PY' 2>/dev/null
import json
import sys

path, field = sys.argv[1:3]
with open(path, "r", encoding="utf-8") as handle:
    prs = json.load(handle)

if isinstance(prs, dict):
    entries = prs.get("prs") or prs.get("pull_requests") or prs.get("entries") or []
    if isinstance(entries, dict):
        entries = [entries[key] for key in sorted(entries, key=lambda item: int(item) if str(item).isdigit() else str(item))]
elif isinstance(prs, list):
    entries = prs
else:
    entries = []

if entries:
    value = entries[0].get(field, "")
    if value is not None:
        print(value)
PY
}

pr_number() {
    local number

    number="$(pr_field number)"
    [[ -n "$number" ]] && printf '%s\n' "$number" || printf '1\n'
}

config_identity() {
    local config="$1"

    python3 - "$config" <<'PY' 2>/dev/null
import sys
import tomllib

with open(sys.argv[1], "rb") as config_file:
    config = tomllib.load(config_file)

args = config.get("mcp_servers", {}).get("exomonad", {}).get("args", [])
try:
    role = args[args.index("--role") + 1]
    name = args[args.index("--name") + 1]
except (ValueError, IndexError):
    raise SystemExit(1)

if args[:1] == ["mcp-stdio"]:
    print(f"{role}\t{name}\t{sys.argv[1]}")
PY
}

mcp_process_running() {
    local role="$1"
    local name="$2"

    pgrep -af "mcp-stdio.*--role[ =]${role}.*--name[ =]${name}" >/dev/null 2>&1 \
        || pgrep -af "mcp-stdio.*--name[ =]${name}.*--role[ =]${role}" >/dev/null 2>&1
}

mcp_initialize_logged() {
    local role="$1"
    local name="$2"
    local logs="$REPO_DIR/.exo/logs"

    {
        grep_fixed_tree "$logs" "method=initialize" || true
        grep_fixed_tree "$logs" '"method":"initialize"' || true
        [[ -n "$SERVER_LOG" && -f "$SERVER_LOG" ]] && grep -F "initialize" "$SERVER_LOG" 2>/dev/null || true
    } | grep -F "mcp_stdio" | grep -F "role=\"$role\"" | grep -F "agent=\"$name\"" >/dev/null 2>&1
}

validate_mcp_stdio_evidence() {
    local found=0
    local identity role name config

    if [[ ! -d "$REPO_DIR/.exo/worktrees" ]]; then
        record_failure "missing .exo/worktrees directory for spawned Codex agent configs"
        return
    fi

    while IFS= read -r config; do
        identity="$(config_identity "$config" || true)"
        [[ -z "$identity" ]] && continue
        role="$(printf '%s' "$identity" | awk -F '\t' '{print $1}')"
        name="$(printf '%s' "$identity" | awk -F '\t' '{print $2}')"
        found=$((found + 1))

        if mcp_process_running "$role" "$name"; then
            record_evidence "mcp-stdio process running for role=$role name=$name config=$config"
        elif mcp_initialize_logged "$role" "$name"; then
            record_evidence "mcp-stdio initialize logged for role=$role name=$name config=$config"
        else
            record_failure "no mcp-stdio process or initialize log for role=$role name=$name config=$config"
        fi
    done <<EOF
$(find "$REPO_DIR/.exo/worktrees" -path '*/.codex/config.toml' -print 2>/dev/null)
EOF

    if (( found == 0 )); then
        record_failure "no spawned Codex agent configs found under .exo/worktrees"
    fi
}

validate_convergence_evidence() {
    local pr reviewer review_file

    if [[ ! -f "$REPO_DIR/.exo/prs.json" ]]; then
        record_failure "missing .exo/prs.json"
        return
    fi

    pr="$(pr_number)"
    reviewer="$(pr_field reviewer_agent)"
    if [[ -z "$reviewer" ]]; then
        record_failure "PR #$pr missing reviewer_agent in .exo/prs.json"
    else
        record_evidence "PR #$pr reviewer_agent=$reviewer"
    fi

    review_file="$REPO_DIR/.exo/reviews/pr_${pr}.json"
    if contains_fixed "$review_file" "changes_requested"; then
        record_evidence "review file has changes_requested: $review_file"
    else
        record_failure "review file missing changes_requested evidence: $review_file"
    fi

    if [[ -n "$SERVER_LOG" && -f "$SERVER_LOG" ]]; then
        grep -F "Fanning out pr_review event to reviewer agent" "$SERVER_LOG" 2>/dev/null \
            | grep -F "kind=fixes_pushed" >/dev/null 2>&1 \
            && record_evidence "server log has fixes_pushed reviewer fan-out" \
            || record_failure "server log missing fixes_pushed reviewer fan-out"

        grep -F "[EventDispatch] Calling handle_event for agent 'review-pr-${pr}" "$SERVER_LOG" >/dev/null 2>&1 \
            && record_evidence "server log has reviewer handle_event call for review-pr-${pr}" \
            || record_failure "server log missing reviewer handle_event call for review-pr-${pr}"

        grep -F "[EventDispatch] handle_event returned" "$SERVER_LOG" >/dev/null 2>&1 \
            && record_evidence "server log has handle_event returned" \
            || record_failure "server log missing handle_event returned"

        grep -F "No plugin found for event target" "$SERVER_LOG" 2>/dev/null | grep -F "review-pr-${pr}" >/dev/null 2>&1 \
            && record_failure "server log contains No plugin found for event target review-pr-${pr}"
        grep -F "pr_review event fired but no reviewer is registered" "$SERVER_LOG" 2>/dev/null | grep -F "PR #${pr}" >/dev/null 2>&1 \
            && record_failure "server log says pr_review event fired without reviewer for PR #${pr}"
    else
        record_failure "server log missing at $SERVER_LOG"
    fi
}

detect_uds_side_channel() {
    local evidence

    evidence="$(
        {
            capture_session_panes
            grep_fixed_tree "$REPO_DIR/.exo/logs" "curl --unix-socket" || true
            [[ -n "$SERVER_LOG" && -f "$SERVER_LOG" ]] && grep -F 'curl --unix-socket' "$SERVER_LOG" 2>/dev/null || true
        } | grep -E '(^|[;&|({[:space:]])curl[[:space:]][^`]*--unix-socket' \
            | grep -Ev 'No `curl --unix-socket|Never call server endpoints|Hard Rules' \
            | head -n 20
    )"

    if [[ -n "$evidence" ]]; then
        record_failure "UDS curl side-channel evidence detected: $(printf '%s' "$evidence" | tr '\n' ' ' | cut -c1-500)"
    else
        record_evidence "no UDS curl side-channel evidence found in tmux panes or .exo logs"
    fi
}

run_success_assertions() {
    validate_convergence_evidence
    validate_mcp_stdio_evidence
    detect_uds_side_channel
}

write_result() {
    local verdict="$1"
    local reason="${2:-}"
    local effective_verdict="$verdict"

    if (( ${#FAILURES[@]} > 0 )); then
        effective_verdict="failure"
    fi

    {
        echo "Verdict: $effective_verdict"
        echo "Failures: $([[ "$effective_verdict" == "success" ]] && echo 0 || echo 1)"
        echo "Reason: $reason"
        if (( ${#EVIDENCE[@]} > 0 )); then
            echo "Evidence:"
            for item in "${EVIDENCE[@]}"; do
                printf '  - %s\n' "$item"
            done
        fi
        if (( ${#FAILURES[@]} > 0 )); then
            echo "Validator failures:"
            for failure in "${FAILURES[@]}"; do
                printf '  - %s\n' "$failure"
            done
        fi
        if [[ -f "$SUCCESS_MARKER" ]]; then
            echo "Success marker content:"
            sed 's/^/  /' "$SUCCESS_MARKER"
        fi
        if [[ -f "$FAILURE_MARKER" ]]; then
            echo "Failure marker content:"
            sed 's/^/  /' "$FAILURE_MARKER"
        fi
        if [[ "$effective_verdict" != "success" && -n "$SERVER_LOG" && -f "$SERVER_LOG" ]]; then
            echo "Last 100 server log lines:"
            tail -n 100 "$SERVER_LOG" | sed 's/^/  /'
        fi
    } > "$RESULT_FILE"
}

log "watching $MARKER_DIR (timeout ${TIMEOUT_SECS}s, poll ${POLL_SECS}s)"

while true; do
    if [[ -f "$SUCCESS_MARKER" ]]; then
        log "success marker observed; running objective validator assertions"
        run_success_assertions
        write_result success "testrunner reported success; validate.sh checked convergence, MCP stdio evidence, and UDS side-channel absence"
        exit 0
    fi
    if [[ -f "$FAILURE_MARKER" ]]; then
        log "failure marker observed"
        detect_uds_side_channel
        write_result failure "testrunner reported convergence-loop FAILED — see marker content + server log"
        exit 0
    fi

    NOW=$(date +%s)
    if (( NOW - START >= TIMEOUT_SECS )); then
        log "timed out after ${TIMEOUT_SECS}s"
        detect_uds_side_channel
        write_result failure "validator timed out after ${TIMEOUT_SECS}s — testrunner never wrote success/failure marker"
        exit 0
    fi

    sleep "$POLL_SECS"
done
