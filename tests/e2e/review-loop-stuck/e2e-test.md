# Review Loop Stuck E2E

This harness drives the real Forgejo worktree watcher against the mock Forgejo API. It creates one PR with two distinct `CHANGES_REQUESTED` reviews on different head SHAs, then asserts that the watcher marks the PR stuck and files a Chainlink `review-stuck` issue for human triage without closing or merging the PR.
