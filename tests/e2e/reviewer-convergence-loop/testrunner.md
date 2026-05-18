# Reviewer Convergence Loop - Testrunner Plan

You are the testrunner companion for an E2E test of the reviewer convergence
loop. You observe via read-only Bash and report progress via `notify_parent`.
The process companion `validate.sh` writes the final objective verdict after it
checks the marker file, server logs, MCP stdio evidence, and side-channel logs.

## Goal

Verify that the real auto-spawned reviewer naturally requests changes from its
fixture-local context, the leaf pushes one fix, and the watcher fans the
resulting `fixes_pushed` event out to both the leaf plugin manager and the
reviewer plugin manager.

Required server-log signals:

```
Fanning out pr_review event to reviewer agent
[EventDispatch] Calling handle_event for agent 'review-pr-
[EventDispatch] handle_event returned
```

The fan-out line must correspond to `kind=fixes_pushed`.

## Phases

### Phase 1 - Wait for PR

Poll `.exo/prs.json` at 5s intervals until at least one PR entry exists. Timeout 5 minutes. Capture:
- The PR `number`
- The leaf `author_agent`
- The leaf's `head_branch`

If timeout: write the failure marker with the exact command and empty output
evidence, then `notify_parent status=failure message="No PR appeared within 5 minutes - evidence: .exo/prs.json absent or empty"`.

### Phase 2 - Wait for reviewer assignment

Poll the same JSON until the PR entry has both `reviewer_agent` and `reviewer_birth_branch` populated. Timeout 3 minutes.

If timeout: write the failure marker with quoted `.exo/prs.json` evidence, then
`notify_parent status=failure message="Reviewer never registered against PR #N - evidence: <quoted jq output/path>"`.

### Phase 3 - Wait for natural ChangesRequested

Do not mutate `.exo/prs.json`. The source of truth is `.exo/reviews/pr_N.json`, written by the reviewer.

Poll `.exo/reviews/pr_N.json` until it has `state = "changes_requested"`. Timeout 5 minutes. If it has `state = "approved"` before a changes-requested verdict, write this failure marker with the review file path and quoted state evidence, then notify parent with failure:

```text
convergence-loop FAILED: reviewer approved before requesting the fixture-required header change
```

### Phase 4 - Wait for leaf fix commit

Resolve the leaf worktree path:

```bash
${REPO_DIR}/.exo/worktrees/${AUTHOR_AGENT}
```

Record its current `git rev-parse HEAD` as the baseline after the changes-requested verdict appears. Poll the same command every 5s until the SHA differs. Timeout 5 minutes.

Do not use `send_message`. The watcher should inject the review comments into the leaf pane naturally.

If the leaf never pushes within 5 minutes, write the failure marker with the
leaf worktree path, baseline SHA, final SHA, and the command used. Then notify
parent with:

```text
convergence-loop FAILED: HEAD SHA did not change after reviewer requested changes
```

### Phase 5 - Wait for watcher fan-out and reviewer dispatch evidence

After the SHA change is observed, sleep 8s so the watcher has at least one poll cycle to dispatch. Then inspect the server log path from `EXOMONAD_SERVER_LOG_FILE` or the fixture path passed in the task.

Wait up to 2 minutes for all required lines to appear. You are only collecting
evidence for `validate.sh`; do not claim transport correctness beyond the exact
lines you can quote.

Required marker evidence:

- At least one `Fanning out pr_review event to reviewer agent` log for this PR with `kind=fixes_pushed`.
- A reviewer `handle_event` invocation for `review-pr-N` after the fan-out.
- A `handle_event returned` line after the reviewer invocation.

Forbidden:

- `No plugin found for event target` for `review-pr-N`
- `pr_review event fired but no reviewer is registered` for this PR

If any required log is missing or forbidden log is present, write a failure marker
that quotes:
- the server log path,
- the grep command used,
- the matching forbidden line or the missing required pattern,
- the last 20 relevant server log lines from `grep -E 'Fanning out pr_review|EventDispatch|No plugin found|no reviewer' "$SERVER_LOG"`.

## Verdict

Success path:
```bash
mkdir -p .exo/e2e-reviewer-convergence
{
  printf 'convergence-loop candidate success for PR #%s\n' "$PR_NUMBER"
  printf 'evidence: server log path: %s\n' "$SERVER_LOG"
  printf 'evidence: fan-out line: %s\n' "$FANOUT_LINE"
  printf 'evidence: reviewer dispatch line: %s\n' "$DISPATCH_LINE"
  printf 'evidence: handle_event returned line: %s\n' "$RETURN_LINE"
  printf 'evidence: leaf worktree: %s\n' "$LEAF_WORKTREE"
  printf 'evidence: baseline sha: %s\n' "$BASELINE_SHA"
  printf 'evidence: fixed sha: %s\n' "$FIXED_SHA"
} > .exo/e2e-reviewer-convergence/success
```
Then:
```
notify_parent status=success message="convergence-loop candidate success for PR #<N>; validate.sh will verify MCP stdio and side-channel evidence"
```

Failure path:
```bash
mkdir -p .exo/e2e-reviewer-convergence
{
  printf 'convergence-loop FAILED: %s\n' "$REASON"
  printf 'evidence path: %s\n' "$EVIDENCE_PATH"
  printf 'evidence command: %s\n' "$EVIDENCE_COMMAND"
  printf 'evidence output:\n%s\n' "$EVIDENCE_OUTPUT"
} > .exo/e2e-reviewer-convergence/failure
```
Then:
```
notify_parent status=failure message="convergence-loop FAILED: <which assertion failed, with grep evidence>"
```

Marker files must be written before `notify_parent` so `validate.sh` observes them.

## Hard Rules

- Read-only on source code.
- Do not mutate `.exo/prs.json` or `.exo/reviews/pr_N.json`.
- Use only `notify_parent` and read-only Bash.
- Never call server endpoints directly. No `curl --unix-socket`, no direct HTTP
  requests to `.exo/server.sock`. Transport correctness belongs to MCP stdio and
  `validate.sh` will fail the test if UDS curl side-channel evidence appears.
- Do not invent causes. A failure claim must name the exact missing or forbidden
  pattern and quote command output or a path that proves it.
- Do not write a success marker unless all required Phase 5 variables are filled
  from real grep output: `FANOUT_LINE`, `DISPATCH_LINE`, and `RETURN_LINE`.
- Do not write a failure marker unless `REASON`, `EVIDENCE_PATH`,
  `EVIDENCE_COMMAND`, and `EVIDENCE_OUTPUT` are concrete and non-empty. If a file
  is missing, the evidence output should be the exact `test -f <path>` result or
  `ls -l`/`stat` error output for that path.
- Do not send messages to the leaf. The watcher must deliver reviewer feedback.
- Do not merge the PR. Do not close Chainlink issues.
- If anything times out, report failure and stop.
