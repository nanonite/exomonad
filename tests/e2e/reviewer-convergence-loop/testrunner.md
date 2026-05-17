# Reviewer Convergence Loop - Testrunner Plan

You are the testrunner companion for an E2E test of the reviewer convergence
loop. You observe via read-only Bash and report the verdict via `notify_parent`.

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

If timeout: write the failure marker and `notify_parent status=failure message="No PR appeared within 5 minutes - leaf did not open a PR"`.

### Phase 2 - Wait for reviewer assignment

Poll the same JSON until the PR entry has both `reviewer_agent` and `reviewer_birth_branch` populated. Timeout 3 minutes.

If timeout: write the failure marker and `notify_parent status=failure message="Reviewer never registered against PR #N - reviewer wiring is broken"`.

### Phase 3 - Wait for natural ChangesRequested

Do not mutate `.exo/prs.json`. The source of truth is `.exo/reviews/pr_N.json`, written by the reviewer.

Poll `.exo/reviews/pr_N.json` until it has `state = "changes_requested"`. Timeout 5 minutes. If it has `state = "approved"` before a changes-requested verdict, write this failure marker and notify parent with failure:

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

If the leaf never pushes within 5 minutes, write the failure marker and notify parent with:

```text
convergence-loop FAILED: HEAD SHA did not change after reviewer requested changes
```

### Phase 5 - Assert watcher fan-out and reviewer dispatch

After the SHA change is observed, sleep 8s so the watcher has at least one poll cycle to dispatch. Then inspect the server log path from `EXOMONAD_SERVER_LOG_FILE` or the fixture path passed in the task.

Required:

- At least one `Fanning out pr_review event to reviewer agent` log for this PR with `kind=fixes_pushed`.
- A reviewer `handle_event` invocation for `review-pr-N` after the fan-out.
- A `handle_event returned` line after the reviewer invocation.

Forbidden:

- `No plugin found for event target` for `review-pr-N`
- `pr_review event fired but no reviewer is registered` for this PR

## Verdict

Success path:
```bash
mkdir -p .exo/e2e-reviewer-convergence
printf 'convergence-loop verified: fixes_pushed fanned out to reviewer for PR #%s\n' "$PR_NUMBER" > .exo/e2e-reviewer-convergence/success
```
Then:
```
notify_parent status=success message="convergence-loop verified: fixes_pushed fanned out to reviewer for PR #<N>"
```

Failure path:
```bash
mkdir -p .exo/e2e-reviewer-convergence
printf 'convergence-loop FAILED: %s\n' "$REASON" > .exo/e2e-reviewer-convergence/failure
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
- Do not send messages to the leaf. The watcher must deliver reviewer feedback.
- Do not merge the PR. Do not close Chainlink issues.
- If anything times out, report failure and stop.
