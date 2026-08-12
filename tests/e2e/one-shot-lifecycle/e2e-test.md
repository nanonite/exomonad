# One-shot lifecycle E2E

This test uses a deterministic Codex fixture executable, not a live model. The
fixture is launched by ExoMonad through the normal `spawn_leaf`, `resume_pr`,
and watcher paths, so the test exercises a non-Claude harness without spending
model tokens.

The validator covers five lifecycle contracts:

1. A clean Codex exit after `file_pr` produces a verified `PublishedHead` and
   the Forgejo watcher spawns exactly one Codex reviewer.
2. A clean Codex exit without `file_pr` produces neither a publication nor a
   reviewer.
3. Guidance sent after the owner exits is unread durable inbox state and is
   consumed by the next `resume_pr` invocation.
4. Guidance sent to a live Codex worker reaches its exact tmux pane; replacing
   its routing target with a stale pane ID falls back to durable delivery.
5. A deliberately changed Forgejo head SHA is rejected by `resume_pr`; a
   matching SHA resumes the same owner worktree and branch without a sibling.

The script starts only `exomonad serve` plus disposable test infrastructure;
it does not run `exomonad init` or start a user ExoMonad session.
