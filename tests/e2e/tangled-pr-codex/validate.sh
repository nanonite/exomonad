#!/usr/bin/env bash
set -euo pipefail

TIMEOUT_SECONDS="${TANGLED_PR_CODEX_E2E_TIMEOUT_SECONDS:-720}"
POLL_SECONDS=5
KNOT_HOSTNAME="localhost:5555"
OWNER_DID="did:plc:localdev"
SPINDLE_PID=""
completed=0
failures=()

log() {
    printf '[tangled-pr-codex-validator] %s\n' "$*"
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

pr_json_value() {
    local key="$1"
    python3 - "$REPO_DIR/.exo/prs.json" "$key" <<'PY'
import json
import sys

path, key = sys.argv[1:3]
with open(path) as f:
    registry = json.load(f)
prs = registry.get("prs", {})
if not prs:
    raise SystemExit(1)
pr = prs[sorted(prs.keys(), key=lambda v: int(v))[0]]
value = pr.get(key)
if value is None:
    raise SystemExit(1)
print(value)
PY
}

start_spindle() {
    local spindle="$PROJECT_ROOT/tangled-core/cmd/spindle/spindle"
    if [[ ! -x "$spindle" ]]; then
        record_failure "spindle binary missing at $spindle"
        return 1
    fi

    mkdir -p /tmp/spindle-logs
    SPINDLE_SERVER_HOSTNAME=localhost \
    SPINDLE_SERVER_LISTEN_ADDR=0.0.0.0:6555 \
    SPINDLE_SERVER_DB_PATH="$SPINDLE_DB" \
    SPINDLE_SERVER_OWNER="$OWNER_DID" \
    SPINDLE_SERVER_DEV=true \
    SPINDLE_SERVER_LOG_DIR=/tmp/spindle-logs \
    SPINDLE_SERVER_JETSTREAM_ENDPOINT="ws://localhost:5555/events" \
    SPINDLE_NIXERY_PIPELINES_NIXERY=nixery.tangled.sh \
    SPINDLE_NIXERY_PIPELINES_WORKFLOW_TIMEOUT=30m \
    SPINDLE_NIXERY_PIPELINES_MAX_JOB_MEMORY_MB=6144 \
        "$spindle" > "$SPINDLE_LOG" 2>&1 &
    SPINDLE_PID=$!
    log "started spindle pid=$SPINDLE_PID log=$SPINDLE_LOG"
}

inject_pipeline_event() {
    local branch="$1"
    local sha="$2"
    local workflow="$3"
    local rkey="$4"

    python3 - "$KNOT_DB" "$EVENT_FILE" "$workflow" "$KNOT_HOSTNAME" "$OWNER_DID" "$REPO_NAME" "$branch" "$sha" "$rkey" <<'PY'
import json
import pathlib
import sqlite3
import sys
import time

db_path, event_file, workflow_path, knot, owner, repo, branch, sha, rkey = sys.argv[1:]
raw = pathlib.Path(workflow_path).read_text()
now = int(time.time())
pipeline = {
    "$type": "sh.tangled.pipeline",
    "triggerMetadata": {
        "kind": "push",
        "push": {
            "ref": f"refs/heads/{branch}",
            "oldSha": "0000000000000000000000000000000000000000",
            "newSha": sha,
        },
        "repo": {"did": owner, "knot": knot, "repo": repo},
    },
    "workflows": [{
        "engine": "nixery",
        "name": "ci.yml",
        "raw": raw,
        "clone": {"depth": 1, "skip": False, "submodules": False},
    }],
}
stream_event = {
    "rkey": rkey,
    "nsid": "sh.tangled.pipeline",
    "event": pipeline,
    "created": now,
}
conn = sqlite3.connect(db_path)
conn.execute(
    "INSERT OR REPLACE INTO events (rkey, nsid, event, created) VALUES (?, ?, ?, ?)",
    (rkey, "sh.tangled.pipeline", json.dumps(pipeline), now),
)
conn.commit()
conn.close()
pathlib.Path(event_file).write_text(json.dumps(stream_event) + "\n")
print(rkey)
PY
}

finish() {
    [[ -n "$SPINDLE_PID" ]] && kill "$SPINDLE_PID" 2>/dev/null || true
    if (( completed == 0 )) && (( ${#failures[@]} == 0 )); then
        failures+=("validator exited before completing checks")
    fi
    {
        printf 'Tangled PR Codex E2E validation completed at %s\n' "$(date -Iseconds)"
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
trap finish EXIT

main() {
    wait_for "Codex TL worktree config exists" "[[ -n \"\$(find '$REPO_DIR/.exo/worktrees' -path '*/.codex/config.toml' -print 2>/dev/null | head -n 1)\" ]] && [[ -n \"\$(bash '$0' --find-tl '$REPO_DIR' 2>/dev/null || true)\" ]]"
    wait_for "Codex worker agent config exists" "[[ -n \"\$(find '$REPO_DIR/.exo/agents' -path '*/.codex/config.toml' -print 2>/dev/null | head -n 1)\" ]]"
    wait_for "Codex dev worktree config exists" "[[ -n \"\$(bash '$0' --find-dev '$REPO_DIR' 2>/dev/null || true)\" ]]"
    wait_for "worker notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'tangled-pr-codex-worker-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    wait_for "dev output file exists" "find '$REPO_DIR/.exo/worktrees' -name tangled-pr-codex-dev-output.txt -print 2>/dev/null | grep -q ."
    wait_for "local PR registry created" "[[ -f '$REPO_DIR/.exo/prs.json' ]] && grep -q 'tangled-pr-codex' '$REPO_DIR/.exo/prs.json'"

    local branch
    local author_agent
    local dev_slug
    branch="$(pr_json_value head_branch)"
    author_agent="$(pr_json_value author_agent)"
    dev_slug="${branch##*.}"
    log "observed PR branch=$branch author_agent=$author_agent"

    wait_for "dev branch pushed to local Tangled remote" "cd '$REPO_DIR' && GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git ls-remote tangled 'refs/heads/$branch' | grep -q ."
    local sha
    sha="$(cd "$REPO_DIR" && GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git ls-remote tangled "refs/heads/$branch" | awk '{print $1}')"
    if [[ -z "$sha" ]]; then
        record_failure "could not resolve pushed Tangled branch sha"
        return
    fi

    local dev_config
    local dev_worktree
    local workflow
    dev_config="$(find_worktree_config_by_role dev || true)"
    dev_worktree="$(dirname "$(dirname "$dev_config")")"
    workflow="$dev_worktree/.tangled/workflows/ci.yml"
    if [[ ! -f "$workflow" ]]; then
        record_failure "workflow file missing in dev worktree"
        return
    fi

    local safe_branch
    local rkey
    safe_branch="$(printf '%s' "$branch" | tr '/.' '--')"
    rkey="tangled-pr-codex-${safe_branch}-$(date +%s)"
    inject_pipeline_event "$branch" "$sha" "$workflow" "$rkey" >/dev/null
    log "injected Tangled pipeline rkey=$rkey branch=$branch sha=${sha:0:12}"

    start_spindle
    wait_for "spindle HTTP endpoint ready" "curl -sf http://localhost:6555/ >/dev/null 2>&1"
    wait_for "spindle enqueued pipeline" "grep -q 'pipeline enqueued successfully.*$rkey' '$SPINDLE_LOG' 2>/dev/null"
    wait_for "spindle workflow log exists" "ls /tmp/spindle-logs/localhost-5555-$rkey-ci.yml.log >/dev/null 2>&1"
    wait_for "spindle workflow succeeded" "grep -q '\"step_status\":\"end\"' '/tmp/spindle-logs/localhost-5555-$rkey-ci.yml.log' 2>/dev/null && ! grep -q '\"step_status\":\"failed\"\\|\"exit_code\":[^0]' '/tmp/spindle-logs/localhost-5555-$rkey-ci.yml.log' 2>/dev/null"

    wait_for "ExoMonad mapped Tangled pipeline to PR branch" "grep -R 'Spindle: CI initiated for worktree' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '$branch' | grep -q ."
    wait_for "ExoMonad ingested spindle success status" "grep -R 'Spindle: CI status updated' '$REPO_DIR/.exo/logs' 2>/dev/null | grep '$branch' | grep 'success' | grep -q ."
    wait_for "Codex reviewer approval recorded" "[[ -f '$REPO_DIR/.exo/reviews/pr_1.json' ]] && grep -q 'approved' '$REPO_DIR/.exo/reviews/pr_1.json'"
    wait_for "Codex reviewer notify_parent tmux delivery succeeded" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'review-pr-1-codex' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    wait_for "merge-ready notification recorded" "grep -R '\\[MERGE READY\\]' '$REPO_DIR/.exo/logs' 2>/dev/null | grep 'PR #1' | grep -q ."
    wait_for "merge-ready release delivered to live dev leaf" "grep -R 'message.delivery' '$REPO_DIR/.exo/logs' 2>/dev/null | grep -E 'agent_id=\"?event-handler\"?' | grep -E 'recipient=\"?$branch\"?' | grep 'tmux_routing' | grep 'outcome=\"success\"' | grep -q ."
    if grep -R "No plugin found for agent '$dev_slug'" "$REPO_DIR/.exo/logs" >/dev/null 2>&1; then
        record_failure "watcher lost dev plugin before merge-ready release for $dev_slug"
        return
    fi
    completed=1
}

if [[ "${1:-}" == "--find-tl" ]]; then
    REPO_DIR="${2:?repo dir required}"
    find_worktree_config_by_role tl
    exit 0
fi

if [[ "${1:-}" == "--find-dev" ]]; then
    REPO_DIR="${2:?repo dir required}"
    find_worktree_config_by_role dev
    exit 0
fi

REPO_DIR="${1:?repo dir required}"
SESSION="${2:?tmux session required}"
RESULT_FILE="${3:?result file required}"
KNOT_DB="${4:?knot db required}"
SPINDLE_DB="${5:?spindle db required}"
EVENT_FILE="${6:?relay event file required}"
SPINDLE_LOG="${7:?spindle log required}"
REPO_NAME="${8:?repo name required}"
PROJECT_ROOT="${9:?project root required}"

main "$@"
