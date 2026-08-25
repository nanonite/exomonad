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
