# E2E Chainlink Codex — Python TL controller dispatches the Chainlink worker

`exomonad init` does not launch an interactive Codex root TL. The project's root
controller is `tl_loop`, a bounded Python process that consumes
`.exo/tl-loop/plan.json` and dispatches Codex children through the ExoMonad MCP
tools. This scenario is the Chainlink half of the Codex suite under that
architecture.

## The chain under test

```text
harness (operator)  -> chainlink issue create "E2E chainlink codex worker"   # the issue owner
plan.json (harness-authored, embeds the issue id)
  -> Python TL controller (TL window, pane 0)
       -> spawn_worker chainlink-codex-worker-codex
            worker: chainlink_session_start
            worker: chainlink_session_work <issue>
            worker: chainlink_issue_comment <issue> [CHAINLINK-CODEX-WORKER-COMMENT] ...
            worker: chainlink_session_end  [CHAINLINK-CODEX-WORKER-DONE] ...
            worker: notify_parent success
       -> terminal slice success -> phase `tl_done`
```

## Why the issue is created by the harness

The retired scenario had the interactive Codex root TL call
`chainlink_issue_create` and then `chainlink_issue_close`. Under the shipped
architecture there is no model in the root position: the Python controller
never calls a Chainlink tool, and the Chainlink tool matrix
(`docs/architecture/agent-system.md`) grants `chainlink_issue_create` and
`chainlink_issue_close` to `root`/`tl` only — never to a worker.

So the ownership assertion inverts, and becomes stronger. The harness plays the
role of the human operator that owns the issue; the dispatched Codex worker may
only *read and comment* on it and may only own a session. The validator proves
the worker did **not** close the issue and did **not** create a Chainlink lock
worktree. A worker that closed the issue would be a role-boundary violation
even if the run otherwise looked healthy.

## What the validator asserts

| Property | How |
|---|---|
| CODEX_HOME propagation | `tmux show-environment` reports the isolated per-run home. The run also gets its own tmux server (`e2e_python_tl_isolate_tmux_server`, on by default), because `exomonad init` propagates `CODEX_HOME` only *after* creating the session's first window, so a shared server would hand the worker the host's Codex home. See `python-tl-worker-notify/e2e-test.md` |
| Isolated Codex home | project trust + the three hook-trust entries for the worker config live in `$CODEX_HOME/config.toml`; teardown proves the host config is byte-for-byte unchanged |
| Hook commands | the generated `<agent dir>/.codex/config.toml` carries the `pre-tool-use`, `post-tool-use`, and `stop` hook commands; `install_codex_hook_trust` derives its trust entries from exactly those three |
| Hook trust | `[hooks.state."<worker config>:<event>:0:0"]` with a `trusted_hash` for each event, plus `[projects."<agent dir>"] trust_level = "trusted"`, are present in the isolated home |
| Retired shape absent | the isolated home does **not** carry a `# BEGIN EXOMONAD CODEX HOOKS` block. That writer was removed in 8934378f (#210) and `trust_codex_project` strips such a block on every write, so finding one means a superseded code path edited the file and Codex would load hooks ExoMonad never hashed |
| MCP tools + role config | the worker config declares `mcp-stdio --role worker --name <agent>`, `hooks = true`, `approval_policy = "never"`, and the Codex **Worker** Agent Protocol |
| No retired root model | neither `.codex/config.toml` nor `.exo/agents/root/.codex/config.toml` is generated |
| Chainlink role workflow | the comment landed on the issue, the session ended, and the completion notification was delivered |
| Chainlink ownership | the issue is still open and no `.chainlink/.locks-cache` worktree exists |
| Durable controller state | `.exo/tl-loop/root/run.json` records the plan slice and `fsm.phase == "tl_done"` |

## Live run, 2026-09-30

Work dir `chainlink-codex.chKmnbsu`, then `chainlink-codex.WsMhnhZk`, both under
`~/.cache/exomonad-e2e`. This is the first live evidence for this scenario in
its migrated form. **The row is still not Green** — see *What still blocks it*.

The worker did its whole job against real product output. The run's own
`.exo/logs/chainlink-codex-worker-codex.jsonl` holds five `tool.called` records
in the order the plan asked for, every one `"success": true` on `"role":
"worker"`:

| # | Tool | Arguments |
|---|---|---|
| 1 | `chainlink_session_start` | `{}` |
| 2 | `chainlink_session_work` | `{"issue_id": 1}` |
| 3 | `chainlink_issue_comment` | `{"issue_id": 1, "message": "[CHAINLINK-CODEX-WORKER-COMMENT] …"}` |
| 4 | `chainlink_session_end` | `{"notes": "[CHAINLINK-CODEX-WORKER-DONE] …"}` |
| 5 | `notify_parent` | `{"status": "success", "message": "[CHAINLINK-CODEX-WORKER-DONE] …"}` |

The comment landed on the harness-owned issue, and the run then recorded two
`message.delivery` events with `"recipient": "root"` and `"outcome": "success"`
— the first via `Tmux`, the second via `agent_inbox_tmux`. The controller
window, the durable checkpoint, `CODEX_HOME` propagation into the tmux session,
the role-correct worker config, and the three hook-trust entries plus project
trust in the isolated home all passed.

Twelve of the validator's assertions pass. The two that remain are the durable
terminal-phase checks, and they fail for the reason below. The second run
(`WsMhnhZk`) reproduced the worker's five calls exactly and additionally proved
the `grep -RF` fix: `worker Chainlink session completion recorded`, which had
cost the first run its full 600s budget, now passes.

### Three fixture defects the run found

**A plan-render guard that refused a correct plan.** It required exactly one
`{{CHAINLINK_ISSUE_ID}}` placeholder, while the plan references the issue three
times on purpose — once in the task's opening line and once per tool call that
takes an issue id. It now requires a non-zero count and relies on the
unrendered-placeholder check, which is the one that actually matters, since it
fires unless *every* occurrence was substituted.
`test_inventory.py::test_chainlink_codex_plan_renders_its_issue_id_everywhere`
pins both halves and fails if the old requirement returns.

**A validator probe that could never succeed.** `worker Chainlink session
completion recorded` grepped `.exo/logs` for `$DONE_MARKER`, which is
`[CHAINLINK-CODEX-WORKER-DONE]`. As a basic regular expression the brackets open
a bracket expression over `CHAINLINK-CODEX-WORKER-DONE`, whose `-` characters
become ranges, and GNU grep rejects the pattern outright:

```console
$ grep '[CHAINLINK-CODEX-WORKER-DONE]' log
grep: Invalid range end          # exit 2, never matches
$ grep -RF '[CHAINLINK-CODEX-WORKER-DONE]' log
{"…","notes":"[CHAINLINK-CODEX-WORKER-DONE] Codex worker session complete."}…
```

The first run lost the assertion's full 600s budget to this and reported a
timeout for a marker the worker had written four lines earlier in the same
file — with stderr discarded, a grep that always fails reads as a slow agent.
`grep -RF` fixes it, and
`test_contract.py::test_migrated_validator_greps_bracketed_markers_as_fixed_strings`
pins it. After the fix the assertion passes live in the second run.

**A validator that could never report its own verdict.** This is the one that
mattered most, because it turned a diagnosable failure into a misleading one.
`validate.sh` runs under `set -euo pipefail` and calls its assertion helpers as
bare statements, so a helper returning 1 on failure aborted the script at the
*first* failed assertion — skipping every later assertion and never writing
`$RESULT_FILE`. `run.sh` then reported `validator wrote no result file`, which
is the opposite of what happened: the validator had already recorded real
failures, and every one was thrown away.

Reproduced against the preserved second run, same inputs, only the fix reverted:

| | verdict file | what `run.sh` says |
|---|---|---|
| before the fix | never written | `validator wrote no result file` |
| after the fix | `Failures: 4`, all four named | the four named failures |

`wait_for` and `check` now return 0 and let the recorded failure count decide,
which is what `run.sh` already keys on. The timeout is still recorded as a
failure, so the fix cannot be satisfied by making the helpers silent.
`test_contract.py::test_migrated_validator_survives_a_failing_assertion` pins it.
**`codex-messaging` and `python-tl-worker-notify` have all three defects** —
the marker grep, the `set -e` abort, and the plan guard is chainlink-only — and
are not fixed here. That is `PENDING_MARKER_FIX` and `PENDING_SET_E_FIX` in
`test_contract.py`, owned by #1152 and #1154.

### What still blocks it

The controller quarantines its own dispatch confirmation. The action journal ends
with `tl.dispatch_event_rejected`, `classification: integrity_conflict`,
`correlation_reason: intent_mismatch`, and `event-quarantine.json` holds the
`agent.spawned` event carrying `run_id` a UUID
(`b7e9e78d-13af-43ac-97bb-9146e23cd160`) where the controller has the literal
`root`, and `agent_id` `root` where it expects the slice
(`chainlink-codex-worker`). The slice therefore never leaves `spawned`,
`fsm.phase` stays `tl_running`, and `last_progress_at` never advances past
dispatch. That is Chainlink #1148, and it is the product's, not the harness's:
the worker's five calls all returned success.

The socket health check that used to stop this scenario before dispatch
(`Server socket exists but health check failed after 30s`, #1150) is gone from
this fixture. The cause was the fixture claiming no `port`, so `serve` contended
for the default `0.0.0.0:7433` with a server an earlier run had left behind; the
fixture now sets `port = 0`, as `claude-only` and `claude-teams-inbox` already
did. The two product defects behind it — `serve` is never reaped, and the
leftover socket turns a fast failure into a 30s wait — are #1150 and are not
fixed by this.

## What was removed and why

The retired scenario validated the interactive root TL's own `.codex/config.toml`
against the "ExoMonad Root TL Protocol" marker, and asserted that the root TL
closed the issue. Normal Python-controller startup provisions no Codex agent in
the project root, and the controller holds no Chainlink authority, so both
assertions described a model the product no longer ships. See
`python_controller_startup_generates_no_codex_root_tl_config` in
`rust/exomonad/src/init.rs`.

The retained role-scoped session workflow also drops `chainlink_session_status`,
which the worker role does not have, and keeps the four calls the matrix grants
`worker`: `chainlink_session_start`, `chainlink_session_work`,
`chainlink_issue_comment`, and `chainlink_session_end`.

## Running it

```bash
just e2e-chainlink-codex          # live run: needs a real `codex` binary
just check-e2e-chainlink-codex    # static: bash syntax only
just check-e2e-python-tl-controller  # the migration contract, no Codex needed
```

Live runs authenticate a real `codex` against the model, so `run.sh` copies the
documented auth artifacts into the isolated home. Keep `KEEP_E2E_WORKDIR=1` to
inspect the generated config, the isolated Codex home, the controller
checkpoint, and the fixture's Chainlink database after a failure.
