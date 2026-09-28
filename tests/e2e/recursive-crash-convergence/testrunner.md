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

Run the real matrix with:

    just tl-loop-recursive-crash-convergence-e2e

The runner creates only temporary local state and always stops the server.

**Chainlink.** No `CHAINLINK_DB` is read. Each case runs `chainlink init`
inside its own temporary directory and seeds exactly the disposable issue that
case needs, so the operator's database is never an input and no case can pass
on a row some other run left behind. The database disappears with the case's
directory.

**Forgejo.** The repository and remote must be disposable, because the
acceptance creates branches and pull requests. A missing environment, mock API,
crash marker, journal receipt, authoritative merge observation, or convergence
assertion is a failure; the harness never reports a partial run as passed.

The server matrix defaults to three complete disposable repetitions. Set
EXOMONAD_1057_SERVER_RUNS=1 for a single diagnostic pass; that is not the
acceptance configuration.

For this matrix only, the Codex shim is a deterministic leaf publisher. It
publishes each prepared leaf branch through real Forgejo, allowing the
production watcher and recursive reducers to observe a genuine non-aggregate
file_pr. Other ordered-recursive server probes keep their idle agent shim.

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
