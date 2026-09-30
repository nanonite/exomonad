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
