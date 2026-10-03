# Recreated publication and recursive crash convergence (Chainlink #1117, #1057)

This is the real-server acceptance for the recreated-publication correlation,
and the final real-server gate before #1058. It uses a disposable checkout on
this run's own Forgejo, its own Chainlink database, its own tmux socket, the
production Rust server, the generated WASM tools, and the Unix-socket
TransportClient. It takes no operator input: there is no shared instance, no
supplied forge URL, token, or repository, and therefore nothing an operator has
to reserve before the run can start.

## The recreate scenario (#1117)

A deterministic leaf publishes PR A through the shipped controller, the run is
recreated with the shipped `--recreate --confirm-recreate`, a new dispatch
follows under a new controller epoch, and the leaf publishes PR B. Each item
then proves one property against durable evidence -- the forge's own
pull-request listing, the committed ledger, git's worktree registry, the active
and archived checkpoints, the publication registry, and the run's own Chainlink
database:

- PR A is never adopted by the recreated generation; its `pr.filed` row is
  permanent audit evidence and the active run owns only PR B
- no remote branch is left without a registered worktree
- an escalation is recorded at most once per (slice, cause, attempt), each with
  its own Chainlink issue, and no issue the run did not create appears
- the active run does not end in the failure state the bug produced
- the leaf's publication is handed to the run durably
- the harness posts the reviewer's approval and the commit status, and the
  watcher records both

## The crash matrix (#1057)

Each logical operation is exercised immediately before and immediately after
the operation. The restart invokes run_tl_loop with plan=None, so the persisted
recursive manifest and checkpoint -- not external plan input -- select the next
action. The action journal, ledger cursor, state version, PR ancestry, lane
identity, and final state are checked after every restart. A response lost
after an effect is intentionally treated as an unknown outcome; reconciliation
must observe the authoritative server state and must not dispatch a second
effect for the same durable identity.

The matrix covers spawn, publication, review, repair, merge intent, remote
merge, merged adoption, parent synchronization, issue closure, changelog,
bookkeeping push, stage release, aggregate publication, and root finalization.
Same-order sub-TLs run together, later orders remain barriers, and nested
children publish only to their direct parent branches.

Each case owns everything it can reach another case through. Its repository is
created fresh on the run's own Forgejo, because a case publishes branches named
after the boundary it is exercising (`main.sub-a`, `aggregate/<case>/<name>`)
and a repository that already carries them rejects the next case's push as a
non-fast-forward — the second case would then be testing the first case's
leftovers. Its directory holds its own Chainlink database (`chainlink init`
inside the case's own directory, seeded with exactly the disposable issue the
case needs, so the operator's database is never read), its own tmux server and
socket, and its own controller state. The run's teardown removes the compose
project and its volume, which takes every case's repository with it; the run
fails if anything it created outlived it.

The row the case seeds is the only durable record of its `issue_close`
boundary, so a case never closes it on the way out: an issue left open is a
boundary that failed, not a fixture that needed tidying. The database is
anchored at `<repo>/.chainlink/issues.db`, where `exomonad init` and
`build_spawn_env` resolve `CHAINLINK_DB`, because an escalation written to any
other file is one nothing reads.

The seed emits no controller event of its own. `emit_controller_event` is
validated against `docs/observability/controller-event-contract.v1.json`, and
`pr.review` / `ci.status_changed` are watcher observations rather than
controller events — the server refuses them, and fabricating an observation to
satisfy a reducer would assert that a watcher saw a review no watcher watched.
The verdict, reviewed head, review id, and CI status are therefore stated on
the slice itself, and the seed checkpoints `TLPlanning` rather than an active
`TLWaiting`, which `_ensure_canonical_scope` refuses to upgrade for a
multi-stage plan.
