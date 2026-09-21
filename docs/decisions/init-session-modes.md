# Init session modes
 
ExoMonad defaults to --continue because restarting a controller must preserve
the durable run, invocation identities, publication ownership, and worktrees.
The old no-flag behavior was effectively --recreate; repeated recovery could
archive live checkpoints and discard the evidence needed to resume them.
 
The modes are deliberately distinct:
 
1. --continue resumes a non-terminal run, reconciles existing invocation
   identities, and refuses plan drift.
2. --start creates a fresh run only when no non-terminal checkpoint exists.
3. --recreate is destructive and requires an inspected plan plus
   --confirm-recreate; protected PRs require the separate --force-recreate
   override.
 
Legacy sessions without a recorded mode are treated as continue-compatible
state. Init records the migration choice without archiving or deleting runtime
artifacts.

## Failed ordered child recovery

`--continue` may reopen an ordered child whose controller stopped at a
retryable startup, transport, or process boundary. The controller requires the
parent and child checkpoints to agree on the immutable plan, child branch,
worktree, parent ownership, accepted dispatch intent, and action journal. It
revalidates the server identity before the child can issue another effect, then
reuses the existing child checkpoint and dispatch records. A repeated
`--continue` therefore does not spawn a second child or repeat a confirmed
effect.

Recovery decides only from durable checkpoint evidence. A live child controller
that exits before authoritative resolution persists its own recursive failure
checkpoint, so the crash is resumable instead of leaving a running checkpoint.
The `controller-exit.json` marker is diagnostic, so `--continue` trusts it only
when it is bound to the exact child checkpoint revision and failure reason it
was recorded against; a new failure rewrites the marker. A stale marker from an
older transient failure therefore cannot reopen a newer nonretryable failure,
and the recovered child is relaunched in the same invocation instead of
requiring a second controller call.

The controller keeps `tl_failed` and opens a named
`tl-ordered-child-recovery-<child>` gate when any proof is missing or
conflicting. Missing child state, an identity mismatch, an unresolved action
journal entry, completed merge evidence, or an unsafe child failure must be
resolved by the operator through the appropriate recovery or recreate path.
Answering that gate does not authorize the controller to guess ownership or
discard resources.

## Stale reviewer action self-heal

A verdict recorded by older code (or any prior bug) can leave a matching
`REVIEWER_SPAWN` action behind for the same head, which holds the slice at
`await_reviewer_spawn_reconciliation` forever even though review and CI are
green. On every startup/`--continue` and heartbeat reconciliation the
controller repairs that specific inconsistency automatically: it clears the
action only when the verdict is set for the exact `reviewed_head`, the action is
a matching `REVIEWER_SPAWN` in a terminal phase (`confirmed`/`reconciled`), a
`reviewer_attempt` for that head exists, and no `spawn_reviewer` journal entry
for the slice is `intended`/`unknown`. The repair goes through the typed
`slice_transition` reducer and the locked run-state writer, preserves
`reviewer_attempt` and all review evidence, and is idempotent.

Everything ambiguous keeps its existing safety gate: a live `REPAIR` action, an
action for a different head, a non-terminal action, a missing
`reviewer_attempt`, and a pending/unknown journal entry are all left untouched
at `await_repair_reconciliation`/`await_reviewer_spawn_reconciliation`. An
operator resolves those through the normal gate path; no manual `run.json`
editing is ever required or supported.

## Recreate publication authorization

`--recreate` may remove an ordered-controller branch only when every
publication it owns is scheduled for disposal by the same plan and, for a
protected PR, `--force-recreate` was supplied. An unprotected PR that the plan
closes, and a merged or already-closed PR whose record the plan removes, no
longer block branch cleanup; `--force-recreate` authorizes the planned disposal
of a protected one. The running server is stopped before destructive cleanup,
and cleanup proceeds only once termination is verified. The recorded pid must
be plausible (PID 0 and PID 1 are rejected), parse as a real record
(malformed, unreadable, and absent records are distinguished), and still
resolve to an `exomonad serve` process whose kernel-resolved working directory
is this workspace before it is signalled, so a reused pid, a process in another
workspace, and a forgeable argument string are never terminated. An alive pid
that cannot be verified against this record and workspace makes `--recreate`
refuse cleanup rather than risk an unrelated process. A server pid that does
not exit, an invalid or unreadable record, or a socket that still accepts
connections likewise refuses and leaves all artifacts in place. All
ordered branches are then revalidated before any worktree, branch, or identity
is removed, and published PRs are closed before local ownership is removed so a
closure failure leaves the branch and identity intact for a safe retry. Cleanup
tolerates a crash between branch deletion and identity removal by completing
the interrupted disposal on the next run.
