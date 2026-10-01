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
to top level fixed it -- the worker's pane now resolves `$CODEX_HOME` to the run's
isolated home, proven live. Set `E2E_PYTHON_TL_TMUX_ISOLATION=0` to reproduce the
shared-server leak.

What was *not* true of that revision is the diagnosis attached to it. It reported
that the isolation "broke the server's socket health check". That claim does not
hold: `chainlink-codex` fails the same check with the isolation disabled, so the
isolation was never the cause. This scenario's own 2026-09-30 run cleared the
health check and dispatched with the isolation on. Keep the two separate -- the
isolation is required and works, and the health check is a separate failure
(#1150) that it did not cause.

`test_isolation_contract.py` pins the placement (top level, before the first
`tmux` call, before `init`, after the library is sourced) precisely because a
call inside `cleanup()` is invisible at runtime -- the run still prints
`Work dir:` and still creates the `tmux/` directory, at teardown.

**The blocked `notify_parent`.** The worker is dispatched, provisioned, and boots
a real Codex session with the correct task text. An earlier revision recorded the
last hop as unproven, from a run that stalled with no `notify_parent` event and
`worker notify_parent event recorded timed out after 600s`. That reading was
wrong, and the run's own artefacts say so. See the live-run section below: the
worker does call `notify_parent`, it succeeds, and the notification is delivered
to the controller's window. What does not happen is the controller *advancing*.

A missing or failing validator result is a hard failure of the scenario.
`run.sh` used to fall back to `exomonad init`'s exit status, and `init` exits 0
as soon as it attaches the session -- so that run reported success while proving
nothing. `test_isolation_contract.py` pins that too.

Because of the blocker above this row is **not** marked Green.

## Live run, 2026-09-30 (work dir `python-tl-worker-notify.kT0YrtMB`)

This run is the evidence behind the three fixes above. Ten assertions pass
against real product output, including the two that had never been observed:

```
OK: Python TL controller window exists
OK: TL plan was consumed into a controller checkpoint
OK: no interactive Codex root TL config was generated
OK: CODEX_HOME propagated into the tmux session
OK: worker routing metadata exists
OK: worker Codex config exists
OK: Codex worker config is role-correct
OK: Codex project + hook trust in .../codex-home
OK: Codex worker trust is in the isolated home
OK: controller window holds the controller plus a dispatched worker pane
OK: worker notify_parent event recorded
OK: worker notify_parent tmux delivery succeeded
```

The last two are new, and they are the ones that matter. The run's own
`.exo/logs/python-tl-worker-notify-worker-codex.jsonl` holds the worker's
`notify_parent` and the delivery it produced:

```json
{"type":"agent.notify_parent","data":{"message":"[PYTHON-TL-WORKER-NOTIFY] Codex
 worker notify_parent reached the Python TL controller.","parent":"root",
 "source":"agent","status":"success"}}
{"type":"message.delivery","data":{"method":"Tmux","outcome":"success",
 "recipient":"root","source":"agent"}}
{"type":"message.delivery","data":{"attempt":1,"detail":"TL","method":
 "agent_inbox_tmux","outcome":"success","recipient":"root"}}
```

So worker dispatch, isolated `CODEX_HOME`, hook and project trust, the role-correct
MCP identity, `notify_parent`, and delivery to the `root` recipient through both
tmux methods are all proven here. The controller received the message; that was
never the open question.

### Three fixture defects this run found

Each one was invisible to every static check, and each would have burned the
validator's full 600s budget per assertion.

**A marker grep that could never match.** The delivery assertion passed
`$MESSAGE_MARKER` to `grep` as a basic regular expression. The marker is
`[PYTHON-TL-WORKER-NOTIFY]`, so the brackets open a bracket expression whose `-`
characters are ranges, and GNU grep rejects the whole pattern:

```console
$ grep -R '[PYTHON-TL-WORKER-NOTIFY]' logs
grep: Invalid range end          # exit 2, never matches
```

It now reads `grep -RF`. This is the same defect `chainlink-codex` fixed, tracked
there as `MARKER_FIXED` in `test_contract.py` and here by the same list.

**A `wait_for` probe that was structurally unreachable.** `wait_for` evaluates
its probe with `bash -c`, and `bash -c` starts a *fresh* shell: it inherits
exported variables but not unexported shell functions. The window assertion
passed the bare name `marker_reached_controller_window`, so every poll printed

```
bash: line 1: marker_reached_controller_window: command not found
```

and burned the full 600s -- while this same run's logs held the marker and two
successful deliveries. The property held and the probe could not see it. The
probe is now a `--assert-window-marker` re-entry point that `wait_for` invokes as
`bash "$0" --assert-window-marker "$SESSION" "$TL_WINDOW"`, which is the idiom
`chainlink-codex/validate.sh` already uses for its own subshell assertions.
`test_contract.py::test_no_wait_for_probe_is_an_unreachable_shell_function` pins
the shape for every migrated scenario.

**A validator that could not report its own verdict.** `validate.sh` runs under
`set -euo pipefail` and calls its helpers as bare statements, so a helper
returning 1 on failure aborted the script at the *first* failed assertion --
skipping every later one and never writing `$RESULT_FILE`. `run.sh` then reports
`validator wrote no result file`, which is the opposite of what happened. Both
helpers return 0 now and let the recorded failure count decide; the count is
what `run.sh` already keys on. Pinned as `SET_E_FIXED`.

The model capability preflight is wired here too. `run.sh` now calls
`e2e_python_tl_assert_codex_model_runnable` before `init`, so an account that
cannot run the provisioned model fails in about five seconds with the model's
name instead of stalling for the validator's whole budget and reading as a
messaging failure. Chainlink #1149.

### What still blocks it: #1148, and this run reproduces it

The controller quarantines its own dispatch confirmation, so the slice never
leaves `spawned`. From this run's `.exo/tl-loop/root/`:

```
run.json:            phase tl_running, slice python-tl-worker-notify-worker = spawned
event-quarantine.json: agent.spawned
    correlation        integrity_conflict
    correlation_reason intent_mismatch
    run_id             949d5daf-be5c-4f2a-9307-107225592541   <- controller has "root"
    agent_id           root                                  <- controller expects the slice
```

That is the same signature recorded on the reference branch, reproduced
independently on a different host and a different day, and it is the product's:
the worker's work is complete and correct, and the controller's own event
correlation rejects its confirmation of it. Filed as Chainlink #1148. The harness
needs no further change for it.

Because the slice never advances, `controller reached a terminal phase` cannot
succeed either, so this row stays **Blocked**. Nothing here has been observed
reaching `tl_done`.

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
