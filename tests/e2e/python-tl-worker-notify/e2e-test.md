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
| Hook commands | the generated `<agent dir>/.codex/config.toml` carries the `pre-tool-use`, `post-tool-use`, and `stop` hook commands; `install_codex_hook_trust` derives its trust entries from exactly those three |
| Hook trust | `[hooks.state."<worker config>:<event>:0:0"]` with a `trusted_hash` for each event, plus `[projects."<agent dir>"] trust_level = "trusted"`, are present in the isolated home |
| Retired shape absent | the isolated home does **not** carry a `# BEGIN EXOMONAD CODEX HOOKS` block. That writer was removed in 8934378f (#210); `trust_codex_project` strips such a block on every write, and `provisioning_writes_hook_commands_into_the_config_not_the_user_config` pins that against live product output |
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
removes the foreign environment. **The harness now does this by default**, via
`e2e_python_tl_isolate_tmux_server` in `tests/e2e/lib/python-tl.sh`, exported at
top level before the first `tmux` call and before `exomonad init`.

An earlier revision reverted it: the isolation call had been placed inside
`cleanup()`, so it took effect only at teardown, and the run kept the host's
`CODEX_HOME`. That much was a placement bug in the harness, and moving the call
to top level fixed it -- the worker's pane now resolves `$CODEX_HOME` to the
run's isolated home, proven live. Set `E2E_PYTHON_TL_TMUX_ISOLATION=0` to
reproduce the shared-server leak.

What was *not* true of that revision is the diagnosis attached to it. It
reported that the isolation "broke the server's socket health check".
`chainlink-codex` fails that check with the isolation disabled too, so the
isolation was never the cause. `exomonad serve` creates `.exo/server.sock` and
then never becomes healthy inside `init`'s 30s budget for that scenario, on the
same host and with the same `codex-messaging` fixture passing. Filed as
Chainlink #1150. Keep the two separate: the isolation is required and works, and
the health check is a separate failure it did not cause.

`test_isolation_contract.py` pins the placement (top level, before the first
`tmux` call, before `init`, after the library is sourced) precisely because a
call inside `cleanup()` is invisible at runtime -- the run still prints
`Work dir:` and still creates the `tmux/` directory, at teardown.

### What actually stalled that run

The 2026-09-30 run was recorded as a `notify_parent` defect. That was wrong,
and the run's own artefacts say so. The worker's Codex rollout holds **nine
events** and ends **5.4 seconds** in:

```
task_complete.error = {"message": "{\"type\":\"error\",\"status\":400,
  \"error\":{\"type\":\"invalid_request_error\",
  \"message\":\"The 'gpt-luna' model is not supported when using Codex with a
  ChatGPT account.\"}}"}
```

There are **zero tool calls**. The worker never attempted `notify_parent`, so it
could not have succeeded, and the controller was never the thing under
observation.

The model comes from `exomonad new`'s scaffold: both `harness_policy.toml` and
`harness_capability.toml` are keyed `codex/gpt-luna`, and the controller splits
that key into agent type plus model, which becomes `model = ...` in the
generated child config. A ChatGPT-account Codex login cannot run it. Every model
probed against this account is rejected the same way -- `gpt-luna`,
`gpt-5-codex`, `gpt-5`, `gpt-5-mini` and `o3` all return that identical 400 --
so this is not one bad name. Filed as Chainlink #1149.

### What the fixtures do about it

`e2e_python_tl_codex_model` resolves the model from the host Codex config --
the same place the operator's own working `codex` invocation gets its model, so
the fixtures follow the account instead of hard-coding a name that rots.
`E2E_CODEX_MODEL` overrides it, and resolving `gpt-luna` fails outright.

`e2e_python_tl_assert_codex_model_runnable` then spends one throwaway turn
proving the account can run it, **before** `exomonad init` starts. That is the
fix that matters, because a model rejection is otherwise invisible: it surfaces
inside the worker's rollout, after dispatch and provisioning, and nothing
notices for the validator's whole 600s budget. So the run reads as a messaging
stall and sends whoever reads it to the wrong bug. The 09-30 run was sent to the
wrong bug exactly that way.

The policy writer also emits the matching `harness_capability.toml` entry now.
`_require_policy_coverage` (`tl_loop/select/capability.py`) rejects a policy
whose allowlist the capability map does not cover, so a fixture that rewrites
only the policy fails preflight with `missing capability entry for
codex/<model>`. That second defect was found by the probe's run, ninety seconds
in, where it would previously have cost another ten-minute stall.

### What is left, once the model works

With a runnable model the worker's turn completes and **both MCP tools return
success** -- see the `codex-messaging` evidence below. The remaining blocker is
on the controller's side, and the run's action journal names it: the controller
quarantines its own `agent.spawned` event as `integrity_conflict` /
`intent_mismatch`, because the agent-reported `run_id` is a UUID where the
controller has the literal `root`, so the slice never leaves `spawned`. Filed as
Chainlink #1148, with the quarantine entry quoted there.

### Live run, 2026-09-30 (isolation correctly placed)

`KEEP_E2E_WORKDIR=1 script -qec ./tests/e2e/python-tl-worker-notify/run.sh`,
work dir `python-tl-worker-notify.t1rzxiYm`. `init` reached "Attaching to
session"; the controller loaded `plan.json`, selected the harness, dispatched
the worker, and wrote a role-correct child config carrying all three hook
commands. The validator recorded:

```
OK: Python TL controller window exists
OK: controller window holds the controller plus a dispatched worker pane
OK: TL plan was consumed into a controller checkpoint
OK: no interactive Codex root TL config was generated
OK: CODEX_HOME propagated into tmux session e2e-python-tl-worker-notify
OK: worker routing metadata exists
OK: worker Codex config exists
OK: Codex worker config is role-correct
OK: Codex project + hook trust in .../codex-home
OK: Codex worker trust is in the isolated home
FAIL: worker notify_parent event recorded timed out after 600s
```

The two trust assertions are the point, and they now pass against real product
output rather than a fixture. Inspecting the run's own isolated home:

* three `[hooks.state."<worker config>:<event>:0:0"]` entries with a
  `trusted_hash`, for `pre_tool_use`, `post_tool_use` and `stop`;
* `[projects."<agent dir>"]` with `trust_level = "trusted"`;
* no occurrence of the retired `# BEGIN EXOMONAD CODEX HOOKS` block;
* the live `codex` process in the worker's pane carried
  `CODEX_HOME=<work dir>/codex-home`, not the host's.

So provisioning, the child config, the trust write and the environment
propagation are all demonstrated. The run then stalls: the slice stays `spawned`,
`fsm.phase` stays `tl_running`, `active_slices=1`, and no progress is made. The
worker boots a real Codex session with the correct task text and never calls
`notify_parent`, so the controller never durably reaches `tl_done`. That is the
remaining blocker. Filed as Chainlink #1148; the harness already provides the
reproduction and needs no change.

The `CODEX_HOME` ordering that the isolation works around is filed separately as
Chainlink #1147.

### A failing run can no longer report success

A missing or failing validator result is a hard failure of the scenario.
`run.sh` used to fall back to `exomonad init`'s exit status, and `init` exits 0
as soon as it attaches the session -- so a run that failed three assertions,
printed no verdict, and left the controller at `tl_running` still exited 0. The
verdict block now exits non-zero when the result file is absent and again when it
reports any failure. `test_isolation_contract.py` pins both.

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
