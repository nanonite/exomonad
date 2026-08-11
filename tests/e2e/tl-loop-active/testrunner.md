# M5.6 active TL E2E test plan

This test runs the Python TL controller directly against a real scratch repository and bare Git remote. It intentionally starts no ExoMonad server, agent process, interactive TL, or tmux session.

## Phases

1. Create an isolated repository with `main` and a bare `origin` remote.
2. Run the bounded controller with two disjoint slices and a cheap-only `codex/gpt-luna` policy capped at 2,000 tokens.
3. The effect stub creates real worktrees, writes each slice, runs each slice's unittest plan, commits, pushes, and records a scratch PR.
4. The ledger-backed queue delivers spawn, approval, completion, and all-children-done events. Review state and merge decisions are deterministic stubs at the effect boundary.
5. Merge both child PRs into `main`, reconcile both budget charges, and file a real upward summary branch.
6. Assert the terminal run state, gap-free ledger, clean worktrees, no mutation-blocked path, and no manual intervention.

## Assertions

- Both disjoint source files exist on `main`.
- Both child PRs are merged exactly once and the upward PR targets `main`.
- Run state is `tl_done` with offset 7.
- Seven ledger events are consumed with `SequenceStatus.COMPLETE` and no findings.
- Every budget charge is reconciled, no reservation remains, and role spend is 1,200 tokens.
- No `MutationBlocked` exception occurs.
- No tmux session is created; child worktrees are removed before the scratch directory cleanup.
- The shell `EXIT` trap removes the complete scratch tree on success and failure.

## Manual intervention report

The first active run is expected to require no intervention. Any ambiguity or failed assertion must stop the harness and be reported rather than guessed around.
