#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-}"
SESSION="${2:-}"
RESULT_FILE="${3:-}"
MOCK_LOG="${4:-}"
TEAM_BASELINE="${5:-}"

TIMEOUT_SECONDS="${CLAUDE_TEAMS_E2E_TIMEOUT_SECONDS:-900}"
POLL_SECONDS=5
DEV_AGENT="teams-inbox-dev-claude"
REVIEWER_AGENT="review-pr-1-claude"
MERGE_MARKER="[MERGE READY]"
failures=()
SNAPSHOT_DIR=""
SETTINGS_MONITOR_PID=""

copy_settings_snapshot() {
    local agent="$1"
    local name="$2"
    local source="$REPO_DIR/.exo/worktrees/$agent/.claude/settings.local.json"
    local dest="$SNAPSHOT_DIR/$name-settings.local.json"
    if [[ -f "$source" && ! -f "$dest" ]]; then
        cp "$source" "$dest" 2>/dev/null || true
    fi
}

start_settings_snapshot_monitor() {
    mkdir -p "$SNAPSHOT_DIR"
    (
        while true; do
            copy_settings_snapshot "$DEV_AGENT" dev
            copy_settings_snapshot "$REVIEWER_AGENT" reviewer
            sleep 0.5
        done
    ) &
    SETTINGS_MONITOR_PID="$!"
}

stop_settings_snapshot_monitor() {
    if [[ -n "$SETTINGS_MONITOR_PID" ]]; then
        kill "$SETTINGS_MONITOR_PID" 2>/dev/null || true
        wait "$SETTINGS_MONITOR_PID" 2>/dev/null || true
    fi
}

log() {
    printf '[claude-teams-validator] %s\n' "$*"
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
        if bash -lc "$command"; then
            log "OK: $label"
            return 0
        fi
        sleep "$POLL_SECONDS"
    done

    record_failure "$label timed out after ${TIMEOUT_SECONDS}s"
    return 1
}

new_team_dirs() {
    local current
    current="$(mktemp)"
    find "$HOME/.claude/teams" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort > "$current"
    comm -13 "$TEAM_BASELINE" "$current" 2>/dev/null || true
    rm -f "$current"
}

new_team_inboxes_have_merge_ready() {
    local team
    while IFS= read -r team; do
        [[ -n "$team" ]] || continue
        if grep -R "$MERGE_MARKER" "$HOME/.claude/teams/$team/inboxes" 2>/dev/null | grep -q .; then
            return 0
        fi
    done < <(new_team_dirs)
    return 1
}

mock_log_has() {
    local method="$1"
    local path_regex="$2"
    python3 - "$MOCK_LOG" "$method" "$path_regex" <<'PY'
import json
import re
import sys

log_path, method, path_regex = sys.argv[1:4]
pattern = re.compile(path_regex)
try:
    with open(log_path, 'r', encoding='utf-8') as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('method') == method and pattern.search(entry.get('path', '')):
                raise SystemExit(0)
except FileNotFoundError:
    pass
raise SystemExit(1)
PY
}

hook_command_has_fallback() {
    local settings="$1"
    python3 - "$settings" <<'PY'
import json
import sys
from pathlib import Path

settings = json.loads(Path(sys.argv[1]).read_text())
commands = []
for entries in settings.get('hooks', {}).values():
    for entry in entries:
        for hook in entry.get('hooks', []):
            command = hook.get('command', '')
            if 'hook ' in command:
                commands.append(command)
if not commands:
    raise SystemExit(1)
if not all('sh -lc' in command and 'exec exomonad hook' in command for command in commands):
    raise SystemExit(1)
PY
}

main() {
    REPO_DIR="${1:?repo dir required}"
    SESSION="${2:?tmux session required}"
    RESULT_FILE="${3:?result file required}"
    MOCK_LOG="${4:?mock log required}"
    TEAM_BASELINE="${5:?team baseline file required}"
    SNAPSHOT_DIR="$(dirname "$RESULT_FILE")/validator-snapshots"
    start_settings_snapshot_monitor
    trap stop_settings_snapshot_monitor EXIT

    wait_for "TeamCreate registered a Claude team" "tmux capture-pane -p -t '$SESSION:Server' 2>/dev/null | grep -q 'Registered team:'"
    wait_for "dev leaf Claude hook settings exist" "test -f '$REPO_DIR/.exo/worktrees/$DEV_AGENT/.claude/settings.local.json' || test -f '$SNAPSHOT_DIR/dev-settings.local.json' || grep -R 'Created tmux window' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -Fq '$DEV_AGENT'"
    wait_for "dev leaf tmux window exists" "tmux list-windows -t '$SESSION' -F '#{window_name}' 2>/dev/null | grep -Fq '$DEV_AGENT' || grep -R 'Created tmux window' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -Fq '$DEV_AGENT'"
    wait_for "dev leaf filed PR through mock Forgejo" "bash '$0' --mock-has '$MOCK_LOG' POST '/api/v1/repos/.*/pulls$'"
    wait_for "reviewer Claude hook settings exist" "test -f '$REPO_DIR/.exo/worktrees/$REVIEWER_AGENT/.claude/settings.local.json' || test -f '$SNAPSHOT_DIR/reviewer-settings.local.json'"
    wait_for "reviewer tmux window exists" "tmux list-windows -t '$SESSION' -F '#{window_name}' 2>/dev/null | grep -Fq '$REVIEWER_AGENT' || grep -R 'Created tmux window' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -Fq '$REVIEWER_AGENT'"
    wait_for "reviewer submitted Forgejo review" "bash '$0' --mock-has '$MOCK_LOG' POST '/api/v1/repos/.*/pulls/1/reviews$'"
    wait_for "watcher observed merge-ready state" "grep -R '$MERGE_MARKER' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -q ."
    wait_for "Teams inbox delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'method="teams_inbox"' | grep 'outcome="success"' | grep -q ."
    wait_for "merge-ready message landed in a new Teams inbox" "bash '$0' --new-inbox-has-merge-ready '$REPO_DIR' '$TEAM_BASELINE'"

    reviewer_settings="$REPO_DIR/.exo/worktrees/$REVIEWER_AGENT/.claude/settings.local.json"
    if [[ ! -f "$reviewer_settings" ]]; then
        reviewer_settings="$SNAPSHOT_DIR/reviewer-settings.local.json"
    fi
    if ! hook_command_has_fallback "$reviewer_settings"; then
        record_failure "reviewer hook commands do not use sh fallback form"
    fi

    if grep -R 'os error 2' "$REPO_DIR/.exo/logs" 2>/dev/null | grep -q .; then
        record_failure "hook logs contain os error 2"
    fi

    {
        printf 'Claude Teams inbox E2E validation completed at %s\n' "$(date -Iseconds)"
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

case "${1:-}" in
    --mock-has)
        MOCK_LOG="${2:?mock log required}"
        mock_log_has "${3:?method required}" "${4:?path regex required}"
        exit 0
        ;;
    --new-inbox-has-merge-ready)
        REPO_DIR="${2:?repo dir required}"
        TEAM_BASELINE="${3:?team baseline required}"
        new_team_inboxes_have_merge_ready
        exit 0
        ;;
esac

main "$@"
