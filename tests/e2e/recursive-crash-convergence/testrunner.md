# Running the #1117 / #1057 acceptance

Static checks (no server, forge, or project):

    just check-e2e-recursive-crash-convergence

The recreated-publication acceptance provisions everything it needs and takes no
operator input:

    just tl-loop-recursive-crash-convergence-e2e

The crash/restart matrix is the second run in this directory, and it is
equally self-contained:

    just tl-loop-crash-matrix-e2e

The recreated-publication run owns all of it:

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

`./tests/e2e/recursive-crash-convergence/run-matrix.sh` runs the 14-boundary
crash/restart matrix. It provisions everything it needs and takes no operator
input; `--mode server` is the only mode, and it names the only thing that has
ever been true of the matrix — every case runs against a real server this run
started, against a real Forgejo this run brought up.

A run owns all of it, with one difference from the scenario above: a case
publishes branches named after the boundary it is exercising, so cases cannot
share a repository.

| Resource | How it is named |
|----------|-----------------|
| Forgejo | its own compose project from `tests/e2e/lib/forgejo/docker-compose.yml`, ephemeral host port, torn down with `down -v` |
| repository | one fresh repository per case on that instance, named after the case |
| Chainlink database | `chainlink init` inside the case's own directory, seeded with exactly the one issue the case's controller closes |
| tmux | one server per case, on its own socket inside the case's directory, registered with the run scope |
| directory | `mktemp -d` under `exo-e2e-1057-`, one subdirectory per case |

`--repetitions 1` is a single diagnostic pass; the default of three is the
acceptance configuration. `--keep` leaves the whole run in place for
inspection, which is how a failed case is read. Each case prints one
`PASS`/`FAIL` line, and the run exits non-zero if any case failed or anything
leaked.

A case never closes its own Chainlink issue on the way out. The row is the
case's only durable record of the `issue_close` boundary, so a case that
leaves it open has failed that boundary rather than tidied up after it. The
database goes at `<repo>/.chainlink/issues.db`, which is where the shipped
controller resolves it; anywhere else is a file no escalation reaches.

## Known blockers: the matrix provisions correctly and still reports 0/28

The run owns, isolates, and tears down everything it touches: a run reports
`0 leaks, 0 cleanup problems, 0 sweep problems` and leaves nothing on the host.
The **cases** are red, and none of the remaining causes is provisioning.

Everything the harness previously got wrong is fixed, and each fix is a
harness-side one:

| Was | Cause | Now |
|-----|-------|------|
| `unsupported controller event type: pr.review` | the seed asked the server to emit watcher observations | the verdict lives on the slice |
| `legacy active phase 'tl_waiting' cannot be resumed safely` | the seed wrote a phase `_ensure_canonical_scope` refuses | the seed checkpoints `TLPlanning` |
| `unable to open database file: <repo>/.chainlink/issues.db` | the database was not where the controller resolves it | anchored at `<repo>/.chainlink` |
| `refusing watcher publication evidence: provenance mismatch` | the seed filed the PR over REST, so `published-heads.json` stayed empty | the seed publishes through the shipped `file_pr`, as the owning child |
| `No TL transition for TLRunning and PRFiled` | the seed published for children behind the barrier | only the released stage's children publish |

Two things still stop the cases converging, and neither is a harness change:

1. **A seeded approval is refused on findings.** The seed posts a real approval
   on the forge with a durable review id and records that review on the slice,
   and the watcher duly records it (`pr.review`, `verdict: approved`,
   `review_id` matching). The controller still logs `ignoring review without
   binding findings`, because the repeated-verdict guard
   (`driver._route_review_event`) compares the envelope's own `head_sha` against
   the slice's `reviewed_head` while the watcher's head lives in `data`. The
   review is therefore re-derived from scratch instead of recognised as the
   repeat it is, and a re-derived approval needs findings the fixture has no
   honest source for. `review-*` and `spawn-*` stop at
   `await_aggregate_review`; `publication` and `repair` never write their crash
   marker.

2. **The run's own Forgejo dies part-way through.** Around the eighth case,
   `POST /api/v1/user/repos` starts answering `Connection refused`. The matrix
   holds one Forgejo for all 28 cases, so every later case fails at
   provisioning rather than at its boundary. On a host with little free memory
   the container is killed; the harness reports it, and teardown is still clean,
   but the run cannot be trusted end to end.

The walk does not stop at either: every failure is attributed to its own case
and the remaining boundaries still run, so the report distinguishes a boundary
that failed from one that was never attempted.

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
