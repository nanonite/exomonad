# Running the #1057 acceptance

Static checks (no server or Forgejo required):

    just check-e2e-recursive-crash-convergence

The acceptance requires a dedicated Forgejo repository and Git remote. Set:

    export EXOMONAD_FORGEJO_E2E_URL=...
    export EXOMONAD_FORGEJO_E2E_TOKEN=...
    export EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN=...
    export EXOMONAD_FORGEJO_E2E_OWNER=...
    export EXOMONAD_FORGEJO_E2E_REPO=...
    export EXOMONAD_FORGEJO_E2E_GIT_REMOTE=...
    export CHAINLINK_DB=/absolute/path/to/.chainlink/issues.db
    export EXOMONAD_BEAST_WORKSPACE=/path/to/captured/beast/workspace
    export EXOMONAD_BEAST_CONTINUE_COMMAND='exomonad --project-root {workspace} init --continue'

Run the real matrix with:

    just tl-loop-recursive-crash-convergence-e2e

The runner creates only temporary local state and always stops the server.
The supplied CHAINLINK_DB is read-only source state: every matrix case receives
an isolated SQLite backup, and issue creation, server effects, and cleanup use
that temporary database. No fixture issue is written to the supplied database.
The Forgejo repository and remote must be disposable, because the acceptance
creates branches and pull requests. A missing environment, mock API, crash
marker, journal receipt, authoritative merge observation, or convergence
assertion is a failure; the harness never reports a partial run as passed.

The server matrix defaults to three complete disposable repetitions. Set
EXOMONAD_1057_SERVER_RUNS=1 for a single diagnostic pass; that is not the
acceptance configuration.

For this matrix only, the Codex shim is a deterministic leaf publisher. It
publishes each prepared leaf branch through real Forgejo, allowing the
production watcher and recursive reducers to observe a genuine non-aggregate
file_pr. Other ordered-recursive server probes keep their idle agent shim.

## Captured Beast continuation: explicit waiver

The Beast continuation leg (`beast.run_three_continuations`) is explicitly
waived until an operator supplies an isolated capture. No available capture
satisfies its contract:

- The only non-quarantined capture is the live operator workspace
  (`/home/goya/beast-workspace/workspace/.exo/tl-loop/root`), which is
  off-limits and whose remotes point at the live Forgejo (`localhost:3000`).
- The runner requires a non-terminal checkpoint whose action journal holds
  exactly one `merge_pr` intent. A read-only scan of all 21 `root*` checkpoints
  found no non-terminal checkpoint with a `merge_pr` intent; the only
  checkpoint carrying one is already `tl_done` (terminal).
- Synthesizing a merge intent into a copy would be a seeded journal, which the
  acceptance explicitly rejects as real-server evidence.

This leg is therefore not run and is not reported as passed. It requires a
disposable capture with a disposable remote and exactly one `merge_pr` intent
in a non-terminal checkpoint.

## Topology note: same-order consuming children

`fixture.plan()` keeps same-order concurrency (two children at order 1) but at
most one event-consuming child per stage. A resumed run rebuilds the plan from
the persisted manifest, which cannot carry `source` objects, so two concurrent
event-consuming siblings always fail the production stage-route guard. The
fixture therefore pairs one consuming child (with its own `LazyLedgerSource`)
beside a non-consuming sibling, matching the working ordered-recursive probe.
This is a deliberate reduction from the original two-publisher order-1 stage
and is not equivalent to it; restoring parallel same-order publication requires
a production change to child source reconstruction, not a harness change.
