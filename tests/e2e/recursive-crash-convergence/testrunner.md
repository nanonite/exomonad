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

A run owns all of it, with three differences from the scenario above: a case
publishes branches named after the boundary it is exercising, so cases cannot
share a repository, a pass gets its own Forgejo rather than sharing the run's,
and every case starts the CI provider its Forgejo does not have.

| Resource | How it is named |
|----------|-----------------|
| Forgejo | one per pass: `exo-e2e-1057-<run id>forgejo-p<pass>`, its own compose project from `tests/e2e/lib/forgejo/docker-compose.yml`, ephemeral host port, released with `down -v` when that pass ends |
| repository | one fresh repository per case on that pass's instance, named after the case |
| Chainlink database | `chainlink init` inside the case's own directory, seeded with exactly the one issue the case's controller closes |
| tmux | one server per case, on its own socket inside the case's directory, registered with the run scope |
| CI | one `ci_status_actor.py` process per case, registered with the run scope |
| directory | `mktemp -d` under `exo-e2e-1057-`, one subdirectory per case |

`--repetitions 1` is a single diagnostic pass; the default of three is the
acceptance configuration. `--keep` leaves the whole run in place for
inspection, which is how a failed case is read, including that pass's Forgejo:
a run that is keeping its state does not release its instances between passes,
so the KEPT line names the compose projects still up. Each case prints one
`PASS`/`FAIL` line, and the run exits non-zero if any case failed or anything
leaked.

Each pass gets its own Forgejo and releases it when the pass ends. One
instance for the whole run made the container the thing that decided how much of
the matrix ran: it carried all 28 cases of a pass, so on a loaded host a
container the kernel killed turned every case after it into an identical
`Connection refused` at repository creation, and the report read as twenty
boundary verdicts instead of one incident. A per-pass instance bounds that to
one pass, the next pass brings up its own, and a health check between cases
names the instance that stopped answering and says how many cases of that pass
were not attempted. A pass that cannot provision or that loses its instance is
reported as a `FAIL FORGEJO` line naming the compose project; the run carries on
with the next pass, and exits non-zero.

A case never closes its own Chainlink issue on the way out. The row is the
case's only durable record of the `issue_close` boundary, so a case that
leaves it open has failed that boundary rather than tidied up after it. The
database goes at `<repo>/.chainlink/issues.db`, which is where the shipped
controller resolves it; anywhere else is a file no escalation reaches.

## Known blockers: the matrix provisions correctly and still reports 0/28

The run owns, isolates, and tears down everything it touches: a run reports
`0 leaks, 0 cleanup problems, 0 sweep problems` and leaves nothing on the host.
The **cases** are red, and the remaining cause is the one below.

Everything the harness previously got wrong is fixed, and each fix is a
harness-side one:

| Was | Cause | Now |
|-----|-------|------|
| `unsupported controller event type: pr.review` | the seed asked the server to emit watcher observations | the verdict lives on the slice |
| `legacy active phase 'tl_waiting' cannot be resumed safely` | the seed wrote a phase `_ensure_canonical_scope` refuses | the seed checkpoints `TLPlanning` |
| `unable to open database file: <repo>/.chainlink/issues.db` | the database was not where the controller resolves it | anchored at `<repo>/.chainlink` |
| `refusing watcher publication evidence: provenance mismatch` | the seed filed the PR over REST, so `published-heads.json` stayed empty | the seed publishes through the shipped `file_pr`, as the owning child |
| `No TL transition for TLRunning and PRFiled` | the seed published for children behind the barrier | only the released stage's children publish |
| the seeded approval sat at `await_aggregate_review` forever | the repeated-verdict guard returned the state unchanged, dropping the aggregate lifecycle edge the held approval still owed the run | a recognised repeat binds the candidate it left behind |
| every head read `ci_status: unknown` | the run's Forgejo has Actions disabled and no runner, so nothing ever reported a commit status | `ci_status_actor.py` is the instance's CI provider |
| `repeated_state_version_action` on `merge_aggregate` | neither controller invocation was told which repository the case owns, so `_repository_identity` raised and the merge lane never resolved | both invocations carry the case's own `RepositoryIdentity` |
| `'CrashBoundaryTransport' object has no attribute 'project_root'` | the review boundary's base advance read a project root the base client never stores | the transport keeps the root it was constructed with |

The CI and the merge-lane rows are the two the last pass found, and they were the
same class of fault: the run had provisioned everything the controller needs and
then not told the controller about it. The Forgejo it brings up has Actions
disabled and no runner registered, so nothing on that instance ever reports a
commit status and every head reads `ci_status: unknown`;
`_execute_aggregate_merges` and `_direct_merge_evidence` both require `success`
or `neutral`, so the case failed at every boundary that merges, about the
instance rather than about the crash. And `_candidate_lane_key` resolves
`owner/repo` through `_repository_identity`, which raises when the run carries
none: an unconfigured controller reserves no lane, returns the state unchanged,
proposes `merge_aggregate` again on the next pass, and the run parks the slice.
Neither is a change to how the controller treats evidence.

`ci_status_actor.py` is the CI provider for one case's repository: it polls for
open pull requests and posts one `success` commit status per head it has not
reported on, which is what a CI provider does and what the watcher then observes
by polling like any other forge fact. It reports on heads only -- it never writes
a slice, a checkpoint, a review, or a controller event -- so every approval and
every publication in a case is still earned the way the boundary under test earns
it, and a run with no actor fails exactly as it did before.

One log line is a false lead here: `ignoring review without binding findings` is
not the approval. It is the watcher's `[CI TRIGGERED]` notification for the same
PR -- a `pr.review` row carrying `kind: ci_triggered` and no verdict at all --
which reaches the review reducer and finds no findings to bind.

### The one that remains

**The seeded approval has no durable reviewer, so `merge_pr` refuses it.** The
review cases now get all the way to `merge_pr` and stop there:

    EffectFailed: merge_pr for 'sub-a': Canonical merge evidence for PR #1:
    review evidence for PR #1: reviewer identity could not be authenticated:
    Forgejo review author did not resolve to a registered agent

The seed posts its approval with the reviewer token, exactly as it should, and
the watcher records the review -- but `resolve_review_author` binds the review to
an agent through `resolve_reviewer_invocation`, which looks for an invocation
with `trigger: review` for that PR and head
(`agent_resolver.rs:362`). The seed writes its invocations with
`trigger: spawn`, because that is what dispatched them, and no `spawn_reviewer`
ever ran: the slice arrives already carrying its verdict and its review
evidence, so the reducer's `reviewer_attempt` is set and it never proposes one.
The reviewer's approval is therefore an approval nobody was assigned to make,
which is precisely what the merge evidence check exists to refuse.

This is a question about what the acceptance is allowed to seed, not about how it
is provisioned, and it has two honest answers that deserve their own review: have
the seed let the shipped reducer dispatch its own reviewer (so the invocation is
the controller's, and the approval is the spawned stand-in's), or change what the
merge evidence is willing to accept. Writing an invocation record into the
fixture would make `merge_pr` accept an approval no controller ever assigned, and
a matrix that went green that way would be proving nothing about the boundary.

The Forgejo lifetime question from the last pass is still open and still bounded:
one instance per pass, released when the pass ends, with a health check between
cases naming the compose project that stopped answering and how many of that
pass's cases were not attempted.

The walk does not stop at a case failure: every failure is attributed to its own
case and the remaining boundaries still run, so the report distinguishes a
boundary that failed from one that was never attempted. A pass whose *instance*
is gone is the one exception, and the `FAIL FORGEJO` line says which instance
died and how many cases of that pass were not attempted.

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
