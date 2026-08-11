# Live TL shadow trajectory

You are the root TL in a bounded E2E run. Complete the whole trajectory before
stopping.

1. Use exactly two `spawn_leaf` calls to spawn these two dev leaves:
   - `shadow-slice-a`: add `shadow-slice-a.txt`, commit, push, file a PR, and
     report the PR to the parent. The harness has already activated the
     scratch-local Chainlink issue before this wave starts.
   - `shadow-slice-b`: add `shadow-slice-b.txt`, commit, push, file a PR, and
     report the PR to the parent. The harness has already activated the
     scratch-local Chainlink issue before this wave starts.
2. Wait for both leaf results. Run the normal reviewer/review-loop protocol
   for each PR, repair only if requested, and merge both approved PRs with
   `merge_pr`.
3. Confirm both merges through the normal event/inbox path, then stop.

Do not edit files in the root worktree, call `fork_wave`, call `merge_pr`
before reviewer approval, skip the review loop, spawn a third child, or stop
after the first merge. The shadow companion is observing the immutable ledger
and must not be contacted or treated as a worker.
