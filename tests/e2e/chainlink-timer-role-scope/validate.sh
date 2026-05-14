#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$(pwd)}"
ROLES="$ROOT/.exo/roles/devswarm"
CHAINLINK_TOOL="$ROOT/haskell/wasm-guest/src/ExoMonad/Guest/Tools/Chainlink.hs"

failures=()

record_failure() {
    failures+=("$*")
    printf '[chainlink-timer-role-scope] FAIL: %s\n' "$*"
}

assert_contains() {
    local file="$1"
    local needle="$2"
    grep -Fq "$needle" "$file" || record_failure "$(basename "$file") missing $needle"
}

assert_not_contains() {
    local file="$1"
    local needle="$2"
    if grep -Fq "$needle" "$file"; then
        record_failure "$(basename "$file") unexpectedly contains $needle"
    fi
}

assert_contains "$ROLES/TLRole.hs" "ChainlinkTimerStart"
assert_contains "$ROLES/TLRole.hs" "ChainlinkTimerStop"
assert_contains "$ROLES/TLRole.hs" "ChainlinkTimerStatus"
assert_contains "$ROLES/TLRole.hs" "ChainlinkSessionStatus"
assert_contains "$ROLES/TLRole.hs" "ChainlinkIssueClose"

assert_not_contains "$ROLES/TLRole.hs" "ChainlinkAgentInit"
assert_not_contains "$ROLES/TLRole.hs" "ChainlinkSync"
assert_not_contains "$ROLES/TLRole.hs" "ChainlinkWorkerStatus"

assert_contains "$ROLES/DevRole.hs" "ChainlinkSessionStart"
assert_contains "$ROLES/DevRole.hs" "ChainlinkSessionWork"
assert_contains "$ROLES/DevRole.hs" "ChainlinkSessionEnd"
assert_contains "$ROLES/DevRole.hs" "ChainlinkSessionStatus"
assert_contains "$ROLES/DevRole.hs" "ChainlinkSubissueCreate"
assert_contains "$ROLES/DevRole.hs" "ChainlinkSubissueClose"

assert_not_contains "$ROLES/DevRole.hs" "ChainlinkIssueClose"
assert_not_contains "$ROLES/DevRole.hs" "ChainlinkTimer"
assert_not_contains "$ROLES/DevRole.hs" "ChainlinkAgentInit"
assert_not_contains "$ROLES/DevRole.hs" "ChainlinkSync"

assert_contains "$ROLES/WorkerRole.hs" "ChainlinkSessionStart"
assert_contains "$ROLES/WorkerRole.hs" "ChainlinkSessionWork"
assert_contains "$ROLES/WorkerRole.hs" "ChainlinkSessionEnd"
assert_contains "$ROLES/WorkerRole.hs" "ChainlinkIssueShow"
assert_contains "$ROLES/WorkerRole.hs" "ChainlinkIssueComment"

assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkIssueClose"
assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkSubissueClose"
assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkSubissueCreate"
assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkSessionStatus"
assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkTimer"
assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkAgentInit"
assert_not_contains "$ROLES/WorkerRole.hs" "ChainlinkSync"

assert_not_contains "$ROLES/ReviewerRole.hs" "Chainlink"

assert_not_contains "$CHAINLINK_TOOL" "buildLocksReleaseArgs"
assert_not_contains "$CHAINLINK_TOOL" "chainlink locks"
assert_not_contains "$CHAINLINK_TOOL" "locks release"
assert_not_contains "$CHAINLINK_TOOL" "chainlink_agent_init"
assert_not_contains "$CHAINLINK_TOOL" "chainlink_sync"
assert_not_contains "$CHAINLINK_TOOL" "chainlink_worker_status"

if (( ${#failures[@]} > 0 )); then
    printf '[chainlink-timer-role-scope] %s failure(s)\n' "${#failures[@]}"
    exit 1
fi

printf '[chainlink-timer-role-scope] PASS\n'
