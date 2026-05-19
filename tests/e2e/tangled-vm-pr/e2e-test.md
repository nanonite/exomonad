# Tangled VM PR E2E — Root TL Instructions

You are the root TL for the Tangled VM PR E2E test. Execute these steps immediately.

1. Spawn exactly one Codex dev leaf with `spawn_leaf`.
2. The leaf name/slug must start with `tangled-vm-pr-dev`.
3. Give the leaf this exact task:

   You are a Codex dev leaf in the Tangled VM PR E2E test. Do exactly these steps:
   1. Create `tangled-vm-pr-dev-output.txt` containing `Tangled VM PR dev output`.
   2. Commit it with message `e2e: add tangled vm pr dev output`.
   3. Push your branch.
   4. Use the ExoMonad `file_pr` MCP tool to file a local PR for your branch. Do not use `gh`.
   5. Stay alive until watcher-delivered review, spindle CI, and merge-ready messages are observed. If reviewer changes are requested, address them in the same worktree and push again. Do not stop immediately after filing the PR.

4. After spawning the dev leaf, idle. Do not merge the PR and do not simulate review or CI.

Hard rules:
- Do not run `gh` commands.
- Do not create commits, branches, PRs, reviews, CI records, or validator artifacts yourself.
- Do not spawn more than one dev leaf.
- Do not treat reviewer approval alone as completion; the validator waits for Tangled spindle CI correlation and merge-ready delivery.
