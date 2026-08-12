#!/usr/bin/env bash
set -euo pipefail

log_file="${E2E_FAKE_CODEX_LOG:?E2E_FAKE_CODEX_LOG is required}"
agent="${EXOMONAD_AGENT_ID:-unknown}"
role="${EXOMONAD_ROLE:-unknown}"
printf 'start role=%s agent=%s args=%q\n' "$role" "$agent" "$*" >>"$log_file"

mcp_call() {
    local mcp_role="$1"
    local mcp_agent="$2"
    local tool="$3"
    local raw_args="$4"
    local request
    request="$(python3 - "$tool" "$raw_args" <<'PY'
import json
import sys

tool, raw_args = sys.argv[1:3]
print(json.dumps({"name": tool, "arguments": json.loads(raw_args)}))
PY
)"
    curl -fsS --unix-socket "${E2E_MCP_SOCKET:?E2E_MCP_SOCKET is required}" \
        -H 'Content-Type: application/json' \
        -d "$request" \
        "http://localhost/agents/$mcp_role/$mcp_agent/tools/call"
}

commit_fixture() {
    local file="$1"
    local contents="$2"
    printf '%s\n' "$contents" >"$file"
    git config user.name "Exomonad fake Codex"
    git config user.email "fake-codex@example.invalid"
    git add "$file"
    git commit -m "Record one-shot E2E fixture" -q
}

case "$agent" in
    one-shot-codex)
        if [[ ! -f one-shot-output.txt ]]; then
            commit_fixture one-shot-output.txt "clean exit with handoff"
            response="$(mcp_call dev "$agent" file_pr '{"title":"One-shot lifecycle fixture","body":"Published by the fake Codex dev."}')"
            printf 'file_pr=%s\n' "$response" >>"$log_file"
            sleep 4
        else
            response="$(mcp_call dev "$agent" check_inbox '{}')"
            printf '[RESUME-INBOX] %s\n' "$response" >>"$log_file"
            sleep 4
        fi
        ;;
    no-handoff-codex)
        commit_fixture no-handoff-output.txt "clean exit without handoff"
        sleep 4
        ;;
    live-guidance-codex)
        while IFS= read -r line; do
            printf '[LIVE-STDIN] %s\n' "$line" >>"$log_file"
        done
        ;;
    review-pr-1-codex)
        printf 'reviewer_spawned=true\n' >>"$log_file"
        sleep 4
        ;;
    *)
        printf 'unexpected fake Codex identity: %s\n' "$agent" >>"$log_file"
        exit 1
        ;;
esac

printf 'finish role=%s agent=%s\n' "$role" "$agent" >>"$log_file"
