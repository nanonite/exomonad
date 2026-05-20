#!/usr/bin/env bash
# Process companion for reviewer-ephemerality E2E.
set -u

REPO_DIR="${1:?REPO_DIR required}"
SESSION="${2:?SESSION required}"
RESULT_FILE="${3:?RESULT_FILE required}"
SERVER_LOG="${4:-}"
TIMEOUT_SECS="${E2E_REVIEWER_EPHEMERALITY_TIMEOUT:-1200}"
POLL_SECS=3
START="$(date +%s)"
FAILURES=()
EVIDENCE=()
PR_NUMBER=""
HEAD_BRANCH=""
FIRST_SHA=""
FIRST_AUTHOR=""
SECOND_SHA=""
SAW_SECOND_REVIEWER=0

log() { printf '[reviewer-ephemerality] %s\n' "$*"; }
record_failure() { FAILURES+=("$*"); log "FAIL: $*"; }
record_evidence() { EVIDENCE+=("$*"); log "OK: $*"; }
elapsed() { echo $(( $(date +%s) - START )); }
timed_out() { [[ "$(elapsed)" -ge "$TIMEOUT_SECS" ]]; }

write_result() {
    {
        printf 'Reviewer Ephemerality E2E Result\n'
        printf 'Failures: %s\n' "${#FAILURES[@]}"
        printf '\nEvidence:\n'
        for item in "${EVIDENCE[@]}"; do printf -- '- %s\n' "$item"; done
        if ((${#FAILURES[@]} > 0)); then
            printf '\nFailures:\n'
            for item in "${FAILURES[@]}"; do printf -- '- %s\n' "$item"; done
        fi
        if [[ -n "$SERVER_LOG" && -f "$SERVER_LOG" ]]; then
            printf '\nLast server log lines:\n'
            tail -n 40 "$SERVER_LOG" 2>/dev/null || true
        fi
    } >"$RESULT_FILE"
}

json_query() {
    python3 - "$@" <<'PY' 2>/dev/null
import json, pathlib, sys

mode = sys.argv[1]
repo = pathlib.Path(sys.argv[2])

def load_prs():
    path = repo / ".exo" / "prs.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        entries = data.get("prs") or data.get("pull_requests") or data.get("entries") or []
        if isinstance(entries, dict):
            return [entries[key] for key in sorted(entries, key=lambda value: int(value) if str(value).isdigit() else str(value))]
        return entries
    return data if isinstance(data, list) else []

def load_review(pr_number):
    path = repo / ".exo" / "reviews" / f"pr_{pr_number}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())

prs = load_prs()
pr = prs[0] if prs else {}

if mode == "pr-field":
    value = pr.get(sys.argv[3], "")
    print("" if value is None else value)
elif mode == "verdict-count-for-sha":
    pr_number, sha = sys.argv[3:5]
    verdicts = load_review(pr_number).get("verdicts", [])
    print(sum(1 for verdict in verdicts if verdict.get("head_sha") == sha))
elif mode == "first-verdict-field":
    pr_number, field = sys.argv[3:5]
    verdicts = load_review(pr_number).get("verdicts", [])
    print(verdicts[0].get(field, "") if verdicts else "")
elif mode == "unknown-authors":
    pr_number = sys.argv[3]
    verdicts = load_review(pr_number).get("verdicts", [])
    bad = [v.get("author_branch", "") for v in verdicts if not v.get("author_branch") or v.get("author_branch") == "unknown"]
    print("\n".join(bad))
elif mode == "sha-count":
    pr_number = sys.argv[3]
    verdicts = load_review(pr_number).get("verdicts", [])
    print(len({v.get("head_sha") for v in verdicts if v.get("head_sha")}))
PY
}

pr_field() { json_query pr-field "$REPO_DIR" "$1"; }
verdict_count_for_sha() { json_query verdict-count-for-sha "$REPO_DIR" "$1" "$2"; }
first_verdict_field() { json_query first-verdict-field "$REPO_DIR" "$1" "$2"; }
unknown_authors() { json_query unknown-authors "$REPO_DIR" "$1"; }
sha_count() { json_query sha-count "$REPO_DIR" "$1"; }

reviewer_slug_from_author() {
    printf '%s' "$1" | awk -F. '{print $NF}'
}

reviewer_resources_gone() {
    local slug="$1"
    [[ -n "$slug" ]] || return 1
    [[ ! -e "$REPO_DIR/.exo/worktrees/$slug" ]] || return 1
    [[ ! -e "$REPO_DIR/.exo/agents/$slug" ]] || return 1
    ! tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -Fxq "$slug"
}

wait_for_first_verdict() {
    while ! timed_out; do
        PR_NUMBER="$(pr_field number)"
        HEAD_BRANCH="$(pr_field head_branch)"
        FIRST_SHA="$(first_verdict_field "${PR_NUMBER:-1}" head_sha)"
        FIRST_AUTHOR="$(first_verdict_field "${PR_NUMBER:-1}" author_branch)"
        if [[ -n "$PR_NUMBER" && -n "$HEAD_BRANCH" && -n "$FIRST_SHA" && -n "$FIRST_AUTHOR" ]]; then
            record_evidence "first verdict pr=$PR_NUMBER sha=$FIRST_SHA author_branch=$FIRST_AUTHOR"
            return 0
        fi
        sleep "$POLL_SECS"
    done
    record_failure "timed out waiting for first reviewer verdict with head_sha and author_branch"
    return 1
}

wait_for_disposal() {
    local slug="$1"
    local deadline=$(( $(date +%s) + 45 ))
    while [[ "$(date +%s)" -lt "$deadline" ]]; do
        if reviewer_resources_gone "$slug"; then
            record_evidence "reviewer resources disposed for $slug"
            return 0
        fi
        sleep 2
    done
    record_failure "reviewer resources still present for $slug after verdict"
}

assert_single_verdict_for_sha_after_grace() {
    local pr="$1"
    local sha="$2"
    local count
    sleep "${E2E_REVIEWER_DUPLICATE_GRACE:-12}"
    count="$(verdict_count_for_sha "$pr" "$sha")"
    if [[ "$count" == "1" ]]; then
        record_evidence "exactly one verdict recorded for PR #$pr at $sha"
    else
        record_failure "expected exactly one verdict for PR #$pr at $sha, found $count"
    fi
}

push_second_round() {
    local branch="$1"
    git -C "$REPO_DIR" fetch origin "$branch" >/dev/null 2>&1 || true
    git -C "$REPO_DIR" checkout "$branch" >/dev/null 2>&1 \
        || git -C "$REPO_DIR" checkout -B "$branch" "origin/$branch" >/dev/null 2>&1
    git -C "$REPO_DIR" -c user.name='Exomonad E2E' -c user.email='e2e@example.com' \
        commit --allow-empty -m 'e2e trigger fresh reviewer round' >/dev/null 2>&1
    git -C "$REPO_DIR" push origin "$branch" >/dev/null 2>&1 || true
    record_evidence "pushed empty commit to $branch to force a fresh review round"
}

wait_for_second_round() {
    local old_sha="$1"
    while ! timed_out; do
        local current_sha
        local slug_count
        current_sha="$(pr_field last_head_sha)"
        if [[ -n "$current_sha" && "$current_sha" != "$old_sha" ]]; then
            SECOND_SHA="$current_sha"
        fi
        slug_count="$(find "$REPO_DIR/.exo/worktrees" -maxdepth 1 -type d -name "review-pr-${PR_NUMBER}-*" 2>/dev/null | wc -l | tr -d ' ')"
        if [[ -n "$SECOND_SHA" && "$slug_count" != "0" ]]; then
            SAW_SECOND_REVIEWER=1
        fi
        if [[ -n "$SECOND_SHA" && "$(sha_count "$PR_NUMBER")" -ge 2 ]]; then
            record_evidence "fresh reviewer round wrote verdict for new sha=$SECOND_SHA"
            return 0
        fi
        sleep "$POLL_SECS"
    done
    record_failure "timed out waiting for fresh reviewer verdict after SHA change"
}

assert_no_unknown_authors() {
    local bad
    bad="$(unknown_authors "$PR_NUMBER")"
    if [[ -z "$bad" ]]; then
        record_evidence "all verdict author_branch values are populated and not unknown"
    else
        record_failure "found missing or unknown author_branch values: $bad"
    fi
}

wait_for_first_verdict || { write_result; exit 0; }
first_slug="$(reviewer_slug_from_author "$FIRST_AUTHOR")"
wait_for_disposal "$first_slug"
assert_single_verdict_for_sha_after_grace "$PR_NUMBER" "$FIRST_SHA"
assert_no_unknown_authors
push_second_round "$HEAD_BRANCH"
wait_for_second_round "$FIRST_SHA"
if ((SAW_SECOND_REVIEWER == 1)); then
    record_evidence "observed a reviewer worktree for the new SHA round"
else
    record_failure "did not observe a reviewer worktree for the new SHA round"
fi
assert_single_verdict_for_sha_after_grace "$PR_NUMBER" "$SECOND_SHA"
assert_no_unknown_authors
write_result
