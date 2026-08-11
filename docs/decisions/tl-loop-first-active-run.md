# Decision: first active TL loop run

Date: 2026-08-11
Status: PASS
Issue: #694

## Outcome

The first bounded active TL-loop wave completed successfully against a fresh
scratch repository with a bare Git remote. The controller ran entirely in the
test process: it did not call exomonad init, start an ExoMonad server, create
an interactive session, or start tmux.

The wave used two disjoint slices, active-slice-a and active-slice-b.
Each slice created a real branch and worktree, wrote a source fixture and
unit test, ran python3 -m unittest discover -s tests, committed, and pushed.
Deterministic watcher adjudications approved PRs #1001 and #1002; both were
merged into main. The controller then filed upward PR #2001 from the merged
result.

## Assertions

- Final TL state was TLDone.
- The ledger contained seven contiguous events with a complete reader status.
- The consumed ledger offset was 7 and there were no reader findings.
- Both child PRs were reviewed once and merged.
- Budget reconciliation recorded 600 tokens per slice, 1,200 worker tokens
  total, no reservations, and no unreconciled charges.
- No MutationBlocked result occurred.
- No manual intervention was required.
- No named tmux session was started.
- Child worktrees were removed, leaving only the scratch repository's main
  worktree.
- The upward PR was filed against main.

## Verification

    just e2e-tl-loop-active
    just check-e2e-tl-loop-active

The harness removes its exact temporary scratch directory on both success and
failure paths.
