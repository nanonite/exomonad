# Recursive parallel pre-publication recovery

This acceptance starts the real ExoMonad server, development WASM, Unix MCP
transport, disposable Git remote, tmux workers, nested ledger-backed
controllers, and the Forgejo-shaped API. It runs the production controller
through nested dispatch, sibling scheduling, restart barriers, base
revalidation, PR publication, watcher delivery, and review handoff.

The harness records a recovery trace at dispatch, aggregate-review,
base-revalidation, and merge boundaries. Recovery generations, rounds, phases,
and owner identity are copied from persisted SliceState.recovery checkpoints;
multiple checkpoints may legitimately share one invocation generation while
the recovery phase advances. The harness never derives them from generic
controller phases. It continues through the UUID-scoped recovered outcome and
requires a later same-owner invocation generation before claiming resume. It asserts that
a sibling completes while the blocked path waits, the same owner and dirty
worktree survive the restart, action journals converge without duplicate keys,
and one identity-matched, UUID-scoped pr.filed → copilot.review/CI chain proves
the review handoff. If two authoritative recovery checkpoints or that event
chain are absent, or the generation never advances after resume, the run fails
instead of substituting Forgejo requests or
unscoped ledger rows. The complete scenario runs three consecutive disposable
sessions and cleans every server, tmux session, worktree, and mock API.

Mutation smoke checks operate only on the captured evidence object. They cover
eager gating, duplicate resume, timeout override, parent takeover, scope
expansion, and no-op recovery; no production source is copied or mutated.
