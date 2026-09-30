# E2E Codex Messaging — Python TL controller dispatches a Codex worker

`exomonad init` does not launch an interactive Codex root TL. The project's root
controller is `tl_loop`, a bounded Python process that consumes
`.exo/tl-loop/plan.json` and dispatches Codex children through the ExoMonad MCP
tools. This scenario is the messaging half of the Codex suite under that
architecture, and it is driven by the plan in [`plan.json`](./plan.json) — there
is no TL prompt to feed and no root agent to read one.

## The chain under test

```text
.exo/config.toml  -> Codex companion `codex-messaging-peer` (spawned by init)
plan.json         -> Python TL controller (TL window, pane 0)
                        -> spawn_worker codex-messaging-sender-codex
                             |   (pane in the controller's own TL window)
                             |
                             +-- send_tmux_message --> codex-messaging-peer
                             +-- notify_parent ----> controller (recipient `root`, tab `TL`)
                        -> terminal slice success -> phase `tl_done`
```

Both legs are still Codex-to-Codex tmux traffic, but neither originates in an
interactive TL agent, because there is no interactive TL agent any more. The
worker reports up to the controller through `notify_parent`, which resolves the
`root` recipient to the `TL` window that hosts the controller.

## Why the peer is a companion and not a second worker

`spawn_worker` refuses to dispatch while another worker is alive in the same
parent window — workers are sequential by design
(`docs/decisions/agent-lifecycle-invariants.md` § Worker sequentiality, enforced
by `active_worker_for_parent_tab`). A plan with two `workers` entries would have
its second dispatch refused, which parks the slice rather than exercising
messaging, so the scenario would assert a park instead of a delivery.

The peer therefore has to be an agent that already exists when the controller
dispatches. A companion is the only such shape that is *addressable*: `init`
writes `routing.json` for agent companions but `continue`s without one for
`process` companions, and a recipient with no routing file is not deliverable.
Declaring the peer a **Codex** companion also puts it through
`provision_codex_agent`, the same lifecycle a dispatched child uses, so the
scenario covers companion and child configuration from one place.

The cost is one extra model session, which is what the retired scenario spent on
its interactive root TL.

## What the validator asserts

| Property | How |
|---|---|
| CODEX_HOME propagation | `tmux show-environment` reports the isolated per-run home. The run also gets its own tmux server (`e2e_python_tl_isolate_tmux_server`, on by default), because `exomonad init` propagates `CODEX_HOME` only *after* creating the session's first window, so a shared server would hand the worker the host's Codex home. See `python-tl-worker-notify/e2e-test.md` |
| Isolated Codex home | project trust + the three hook-trust entries for each Codex agent live in `$CODEX_HOME/config.toml`, and teardown proves the host config is byte-for-byte unchanged |
| Hook commands | the generated `<agent dir>/.codex/config.toml` carries the `pre-tool-use`, `post-tool-use`, and `stop` hook commands; `install_codex_hook_trust` derives its trust entries from exactly those three |
| Hook trust | `[hooks.state."<agent config>:<event>:0:0"]` with a `trusted_hash` for each event, plus `[projects."<agent dir>"] trust_level = "trusted"`, are present in the isolated home |
| Retired shape absent | the isolated home does **not** carry a `# BEGIN EXOMONAD CODEX HOOKS` block; see `python-tl-worker-notify/e2e-test.md` and the note in `tests/e2e/lib/python-tl.sh` |
| MCP tools + role config | the worker declares `mcp-stdio --role worker --name codex-messaging-sender-codex`; the peer declares `mcp-stdio --role worker --name codex-messaging-peer`; both carry `hooks = true`, `approval_policy = "never"`, and the Codex **Worker** Agent Protocol |
| No retired root model | neither `.codex/config.toml` nor `.exo/agents/root/.codex/config.toml` is generated |
| Messaging | `message.delivery` records a successful `agent_inbox_tmux` injection to the peer, and a successful `notify_parent` delivery to `root` |
| Durable controller state | `.exo/tl-loop/root/run.json` records the plan slice and `fsm.phase == "tl_done"` |

## Live run, 2026-09-30

Work dir `codex-messaging.q8sxs0Ek`. This is the first live run of this
scenario in its migrated form, and it is where the migrated fixtures stopped
being theoretical.

**The worker does its whole job correctly.** The worker's Codex rollout holds 47
events and four tool calls, in the order the plan asked for:

```
CALL  exec  await new Promise(r => setTimeout(r, 30000));   -> delay-complete, 30.0s
CALL  exec  tools.mcp__exomonad__send_tmux_message({recipient: codex-messaging-peer, ...})
      OUTPUT {"delivery_method":"tmux_stdin","success":true}
CALL  exec  tools.mcp__exomonad__notify_parent({status: "success", ...})
      OUTPUT {"success":true}
```

The task text reached the worker intact -- user message 2, 5271 characters,
carrying both tool instructions. So the peer is reachable, the worker's MCP
identity routes, and `send_tmux_message` reports a successful `tmux_stdin`
delivery.

**The run still does not finish, and the reason is the controller's side.** The
slice stays `spawned` and `fsm.phase` stays `tl_running` even though both tools
returned success. The action journal names why:

```
tl.dispatch_event_rejected
  classification: integrity_conflict
  correlation_reason: intent_mismatch
```

and `event-quarantine.json` holds the quarantined `agent.spawned` event with
`run_id` a UUID where the controller has the literal `root`, and `agent_id`
`root` where it expects the slice. Filed as Chainlink #1148.

**Observed `run.sh` exit codes.** A run with no result file exits non-zero, which
is the false-pass fix doing its job: `ERROR: validator wrote no result file at
... (init exited 1)` followed by `RUNSH_EXIT=1`. An earlier attempt at the same
scenario, with the scaffold's `gpt-luna` still in the policy, was caught by
preflight inside 90 seconds with `missing capability entry for
codex/gpt-5.6-luna` -- see `python-tl-worker-notify/e2e-test.md` for the model
defect behind it.

This row is **not** Green: the messaging and role assertions above are proven,
but the run does not reach `tl_done`, so the durable-controller-state row is not
yet satisfied.

## What was removed and why

The retired scenario drove `exomonad init` into an interactive Codex root TL
and validated that TL's own `.codex/config.toml` (approval policy, `mcp-stdio`,
and an "ExoMonad Root TL Protocol" marker). Normal Python-controller startup
provisions no Codex agent in the project root at all, so that config is never
generated and asserting it would have pinned a model the product no longer
ships. See `python_controller_startup_generates_no_codex_root_tl_config` in
`rust/exomonad/src/init.rs`.

The old validator also asserted `agent.message_sent` with `success=true` for a
`send_tmux_message` call. `agent.message_sent` is emitted by `send_message`,
not by `send_tmux_message`; the observable record for a tmux send is
`message.delivery` with `method = "agent_inbox_tmux"`, which is what this
validator now asserts. It also matched `tmux_routing`, which is only logged on
a *failed* routing, so the old "delivery succeeded" probe could not have passed.

## Running it

```bash
just e2e-codex-messaging          # live run: needs a real `codex` binary
just check-e2e-codex-messaging   # static: bash syntax only
just check-e2e-python-tl-controller  # the migration contract, no Codex needed
```

Live runs authenticate a real `codex` against the model, so `run.sh` copies the
documented auth artifacts into the isolated home. Keep `KEEP_E2E_WORKDIR=1` to
inspect the generated configs, the isolated Codex home, and the controller
checkpoint after a failure.
