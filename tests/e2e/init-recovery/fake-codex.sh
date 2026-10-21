#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "debug" && "${2:-}" == "models" ]]; then
    cat <<'JSON'
{"models":[{"slug":"gpt-5.6-luna","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]}]}
JSON
    exit 0
fi

agent="${EXOMONAD_AGENT_ID:-unknown}"
role="${EXOMONAD_ROLE:-unknown}"
model=""
next_is_model=false
for argument in "$@"; do
    if [[ "$next_is_model" == true ]]; then
        model="$argument"
        next_is_model=false
    elif [[ "$argument" == "--model" ]]; then
        next_is_model=true
    elif [[ "$argument" == --model=* ]]; then
        model="${argument#--model=}"
    fi
done

record_event() {
    python3 - "$E2E_AGENT_LOG" "$1" "$agent" "$role" "$model" "${2:-}" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, event, agent, role, model, extra_json = sys.argv[1:]
extra = json.loads(extra_json) if extra_json else {}
record = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "event": event,
    "agent_id": agent,
    "role": role,
    "model": model,
    **extra,
}
with open(path, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\n")
PY
}

mcp_call() {
    local mcp_role="$1"
    local mcp_agent="$2"
    local tool="$3"
    local raw_args="$4"
    local request
    request="$(python3 - "$tool" "$raw_args" <<'PY'
import json
import sys

tool, raw_args = sys.argv[1:]
print(json.dumps({"name": tool, "arguments": json.loads(raw_args)}))
PY
)"
    curl -fsS --unix-socket "$E2E_MCP_SOCKET" \
        -H 'Content-Type: application/json' \
        -d "$request" \
        "http://localhost/agents/$mcp_role/$mcp_agent/tools/call"
}

require_tool_success() {
    python3 - "$1" <<'PY'
import json
import sys

response = json.loads(sys.argv[1])
if response.get("success") is not True:
    raise SystemExit("MCP tool returned a non-success result")
PY
}

case "$role" in
    dev)
        [[ "$model" == "gpt-5.6-luna" ]] || {
            printf 'unexpected worker model: %s\n' "$model" >&2
            exit 1
        }
        source_branch="$(git -C "$E2E_SOURCE_WORKTREE" branch --show-current)"
        source_head="$(git -C "$E2E_SOURCE_WORKTREE" rev-parse HEAD)"
        source_status="$(git -C "$E2E_SOURCE_WORKTREE" status --porcelain --untracked-files=all)"
        [[ -z "$source_status" ]] || {
            printf 'handoff source worktree is not clean at leaf spawn\n' >&2
            exit 1
        }
        [[ -f "$E2E_ROOT_ONLY_DIRTY_FILE" ]] || {
            printf 'outer root dirty probe was not present at leaf spawn\n' >&2
            exit 1
        }
        expected_head="$(cat "$E2E_HANDOFF_HEAD_FILE")"
        [[ "$source_head" == "$expected_head" ]] || {
            printf 'child source head differs from committed handoff head\n' >&2
            exit 1
        }
        record_event worker_started "$(python3 - "$PWD" "$E2E_SOURCE_WORKTREE" "$source_branch" "$source_head" <<'PY'
import json
import sys

print(json.dumps({
    "working_directory": sys.argv[1],
    "source_worktree": sys.argv[2],
    "source_branch": sys.argv[3],
    "source_head": sys.argv[4],
    "source_clean": True,
    "outer_root_dirty": True,
}))
PY
)"
        printf 'post-tidy leaf change from %s\n' "$agent" > post-tidy-leaf.txt
        git add -- post-tidy-leaf.txt
        git commit -m "feat: add post-tidy leaf fixture" -q
        response="$(mcp_call dev "$agent" file_pr '{"title":"feat: verify post-tidy leaf handoff","body":"## Summary\nExercise a real leaf after the commit-all handoff.\n\n## Acceptance Criteria\n- The worker starts from the committed parent revision.\n- The worker uses the selected codex route."}')"
        require_tool_success "$response"
        record_event worker_filed_pr
        # Keep the producer invocation active while the disposable init test
        # exercises replay. Exiting without a model handoff would correctly
        # generate a worker-lifecycle blocker unrelated to spawn readiness.
        trap 'exit 0' HUP INT TERM
        while true; do
            sleep 1
        done
        ;;
    reviewer)
        [[ "$model" == "gpt-5.6-luna" ]] || {
            printf 'unexpected reviewer model: %s\n' "$model" >&2
            exit 1
        }
        pr_suffix="${agent#review-pr-}"
        pr_number="${pr_suffix%%-*}"
        [[ "$pr_number" =~ ^[0-9]+$ ]] || {
            printf 'reviewer identity does not contain a PR number\n' >&2
            exit 1
        }
        pr="$(curl -fsS "$E2E_MOCK_URL/api/v1/repos/owner/repo/pulls/$pr_number")"
        head_sha="$(python3 - "$pr" <<'PY'
import json
import sys

record = json.loads(sys.argv[1])
print(record["head"]["sha"])
PY
)"
        [[ -n "$head_sha" ]] || {
            printf 'mock PR had no current head SHA\n' >&2
            exit 1
        }
        arguments="$(python3 - "$pr_number" "$head_sha" <<'PY'
import json
import sys

print(json.dumps({
    "pr_number": int(sys.argv[1]),
    "head_sha": sys.argv[2],
    "body": "Disposable post-tidy worker/reviewer route acceptance.",
    "findings": [],
}))
PY
)"
        response="$(mcp_call reviewer "$agent" approve_pr "$arguments")"
        require_tool_success "$response"
        record_event reviewer_approved "$(python3 - "$pr_number" "$head_sha" <<'PY'
import json
import sys

print(json.dumps({"pr_number": int(sys.argv[1]), "head_sha": sys.argv[2]}))
PY
)"
        ;;
    *)
        printf 'unexpected role for post-tidy fixture: %s\n' "$role" >&2
        exit 1
        ;;
esac
