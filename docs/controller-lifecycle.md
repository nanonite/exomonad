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
startup handoff reconciles the stopped controller bindings before clearing this
marker and accepting new dispatch. A completed intent is durable proof even
while it is still active; the next successful controller startup archives it.
If shutdown reports a conflict or timeout, preserve the marker and use
`exomonad init --continue` to run supported recovery after resolving the
reported target evidence.

Continuation after a workspace shutdown reopens an ordered child dispatch whose
controller stopped through that exact shutdown and re-provisions it through the
supported ordered sub-TL endpoint. The successor records a new dispatch
generation but keeps the stopped invocation as its predecessor binding, so the
dead controller is never treated as live. When the stopped controller had
already received a spawn receipt but had not checkpointed the host launch row,
the restarted controller adopts that exact owner once, records the host launch
row, and proceeds with the pending recovery. Replaying the blocked owner's
terminal notification repeats the already-recorded human gate instead of
failing the child, and no worker is relaunched or dispatch state reset. The
`agent.spawned` row is the only launch proof; `agent.resumed` stays the
human-facing resume audit and is never treated as confirmation for this
adoption path.

Terminal-owner human recovery of a historical resume result whose host
predates the launch confirmation is a separate, bounded case. It proves the
saved result from the durable owner invocation lineage, the immediately
following confirmed action, and the exact
`agent.invocation.started`/`agent.resumed`/`agent.invocation.finished` trio,
and it holds when any `agent.spawned` row names that successor. It never
synthesizes a launch row or rewrites the saved response, and the modern
adoption above is unchanged. A historical generation that predates route
provenance is resolved as an evidence gap: the gap is recorded in the recovery
evidence and the next generation is dispatched from the currently configured
harness/model/effort, never from the missing history. The successor records the
new route, and a changed configuration is a routing transition rather than an
owner/scope/worktree conflict. The relaxation is limited to a generation whose
receipt carries a positively proven legacy route gap; a modern record still has
to match its own route fields exactly. The leaf-resume receipt proof has no
controller-epoch field; epoch/process identity is enforced on the controller
ownership path (`verify_controller_epoch`) and on dispatch confirmation, where a
row from a predecessor epoch is retained as historical audit and can never
confirm the current controller's dispatch.

The implementation does not remove worktrees, branches, receipts, journals,
publication evidence, or gates. A server-only restart does not terminate agent
panes. A SIGKILL or host crash can leave stale Running records; continuation
must use the liveness reconciliation path rather than treating the saved status
as proof of a live process.

A committed root controller record whose exact tmux server process (PID and
start ticks) has ended is not live even when a fresh server recycles the same
`@window`/`%pane` ids. Startup relaunches the root controller and classifies the
historical lifetime as superseded; same-server pane and process-start checks
remain strict.

A completed workspace shutdown intent is not permission to ignore a newer
interrupted startup. When init persisted a pending startup target with a newer
controller epoch in the same root scope and the completed operation has not yet
been archived, `exomonad shutdown --workspace` retargets to that exact pending
target and quiesces it. Repeated shutdown with no newer pending epoch still
reports `already_complete`.

Workspace shutdown preserves worker panes. The fresh server that starts with
`init --continue` revalidates retained live invocations against their recorded
routing ownership and reattaches their exit monitors, so an adopted worker
records its terminal outcome instead of staying `running` forever. Workers whose
routing cannot be revalidated are left untouched and are not monitored.

Revalidation requires the recorded delivery target to still exist and its tmux
ownership tag to prove this exact invocation, or a declared parent or shared
sibling. A recorded pane or window that no longer exists, an ownership tag that
names another agent or invocation, a malformed invocation record, and a server
started without a configured tmux session all leave the invocation record
unchanged and unmonitored. Only an existing, unowned target or one already
tagged for this invocation is adopted; a missing target is not treated as a
terminal outcome because no exit marker can be observed.

Live reuse inside a server that is already running is a separate case. Its own
exit monitor is responsible for the terminal outcome, so a target that disappears
while that monitor is finishing does not invalidate the live checkpoint.
