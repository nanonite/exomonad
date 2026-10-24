#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "debug" && "${2:-}" == "models" ]]; then
    cat <<'JSON'
{"models":[{"slug":"gpt-5.6-luna","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]},{"slug":"gpt-6-luna","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]},{"slug":"gpt-6.1-sol","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]}]}
JSON
    exit 0
fi

agent="${EXOMONAD_AGENT_ID:-unknown}"
role="${EXOMONAD_ROLE:-unknown}"
model=""
effort=""
next_is_model=false
for argument in "$@"; do
    if [[ "$next_is_model" == true ]]; then
        model="$argument"
        next_is_model=false
    elif [[ "$argument" == "--model" ]]; then
        next_is_model=true
    elif [[ "$argument" == --model=* ]]; then
        model="${argument#--model=}"
    elif [[ "$argument" == model_reasoning_effort=* ]]; then
        effort="${argument#model_reasoning_effort=}"
    fi
done

record_event() {
    python3 - "$E2E_AGENT_LOG" "$1" "$agent" "$role" "$model" "$effort" "${2:-}" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, event, agent, role, model, effort, extra_json = sys.argv[1:]
extra = json.loads(extra_json) if extra_json else {}
record = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "event": event,
    "agent_id": agent,
    "role": role,
    "model": model,
    "effort": effort,
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

if [[ "$role" == "dev" && "$agent" == *issue-814* ]]; then
    if [[ "$(basename "$0")" == "opencode" ]]; then
        record_event historical_opencode_owner_failed '{"reason":"pre-publication OpenCode invocation exited before work began"}'
        # Let the host observe a live routed pane before recording the
        # historical pre-publication failure.
        sleep "${E2E_OPENCODE_FAIL_DELAY:-4}"
        exit 42
    fi
    [[ "$(basename "$0")" == "codex" && "${model%%#*}" == "gpt-6-luna" && "$effort" == "xhigh" ]] || {
        printf 'unexpected recovered #814 worker model: %s\n' "$model" >&2
        exit 1
    }
    source_branch="$(git -C "$PWD" branch --show-current)"
    source_head="$(git -C "$PWD" rev-parse HEAD)"
    source_status="$(git -C "$PWD" status --porcelain --untracked-files=all)"
    [[ -z "$source_status" ]] || {
        printf 'recovered #814 worker started from a dirty source worktree\n' >&2
        exit 1
    }
    record_event recovery_worker_started "$(python3 - "$PWD" "$source_branch" "$source_head" <<'PY'
import json
import sys

print(json.dumps({
    "working_directory": sys.argv[1],
    "source_branch": sys.argv[2],
    "source_head": sys.argv[3],
    "source_clean": True,
}))
PY
)"
    printf 'recovered Kani leaf from %s\n' "$agent" > kani-recovery.txt
    git add -- kani-recovery.txt
    git commit -m "test: complete recovered issue 814 spawn" -q
    response="$(mcp_call dev "$agent" file_pr '{"title":"test: recover Kani issue 814","body":"Disposable continuation fixture for a previously rejected, pre-execution spawn."}')"
    require_tool_success "$response"
    record_event recovery_worker_filed_pr
    pr_number="$(python3 - "$response" <<'PY'
import json
import sys


def find_pr_number(value):
    if isinstance(value, dict):
        candidate = value.get("pr_number")
        if isinstance(candidate, int) and candidate > 0:
            return candidate
        for nested in value.values():
            found = find_pr_number(nested)
            if found is not None:
                return found
        text = value.get("text")
        if isinstance(text, str):
            try:
                found = find_pr_number(json.loads(text))
            except json.JSONDecodeError:
                found = None
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_pr_number(nested)
            if found is not None:
                return found
    elif isinstance(value, str):
        try:
            return find_pr_number(json.loads(value))
        except json.JSONDecodeError:
            return None
    return None


number = find_pr_number(json.loads(sys.argv[1]))
if number is None:
    raise SystemExit("file_pr response did not include its authoritative PR number")
print(number)
PY
)"
    response="$(mcp_call dev "$agent" notify_parent '{"status":"success","message":"Recovered Kani change is published; the fixture keeps this invocation active until review and merge finish."}')"
    require_tool_success "$response"
    record_event recovery_worker_notified_parent "$(python3 - "$pr_number" "$E2E_AGENT_LOG" <<'PY'
import json
import sys
from pathlib import Path

print(json.dumps({
    "pr_number": int(sys.argv[1]),
    "merge_marker_path": str(Path(sys.argv[2]).parent / "merged-pr-markers" / f"pr-{sys.argv[1]}.json"),
}))
PY
)"
    : "${E2E_AGENT_LOG:?E2E_AGENT_LOG is required to locate the mock merge marker}"
    merge_marker="${E2E_AGENT_LOG%/*}/merged-pr-markers/pr-$pr_number.json"
    [[ -d "${merge_marker%/*}" ]] || {
        printf 'mock merge marker directory is missing: %s\n' "${merge_marker%/*}" >&2
        exit 1
    }
    for _ in $(seq 1 180); do
        if [[ -s "$merge_marker" ]]; then
            break
        fi
        sleep 1
    done
python3 - "$merge_marker" "$pr_number" <<'PY'
import json
import sys
from pathlib import Path

marker = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if marker.get("pr_number") != int(sys.argv[2]) or not marker.get("merge_commit_sha"):
    raise SystemExit("merged PR marker does not match the recovered leaf")
PY
    record_event recovery_worker_pr_merged "$(cat "$merge_marker")"
    exit 0
fi

if [[ "$role" == "tl" ]]; then
    output_path=""
    next_is_output=false
    for argument in "$@"; do
        if [[ "$next_is_output" == true ]]; then
            output_path="$argument"
            next_is_output=false
        elif [[ "$argument" == "--output-last-message" ]]; then
            next_is_output=true
        fi
    done
    [[ -n "$output_path" ]] || {
        printf 'TL fixture judgment did not receive --output-last-message\n' >&2
        exit 1
    }
    cat > "$output_path" <<'JSON'
{"action":"captured","reason":"The recovered fixture adds a test-only file and is not user-visible."}
JSON
    exit 0
fi

case "$role" in
    dev)
        [[ "$(basename "$0")" == "codex" && "$model" == "gpt-6-luna" && "$effort" == "xhigh" ]] || {
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
        [[ "$(basename "$0")" == "codex" && "$model" == "gpt-6.1-sol" && "$effort" == "medium" ]] || {
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
        record_event reviewer_approved "$(python3 - "$pr_number" "$head_sha" "$effort" <<'PY'
import json
import sys

print(json.dumps({
    "pr_number": int(sys.argv[1]),
    "head_sha": sys.argv[2],
    "effort": sys.argv[3],
}))
PY
)"
        ;;
    *)
        printf 'unexpected role for post-tidy fixture: %s\n' "$role" >&2
        exit 1
        ;;
esac
