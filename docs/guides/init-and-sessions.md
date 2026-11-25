# Init, sessions, and recovery details

Moved verbatim from the root `CLAUDE.md`.

A tmux pane's environment is fixed when tmux spawns the pane's process, so a value
written with `set-environment` after `new-session` returns reaches only the windows
created afterwards. `init` therefore hands `CODEX_HOME` to `new-session` itself as
`-e CODEX_HOME=<path>`, which makes the window it renames to `Server` — and so
every agent `exomonad serve` spawns — resolve the run's Codex home even when a tmux
server started by another process already exists. It then reads that pane's own
process ID and the tmux server's process ID through the same selected socket, waits
for the pane executable to differ from the server executable, then polls
`/proc/<pane-pid>/environ` until it exposes the exported `CODEX_HOME` on three
consecutive reads. The executable link can update before procfs settles on the new
environment, so one matching read at the exec boundary is not enough. A bounded timeout
fails startup with the last observed environment and process details. `tmux show-environment`
is not a substitute for that check: the session and window environments both follow
later writes.

The three init modes are explicit and recorded in `.exo/tl-loop/session-mode.json`
and the run checkpoint. In this surface-only phase, omitting a mode retains the
--continue is the safe default. Supplying more than one mode is rejected.
--start refuses a non-terminal existing run, while --recreate requires an
explicit destruction-plan confirmation. tl_loop status reports the recorded
mode alongside the durable state.

When continuing, ExoMonad compares .exo/tl-loop/plan.json byte-for-byte with
the immutable .exo/tl-loop/plan.snapshot recorded at session creation. Plan
drift is rejected before orchestration resumes. Existing agent directories are
classified from their invocation record and verified publication ownership;
the decision is recorded in each directory's continuation.json. A preserved
invocation keeps its original ID verbatim, while an invocation classified for
recreation carries an explicit reason for the later spawn path.

A completed workspace-shutdown handoff is scoped by the root run and by an
already-ended controller lifetime, not by the tmux session name. `init
--continue` therefore adopts the same run's completed handoff even under a
`--session` override; both the recorded and adopting session names are retained
as archive evidence. The handoff is validated before the controller is
released on both startup paths, so an incomplete, unsafe, foreign, conflicting,
or still-live handoff fails before ownership is committed. The handoff epoch
must not be the epoch being started, a pending target epoch, or the epoch of a
committed controller whose pane is still live; it is not required to equal the
latest epoch marker, because a later failed `init --continue` may have advanced
the marker while the handoff still names the earlier stopped lifetime. Archival
happens only after the controller startup commits, and a remaining archive
failure leaves the pending target and startup fence intact so the next
`init --continue` resumes the exact controller pane and archives once.

Optional local Forgejo CI stack:
```bash
cd forgejo
docker compose up -d
```

**Project setup:**
```bash
exomonad new                     # Bootstrap new project (.exo/config.toml, WASM, rules)
```

**Server management:**
```bash
exomonad reload                  # Clear WASM plugin cache (next call loads fresh from disk)
exomonad shutdown                # Gracefully shut down the running server
```

**Log and evidence management:**
```bash
exomonad logs import --source <path> [--source <path>] [--format auto|jsonl|json|sqlite|text] [--dry-run] [--rebuild] [--include-quarantined]
                                 # Normalize log sources into .exo/analysis/atlas.db. Explicit,
                                 # idempotent, and read-only with respect to the source files.
                                 # Sources under .exo/worktrees-residue/ are forensic evidence about a
                                 # worktree that no longer exists and are excluded unless
                                 # --include-quarantined is passed; the count is reported as
                                 # excluded_quarantined_sources.
exomonad logs drop-segments --older-than-seconds 2592000 [--dry-run]
                                 # Whole-segment retention on closed ledger segments
exomonad logs export --mode aggregate --output .exo/analysis/export
                                 # L4 compile — the only shareable output
exomonad logs measure --output .exo/analysis/measurement [--require-ready]
                                 # Local detectors, incidents, adjudication, measurement gate
```
See [docs/guides/migrating-to-the-tl-loop.md](docs/guides/migrating-to-the-tl-loop.md)
for using `logs import` to bring an older project's history into the same
Failure Atlas view.
