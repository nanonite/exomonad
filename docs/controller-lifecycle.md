# Controller shutdown and restart

ExoMonad has two shutdown scopes:

- `exomonad shutdown` stops only the ExoMonad server listeners. `serve` receiving
  SIGINT or SIGTERM has the same server-only meaning. These paths leave the
  root TL, shared subTL controller work, and independent worker panes running.
- `exomonad shutdown --workspace` explicitly requests workspace control-plane
  shutdown. It persists an intent, fences new agent dispatch, then interrupts
  only the root TL pane identified by the current init ownership record. Ordered
  subTL controllers share that pane and are retired through their exact saved
  invocation bindings. Worker panes in the same window are counted and left
  running. The command refuses if the init ownership record, controller epoch,
  root run ID, tmux server, pane ID, or process-start evidence is missing or
  changed.

The workspace shutdown intent remains present after the server exits. It is a
recoverable transaction marker, not permission to clear RunStore state. The
startup handoff work in #1290 will reconcile the stopped controller bindings
before clearing this marker and accepting new dispatch. If shutdown reports a
conflict or timeout, preserve the marker and use `exomonad init --continue` to
run supported recovery after resolving the reported target evidence.

The implementation does not remove worktrees, branches, receipts, journals,
publication evidence, or gates. A server-only restart does not terminate agent
panes. A SIGKILL or host crash can leave stale Running records; continuation
must use the liveness reconciliation path rather than treating the saved status
as proof of a live process.
