# Reviewer Convergence Loop — Testrunner Plan

You are the testrunner companion for an E2E test of chainlink #247 / #249 / #250. You observe via read-only Bash (no edits to source code; only fixture mutations under `.exo/`). You report the verdict via `notify_parent`.

## Goal

Verify that when a dev-leaf pushes a fix after a reviewer's `ChangesRequested`, the worktree event watcher fans the resulting `fixes_pushed` event out to BOTH the leaf's plugin manager AND the reviewer's. The canonical signal is the server-log line:

```
Fanning out pr_review event to reviewer agent
```

…with `kind=fixes_pushed` in the structured fields.

## Phases

### Phase 1 — Wait for PR

Poll `.exo/prs.json` (path resolved from the project root passed to your task) at 5s intervals until at least one PR entry exists. Timeout 5 minutes. Capture:
- The PR `number`
- The leaf `author_agent` (used to identify the leaf's tmux window)
- The leaf's `head_branch`

If timeout: `notify_parent status=failure message="No PR appeared within 5 minutes — leaf did not open a PR"` and stop.

### Phase 2 — Wait for reviewer assignment

Poll the same JSON until the PR entry has BOTH `reviewer_agent` AND `reviewer_birth_branch` populated. Timeout 3 minutes.

This signals that `spawn_reviewer_for_pr` ran and the PR registry was updated (subissue #248's work). If timeout: `notify_parent status=failure message="Reviewer never registered against PR #N — #248 wiring is broken"` and stop.

### Phase 3 — Inject ChangesRequested

The watcher polls every `poll_interval` seconds (5s in this fixture). To deterministically trigger the FixesPushed path, the watcher needs to see `review_state=ChangesRequested` BEFORE the head SHA changes.

**Race note:** in a real run the auto-spawned reviewer may complete its review (approve or request-changes) before this phase runs. If the reviewer already approved (read `.exo/reviews/pr_N.json` first to check), the convergence test is moot — the reviewer made a different decision than the test is set up to exercise. Report this as a skipped result rather than a failure:
```
notify_parent status=failure message="convergence-loop SKIPPED: reviewer already approved before ChangesRequested could be injected; race window too tight to exercise the fan-out path"
```
…and write the failure marker accordingly.

Otherwise, mutate the local PR registry:
1. Read `.exo/prs.json` via `jq`.
2. For the target PR, set `review_state` to `"ChangesRequested"`. Do NOT touch `last_head_sha` — that field is dead in the local-PR path (see chainlink #254).
3. Atomic write: write to a temp file, then rename to `.exo/prs.json`.

Wait 8 seconds (≥ one poll cycle) so the watcher latches the ChangesRequested state.

### Phase 4 — Tell the leaf to push a fix

Use `send_message` to the leaf (using the `author_agent` captured in Phase 1):

```
send_message recipient=<author_agent> summary="push a trivial fix" content="Append a second paragraph to CONTRIBUTING.md, commit, and push. The PR head SHA must change. Do not file a new PR. Do not call notify_parent — the testrunner will detect the SHA change."
```

### Phase 5 — Wait for SHA change

The watcher tracks SHA changes in its in-memory `WatchState`, but per chainlink #254 the `last_head_sha` field on the persisted PrEntry is dead (never written). Do NOT poll it.

Instead, poll the **leaf's worktree git log** directly:

1. Resolve the leaf's worktree path: `${REPO_DIR}/.exo/worktrees/${AUTHOR_AGENT}` (the leaf's `author_agent` from Phase 1).
2. Record the current head SHA via `git -C <worktree> rev-parse HEAD` — that's the baseline.
3. Poll the same command every 5s. When the SHA differs from the baseline, the leaf pushed a fix. Capture the new SHA.

Timeout 5 minutes.

If the leaf never pushes within 5 minutes: report failure and stop. Likely root cause is that the codex leaf doesn't loop after stop-hook block and never processed your `send_message` — see chainlink #247 discussion thread.

### Phase 6 — Assert the fan-out fired

After the SHA change is observed, give the watcher one more poll cycle to dispatch (sleep 8s). Then grep the server log file (path resolved from the project root, default `.exo/server.log` or the env var `EXOMONAD_SERVER_LOG_FILE`) for:

1. The literal phrase `Fanning out pr_review event to reviewer agent` — must appear at least once with `kind=fixes_pushed` in the structured fields AND the PR number matching.
2. (Negative check) The literal phrase `pr_review event fired but no reviewer is registered` — must NOT appear for this PR number.

Both conditions must hold. Report the verdict to BOTH the parent (so the root TL stops idling) AND the validate.sh process companion (which polls `.exo/e2e-reviewer-convergence/` for marker files):

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

Marker files MUST be written before the notify_parent call so validate.sh observes them.

## Hard rules

- Read-only on source code. Only fixture mutations are allowed, and only under `.exo/`.
- Use only `notify_parent`, `send_message`, and read-only `bash` (jq, grep, sed for atomic writes, sleep).
- Do not merge the PR. Do not close any chainlink issues.
- If anything times out, report failure and stop — do not retry, do not recover.
- The test is one-shot: do not loop the scenario.
