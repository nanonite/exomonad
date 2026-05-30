# Agent Lifecycle Invariants E2E

This harness verifies the local lifecycle invariants from Chainlink #305 through the real ExoMonad hook path and an isolated git repository.

Phases:

1. Start a real ExoMonad server with the devswarm WASM guest in a temp repo.
2. Dirty a tracked worker file and call the worker Stop hook; it must block and name the dirty file.
3. Commit the worker output with the deterministic worker identity from `docs/decisions/agent-lifecycle-invariants.md`.
4. Call the worker Stop hook again; it must allow once the worktree is clean.
5. Dirty a tracked file on a TL feature branch and call the TL Stop hook; it must allow with an actionable nudge rather than silently ending with uncommitted work.
6. Discard the TL throwaway file and prove the worktree is clean again.
