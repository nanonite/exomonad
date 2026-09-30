# E2E Worker Notify — Python TL controller dispatch and controller-pane delivery

`exomonad init` does not launch an interactive Codex root TL. The project's root
controller is `tl_loop`, a bounded Python process that reads
`.exo/tl-loop/plan.json` and dispatches Codex children through the ExoMonad MCP
tools. This scenario is the delivery half of the Codex suite: a dispatched
Codex worker reports up to the controller with `notify_parent`, and the
notification has to land in the `TL` window that hosts the controller.

This scenario used to be named `subtl-worker-notify`, back when it drove a Codex
sub-TL and its worker. There is no sub-TL in the shipped architecture — the root
controller is the Python process — so the old name described a model that no
longer exists. It is named after what it now tests.

## The chain under test

```text
plan.json
  -> Python TL controller  (TL window, pane 0)
       -> spawn_worker python-tl-worker-notify-worker-codex
            Codex worker pane split into the controller's own TL window
            worker notify_parent -> recipient `root` -> tab `TL`
       -> terminal slice success -> phase `tl_done`
```

Two properties of that chain are the reason this scenario exists:

- **Pane pinning.** The controller keeps running in `TL` pane 0 while its own
  dispatched worker occupies another pane of the same window. A parent
  notification must be injected into the controller's window rather than
  wherever focus happens to be, and it must arrive while the worker pane is
  still live.
- **Recipient resolution.** A worker spawned by the Python controller has no
  model in the root position. `notify_parent` resolves structurally, and
  `resolve_tab_name_for_agent("root")` is what maps the `root` recipient onto the
  `TL` window.

## What the validator asserts

| Property | How |
|---|---|
| Controller dispatch | the `TL` window exists, holds the controller pane plus the controller-dispatched worker pane, and the worker wrote `routing.json` |
| Delivery | the notification marker is recorded, `message.delivery` shows a successful `agent_inbox_tmux` injection to the `root` recipient, and the marker is visible in the controller's `TL` window |
| CODEX_HOME propagation | `tmux show-environment` reports the isolated per-run home |
| Isolated Codex home | project trust + the three hook-trust entries for the worker config live in `$CODEX_HOME/config.toml`; teardown proves the host config is byte-for-byte unchanged |
| Hook trust | the shared `# BEGIN EXOMONAD CODEX HOOKS` block and `<worker config>:pre_tool_use:0:0` state are present in the isolated home |
| MCP tools + role config | the worker config declares `mcp-stdio --role worker --name <agent>`, `hooks = true`, `approval_policy = "never"`, and the Codex **Worker** Agent Protocol |
| No retired root model | neither `.codex/config.toml` nor `.exo/agents/root/.codex/config.toml` is generated |
| Durable controller state | `.exo/tl-loop/root/run.json` records the plan slice and `fsm.phase == "tl_done"` |

### One assertion deliberately dropped

The retired validator asserted that the *active* pane of the `TL` window was not
pane 0 at the moment the notification was observed. That encoded an assumption
about how the recipient target is resolved. The shipped code resolves a recipient
with no `routing.json` through `current_tmux_pane_target`, which reads the
window's current pane, so pinning an index would have pinned an invariant the
delivery path does not hold. The scenario now asserts the property that is
actually true and still worth protecting: the notification reaches the
controller's `TL` window at all, and the controller and its worker share that
window.

## What a live run shows, and what still blocks it

A live run of this scenario drove the whole migrated chain and is what found
most of the defects listed in [Migrated under #1127](#migrated-under-1127).
Reaching a dispatched Codex worker took four fixes beyond the migration itself,
each of them a property of the shipped controller that no static check would
have surfaced:

1. **The controller opens the project's Chainlink database at startup.** Without
   one it exits with `unable to open database file` and `init` reports the TL
   window exiting before startup. The retired interactive-root scenarios never
   started a controller and never needed a database.
2. **The origin has to be an HTTP URL.** `repository_identity` refuses a
   local-path remote, and the controller resolves identity during startup.
   Pushes still go to a local bare repository, so the scenario stays hermetic.
3. **The role ceilings have to sit above the run's declared token budget.** The
   selector conservatively attributes a role's whole share of the run budget to
   that role until it has recorded per-role spend, so a plan whose budget meets
   `exomonad new`'s scaffolded 120000-token worker ceiling parks its only slice
   with `over_budget` before dispatching anything. The harness derives the
   ceilings from the plan instead.
4. **`tmux show-environment` prints `NAME=value`.** Comparing that whole line to
   a bare path fails against a correctly propagated session.

### Two blockers that belong to the product, not to this scenario

Both are properties of `exomonad init` in the checked-in revision, and neither
has a fix that belongs in an e2e harness.

**`init` propagates `CODEX_HOME` after it has already created the session's
first window.** When a tmux server is already running -- another scenario, a
parallel workspace -- the new session attaches to it, so the window that
`init` renames to `Server` and runs `exomonad serve` in keeps the *existing
server's* captured `CODEX_HOME`. The session environment then reads correctly,
so `tmux show-environment` passes, while the server process and every agent it
spawns resolve a different Codex home and no hook trust is seeded. That is the
exact leak `tests/e2e/lib/codex-home.sh` exists to prevent, and the mitigation
already in `init.rs` is defeated by the ordering.

Giving the run its own tmux server (`TMUX_TMPDIR` under `WORK_DIR`, the
isolation `tests/e2e/lib/e2e_harness/tmuxio.py` gives the Python acceptances)
does remove the foreign environment -- a live run then produced a populated
`$CODEX_HOME/config.toml` and a Codex session writing only into the isolated
home. It was reverted because in this environment the ExoMonad server then
failed `init`'s socket health check and the controller was never reached, so
trading a correct Codex home for a dead server is not a fix. The regression was
confirmed by A/B: with the isolation disabled the server starts and `init`
reaches "Attaching to session".

**The blocked `notify_parent`.** The worker is dispatched, provisioned, and
boots a real Codex session with the correct task text; what is unproven in this
revision is the last hop, the worker actually calling `notify_parent` and the
controller durably reaching `tl_done`. The validator already reports which leg
failed, so the next run's result is unambiguous.

Because of the two blockers above this row is **not** marked Green.

## Running it

```bash
just e2e-python-tl-worker-notify          # live run: needs a real `codex` binary
just check-e2e-python-tl-worker-notify   # static: bash syntax only
just check-e2e-python-tl-controller      # the migration contract, no Codex needed
```

Live runs authenticate a real `codex` against the model, so `run.sh` copies the
documented auth artifacts into the isolated home. Keep `KEEP_E2E_WORKDIR=1` to
inspect the generated config, the isolated Codex home, the controller
checkpoint, and the captured panes after a failure.
