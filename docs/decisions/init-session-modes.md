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

The controller keeps `tl_failed` and opens a named
`tl-ordered-child-recovery-<child>` gate when any proof is missing or
conflicting. Missing child state, an identity mismatch, an unresolved action
journal entry, completed merge evidence, or an unsafe child failure must be
resolved by the operator through the appropriate recovery or recreate path.
Answering that gate does not authorize the controller to guess ownership or
discard resources.
