# Slice abandonment and redispatch E2E

This harness is the real-server acceptance gate for #935. It uses a disposable
Git repository and mock Forgejo HTTP service, but the ExoMonad server, WASM
plugin, Unix-socket effect transport, Git worktree operations, ledger, and
operator commands are real.

The run must prove, in order, that a closed-unmerged PR is preserved as
publication evidence, the live attempt is parked and disposed, a repeated
abandon is idempotent, and redispatch creates a fresh attempt from the plan
without inheriting PR identity. Each of the three complete cases executes this
sequence twice: once for the root run and once for a real nested-sub-TL run
with parent_run_id=root. The nested sequence uses its own slice, agent,
worktree, branch, invocation, checkpoint, and filtered ledger evidence while
sharing the real server.

Negative controls are part of the acceptance contract. The role-registration
mutation must fail the source-derived tool-surface check; the event, cleanup,
PR-reset, and double-charge mutations are represented by durable assertions in
the same scenario and must not be weakened to final-state-only checks.

Never run this against the operator workspace. Never leave tmux sessions,
worktrees, child processes, or temporary repositories behind.
