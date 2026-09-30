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
