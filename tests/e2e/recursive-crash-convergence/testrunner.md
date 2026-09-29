# Running the #1117 / #1057 acceptance

Static checks (no server, forge, or project):

    just check-e2e-recursive-crash-convergence

The acceptance provisions everything it needs and takes no operator input:

    just tl-loop-recursive-crash-convergence-e2e

A run owns all of it:

| Resource | How it is named |
|----------|-----------------|
| Forgejo | its own compose project from `tests/e2e/lib/forgejo/docker-compose.yml`, ephemeral host port, torn down with `down -v` |
| Chainlink database | `chainlink init` inside the run's own directory, seeded with exactly the two issues the scenario needs; the operator's `CHAINLINK_DB` is never read |
| tmux | its own socket at `<run dir>/tmux-<uid>/default`, reached only through `e2e_harness.tmuxio` |
| directory | `mktemp -d` under `exo-e2e-1117-` |

The run prints one `PASS`/`FAIL` line per item and a verdict carrying the leak,
cleanup, and sweep counts; it exits non-zero if any item failed or anything
leaked. `--keep` leaves the run's state in place for inspection.

## The scenario

`publish_pr_a` -> `confirmed_recreate` -> `new_dispatch` -> `publish_pr_b`,
followed by the properties the shape has to hold: PR A is never adopted, no
branch is orphaned, the escalation path is exercised at most once per
(slice, cause, attempt), the run does not terminally fail, the leaf's
publication is handed to the run durably, and review and CI are posted by the
harness and observed by the watcher.

Every step goes through the shipped binary: `exomonad new`, `exomonad
init --start`, and `exomonad init --recreate --confirm-recreate`. The leaf is
the deterministic actor beside the harness, so no model call is spent, and
every agent type resolves through that shim, so a real agent binary is never
started against a disposable repository.

`init` finishes by attaching to the session, which cannot succeed without a
TTY: a successful run therefore exits 1 with `open terminal failed: not a
terminal`, and anything else is a failure the acceptance reports.

The controller archive is the one this worktree's build produced, installed
into the run's own `HOME`, because the shipped resolution reads
`$HOME/.exo/tl_loop.pyz` and the acceptance must not exercise an operator's
installed archive.

## Crash matrix (chainlink #1057)

`python3 tests/e2e/recursive-crash-convergence/run.py --mode server` runs the
14-boundary crash/restart matrix. It still needs the dedicated repository
environment (`EXOMONAD_FORGEJO_E2E_URL`, `_TOKEN`, `_REVIEWER_TOKEN`, `_OWNER`,
`_REPO`, `_GIT_REMOTE`) and creates its own disposable Chainlink database per
case. Set `EXOMONAD_1057_SERVER_RUNS=1` for a single diagnostic pass; that is
not the acceptance configuration. It refuses the Forgejo-shaped mock.

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
