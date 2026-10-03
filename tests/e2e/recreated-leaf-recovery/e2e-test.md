# Root TL rules: recreated-leaf recovery acceptance (#1111)

This acceptance has no interactive root TL. It is a harness that drives the
shipped server and controller tool surface directly and reports one line per
T-item. The rules below are the constraints that makes that legitimate.

## Never do these

- Do not run `exomonad init`, `exomonad serve`, or `exomonad new` anywhere in
  the repository or its worktrees. The only project this acceptance touches is
  the clone it makes inside its own `mktemp -d` directory.
- Do not install anything. `just install-all-dev`, `just install-all`, and any
  copy into `~/.cargo/bin` or `~/.exo` replaces the operator's live binary and
  WASM. Build in this worktree and invoke `target/debug/exomonad` and
  `.exo/wasm/wasm-guest-devswarm.wasm` by absolute path.
- Do not contact the operator's Forgejo on port 3000, its data, or its
  repositories. The run's forge is its own compose project on an ephemeral port.
- Do not read or write the operator's `CHAINLINK_DB`. The run's server and
  controller receive this run's own database through `CHAINLINK_DB`.
- Do not reuse a tmux session name, a compose project name, a directory path, or
  a Forgejo account name across runs. Every run-scoped name is derived from a
  per-run id.
- Do not leave a session, a process, a compose project, or a directory behind.
  A leak fails the run; it is not a warning.
- Do not assert on a duration. Wait on a durable boundary: a ledger record, a
  git worktree registry entry, a forge record, or a recorded refusal.
- Do not add a fallback to make an item pass. An item either proves its
  contract or fails, and a limitation is recorded as a limitation.

## Determinism

- The leaf is a deterministic actor, not a model. It commits one unique payload,
  pushes it, and files exactly one pull request through the shipped `file_pr`.
  It is idempotent per branch: a relaunch onto a branch that already carries its
  payload makes no second commit, which is what lets the acceptance assert a
  head that does not move under it.
- That actor waits for a release file before it does anything, and the harness
  writes that file only after the spawn or resume it issued has returned. The
  actor is therefore provably alive across the server's own readiness check
  without anything depending on a duration, and it records that it waited.
- The retryable probe holds the shared worktree lifecycle lock with a child of
  this harness, which announces on standard output when the lock is actually
  held. A shell utility is not used: its own child can keep the lock open after
  the utility is terminated, and a re-drive would then be refused for a lock
  nobody holds.
- Counts are read after a terminal boundary, never after a quiet period. A count
  that has not moved for a few seconds proves nothing about a duplicate that has
  not arrived yet.

## Cleanup

One trap on `EXIT`, `INT`, and `TERM` removes every resource the run created,
whatever happens, including a failed assertion. It is idempotent, because it
runs on every exit path. After it runs, the scope reports what it can still see
and the run fails if any of it remains. A child process must start in its own
process group (`start_new_session=True`), or the group signal a teardown sends
would kill the harness itself.

Teardown is not the only reclamation path. A SIGKILL, a host reboot, or a Docker
restart takes the trap with it, so every run starts by sweeping for, and
reclaiming, what an interrupted run left behind. The sweep is prefix-driven,
because a run this harness has no record of is exactly what it has to reclaim.

Short socket roots are swept too, under a different rule. A short root exists
because `sun_path` is 107 bytes and `TMPDIR` is not the harness's to set, so it
is created under `/tmp` with the socket prefix (`exo-e2e-sock-`) rather than
with a run's directory prefix — which is exactly why a run-scoped sweep cannot
see one, and why an abandoned one outlived runs on 2026-09-28. A short root is
allocated through `tmuxio.temporary_short_root`, so the allocation sits inside
the block that removes it and a failed `bind` cannot strand the directory. The
sweep is the backstop for what a SIGKILL strands: it reclaims a root that has
been untouched for `SHORT_ROOT_IDLE_SECONDS` with no process running in it, and
leaves a younger one, or one something is still running in, to the run that owns
it. A contract test fails the build if any harness allocates a short root
outside that helper.
