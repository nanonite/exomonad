# Reviewer Ephemerality E2E

You are the root TL for this temporary ExoMonad test repository.

Create exactly one Codex dev leaf with a small PR-able task:

1. Ask the leaf to create `REVIEWER_EPHEMERALITY.md` with one paragraph that says this fixture exists for reviewer lifecycle testing.
2. Ask the leaf to commit the change and call `file_pr` for the branch.
3. After the leaf reports the PR is filed, idle. Do not merge. The process companion validates reviewer disposal, duplicate verdict prevention, and fresh reviewer creation after it pushes a synthetic new SHA to the PR branch.

Do not run `gh`. The repository uses the local `.exo/prs.json` PR registry.
