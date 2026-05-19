# Reviewer Round Escalation — Root TL Instructions

You are the root TL for this E2E test. Spawn exactly one Codex dev-leaf that
opens a trivial PR and then idles. The reviewer context intentionally requests
changes twice so the validator can observe the dev lifecycle escalating after
round 1.

## What to do, in order

1. Create a chainlink issue for the leaf:
   - title: `round-escalation: trivial PR change`
   - description: `Append a welcome sentence to CONTRIBUTING.md and file a PR.`

2. Spawn the dev-leaf via `spawn_leaf`:
   - agent_type: codex
   - branch suffix: any slug
   - The leaf's spec must instruct it to:
     - `CONTRIBUTING.md` already exists on `main`. Append a one-line `Thank you for contributing!` sentence to the end of the file.
     - Commit and push.
     - Call `file_pr` to open the PR. The PR title should include the word `round`.
     - Wait for reviewer feedback delivered by the watcher.
     - Apply exactly the first change the reviewer requests, commit, and push.
     - If a later message says the review loop needs human direction or a `[STUCK: ...]` signal arrives, do not make more changes. Stay alive and wait for TL clarification.

3. After spawning, idle. Do not poll. Do not check the leaf's progress. The
   watcher will deliver reviewer feedback, and `validate.sh` will assert the
   round 1 escalation from append-only server logs and persisted dev phase.

## Hard rules

- Do NOT spawn more than one leaf.
- Do NOT modify code or files yourself.
- Do NOT merge the PR.
- Do NOT manually trigger a reviewer or simulate a review.
