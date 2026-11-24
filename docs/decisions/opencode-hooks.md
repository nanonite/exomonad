# OpenCode Hook Integration

## Context

ExoMonad's hook system gives Claude Code and Codex agents behavioral guardrails and context steering at tool call boundaries. Claude Code hooks work via `settings.local.json` + `exomonad hook <event>` shell commands. Codex hooks work the same way via its project configuration. Both ultimately call `exomonad hook <event> --runtime <runtime>` which reads JSON stdin, forwards to the WASM server, and returns a `HookEnvelope`.

OpenCode agents were excluded from hooks because OpenCode doesn't support the shell-command-in-JSON-config model. The `opencode.json` configuration has no `hooks` key.

## Decision

OpenCode v2 provides a TypeScript plugin API. A plugin is a local npm-style package (Bun resolves `.ts` natively, no compilation needed) referenced in `opencode.json` via a `"plugin"` array. The default export is a definition object `{ id, setup }`, and `setup(ctx)` registers handlers on the host context with `ctx.tool.hook("execute.before", ...)`, `ctx.tool.hook("execute.after", ...)`, and `ctx.event.subscribe(...)`. This is the same shape the bundled `context-mode` v2 plugin uses.

Because the setup handlers can spawn a subprocess, they call `exomonad hook <event> --runtime opencode` exactly like the other runtimes. This makes the TypeScript plugin a thin bridge to the existing WASM dispatch path with no new server-side logic.

## Bridge Pattern

```
OpenCode v2 (TypeScript plugin, in-process Bun)
  ├── ctx.tool.hook("execute.before")
  │     └── node:child_process → exomonad hook pre-tool-use --runtime opencode
  │           └── stdin: { tool_name, session_id, call_id, args }
  │                 └── normalize_opencode_hook_payload (Rust CLI)
  │                       └── UDS → exomonad serve → WASM dispatch
  │                             └── Haskell role handler → HookEnvelope { stdout, exit_code }
  │                 └── TypeScript reads stdout, denies or merges into input
  │
  ├── ctx.tool.hook("execute.after")
  │     └── exomonad hook post-tool-use --runtime opencode
  │
  └── ctx.event.subscribe (terminal session.execution.* event)
        └── exomonad hook stop|worker-exit --runtime opencode
```

All hook logic lives in Haskell WASM — the plugin is purely glue.

## Payload and Tool-Name Normalization

OpenCode's v2 plugin payload is not the internal `HookInput` shape. Before the
CLI posts to `exomonad serve`, `normalize_opencode_hook_payload` in
`exomonad-core/src/protocol/hook.rs` performs the single OpenCode mapping:

| OpenCode payload | Internal field |
|---|---|
| `args` | `tool_input` |
| `output` | `tool_response` |
| `call_id` | `tool_use_id` |
| `session.execution.*` `data.sessionID` / `durable.aggregateID` | `session_id` |
| `hook` subcommand event (`pre-tool-use`, `post-tool-use`, `stop`, `worker-exit`) | `hook_event_name` |

The v2 CLI sends `shell` where the v1 plugin sent `bash`
(verified against OpenCode v2.3.2 with `ctx.tool.list()` and live tool calls).
The same adapter maps the provider ids onto the canonical OpenCode ids that
`.exo/lib/HookPolicy.hs` matches for `Runtime::OpenCode`:

| OpenCode provider id | Canonical id |
|---|---|
| `shell`, `bash`, `run_command` | `shell` |
| `read` | `read` |
| `write` | `write` |
| `edit` | `edit` |
| `patch`, `multiedit` | `patch` |
| `grep`, `glob`, `webfetch`, `websearch` | unchanged |
| `list`, `ls` | `list` |
| `subagent`, `task` | `subagent` |
| `exomonad_<tool>` (ExoMonad MCP server) | `<tool>` (e.g. `exomonad_approve_pr` → `approve_pr`) |
| other MCP tools | unchanged server-prefixed id |

An id that is not in the table above, is not an `exomonad_<tool>` id, and does
not contain an underscore (the MCP `<server>_<tool>` shape) is **denied on
PreToolUse**. Failure is closed: the CLI prints a deny envelope and exits 2
before it looks for `exomonad serve`, and the reason contains the exact id and
`canonical_opencode_builtin`, the table to extend in
`exomonad-core/src/protocol/hook.rs`. Every role gets this deny; there is no
per-role allowlist and no env/flag bypass. PostToolUse, Stop, and worker-exit
never deny: they log the same `hook.opencode_unknown_tool` warning and pass the
id through as observation only. MCP ids keep their server prefix and are
debugged as `hook.opencode_mcp_tool`.

## Deny Exit Code

`exomonad hook` prints the `HookEnvelope.stdout` and exits `2` when
`continue=false`. The plugin must parse stdout even when `execFile` reports a
non-zero exit; treating `_error` as a spawn failure would turn every enforced
deny into an allow. `callHook` therefore parses whatever stdout it received
first and only falls back to `{ continue: true }` when stdout is absent or not
JSON.

## Event Mapping

| OpenCode v2 event | ExoMonad HookEventType | HookDispatch |
|---|---|---|
| `execute.before` | `PreToolUse` | `ToolUse` |
| `execute.after` | `PostToolUse` | `ToolUse` |
| `session.execution.succeeded` | `Stop` | `Stop` |
| `session.execution.failed` | `Stop` | `Stop` |
| `session.execution.interrupted` | `Stop` | `Stop` |
| process end (process `exit` handler) | `WorkerExit` when `EXOMONAD_ROLE=worker`, otherwise `Stop` | `WorkerExit` / `Stop` |

`session.stopped` from the v1 API no longer exists in v2. A one-shot
`opencode run` emits `session.execution.succeeded` at the end and does not emit
`session.idle`.

The terminal `session.execution.*` event marks the end of one **execution**
(one prompt turn), not the end of the OpenCode process. In `opencode run` those
coincide, but the interactive companion form `opencode mini` keeps the process
alive for the next prompt. Sending `WorkerExit` from the terminal event closed
a worker companion's window after its first turn, so the terminal event always
reports `Stop`, and the process-end hook owns `WorkerExit`. The exit hook runs
from the process `exit` handler with `spawnSync`, because the process is
already tearing down and an async `execFile` would be killed before it
finished. v2.3.2 runs that handler after the setup disposer on `mini` `/exit`
and when `run`'s one-shot server is reaped; a plugin reload disposes setup but
is not a process exit, so the disposer deliberately does not deliver it. The
once-only guard lives on the process global, so a reload cannot double-deliver
either. A user quit is delivered exactly once; a SIGKILL or crash emits no
hook. A non-worker interactive session reports one `Stop` per completed turn
and no second `Stop` at exit (that would duplicate the `turn_end` record),
except a quit before any execution completes, which reports one `Stop` at
process end. A worker session reports `Stop` per completed turn and exactly one
`WorkerExit` at process end.

## Plugin Package

The plugin is embedded as Rust compile-time constants in `rust/exomonad-core/src/opencode_plugin.rs` (`OPENCODE_PLUGIN_TS`, `OPENCODE_PLUGIN_PKG_JSON`). At spawn time, Rust writes these to `.exo/opencode-plugin/` inside the agent's working directory:

```
<agent-dir>/
├── opencode.json          { "mcp": {...}, "plugin": ["./.exo/opencode-plugin"] }
└── .exo/opencode-plugin/
    ├── index.ts            TypeScript bridge (callHook helper + hook handlers)
    └── package.json        { "name": "@exomonad/opencode-plugin", "type": "module" }
```

No external npm install is required. Bun resolves `.ts` natively.

## Version Support Decision

The generated module is **v2-only**: its default export is
`{ id: "exomonad", setup }`, with no legacy `server` export. OpenCode v1.14
used a different plugin API (`export const server` returning a hooks object),
but v2.3.2's loader rejects any module whose default export lacks `{ id, setup }`.
A dual-shape module was considered, but v1.14.48 is not installed on the
verification host, so the v1 half could not be exercised. Exporting an
unverified v1 path would be a silent, untested fallback, so this bridge drops
v1.14 plugin hooks. No v2 code path sniffs the CLI version.

**OpenCode 1.14: plugin hooks are not emitted (v2-only plugin); unverified,
unsupported.** This is the decided status, not a pending question: the v1 half
cannot be verified here, so the bridge does not claim or attempt it.

## Spawn Sites

`AgentControlService::write_opencode_plugin_files(dir)` is called alongside every `opencode.json` write:

- `spawn_subtree` — OpenCode TL agents (worktree)
- `spawn_worker` — OpenCode worker agents (agent config dir, which is the worker's CWD)
- `init.rs` — root OpenCode TL (both `.exo/agents/root/` and repo root CWD)

## Response Format

`to_runtime_json(&Runtime::OpenCode)` returns Claude-format output (`{ "continue": true }` / deny). The TypeScript bridge parses this and applies tool arg mutations or passes through unchanged.

## Context Steering Use Cases

Because pre/post hooks intercept every MCP tool call, they enable WASM-enforced workflow steering:

- **`file_pr` before**: inject PR body guidelines, enforce title format, block if `git push` hasn't run
- **`notify_parent` before**: enforce structured notification vocabulary (`[FIXES PUSHED]`, `[FAILED]`, etc.)
- **`merge_pr` after**: inject next-wave verification reminder into the post-hook context

This logic lives in Haskell WASM role handlers, not the TypeScript plugin. The plugin calls the binary; the binary routes to WASM; WASM applies role-specific logic.

## Code-mode `execute` and inner calls (verified)

OpenCode v2.3.2 ships a code-mode `execute` tool. It is in the default tool set
— a session with a plain `opencode.json` can call it — but the tool catalog
*inside* the script is empty and every inner call fails with
`Unknown tool 'shell'. Use search to find available tools.` No built-in tool
call can be wrapped until code mode is enabled with
`experimental.full_codemode: true` in `opencode.json` (or
`OPENCODE_FLAGS=full_codemode`).

With full code mode enabled, inner calls are **hooked individually**: each
`tools.<name>` call fires `execute.before` again with its own provider id
(captured for `shell` and `write`), its own arguments, and its own `call_id`,
and it fires `execute.after` when it completes. A deny thrown by the before hook
for an inner id blocks that inner call before it runs and surfaces the reason
inside the script's result; the outer `execute` still completes.

Captured against v2.3.2 with `opencode-go/beam-free` and a probe plugin that
wrote every hook payload to JSONL. One `execute` script called
`tools.shell({command: "echo EXO_DENY_ME"})` and
`tools.write({path: "allowed.txt", content: "allowed write"})` inside
`Promise.allSettled`; the probe denied the shell call:

| # | event | `tool` | args |
|---|-------|--------|------|
| 1 | `execute.before` | `execute` | `{code: "...tools.shell(...); tools.write(...)"}` |
| 2 | `execute.before` | `shell` | `{command: "echo EXO_DENY_ME"}` |
| 3 | `execute.before` | `write` | `{path: "allowed.txt", content: "allowed write"}` |
| 4 | `execute.after` | `write` | completed, file created |
| 5 | `execute.after` | `execute` | completed, result contains the deny reason |

`shell` has no `after` event because the deny ran first, and the session output
carried `probe deny: shell blocked by execute.before` as the rejected promise's
reason. So a role deny on `shell`/`edit`/`patch`/`write` is **not** bypassable by
wrapping the call in `execute`, and the adapter keeps `execute` → `execute`.

The captured sequence is committed as
`rust/exomonad-core/tests/fixtures/opencode_v2_codemode_hook_events.jsonl`.
`tests/opencode_plugin_v2.rs` has two checks:

- `captured_codemode_inner_calls_normalize_to_guarded_ids` (always runs) replays
  those payloads through `normalize_opencode_hook_payload` and
  `opencode_unknown_tool_deny`, and fails if an inner id is collapsed onto
  `execute` or is no longer a known id.
- `codemode_inner_calls_are_hooked_individually_when_enabled` (opt-in with
  `EXOMONAD_OPENCODE_V2_PROBE=1`) re-runs the session against the real CLI with
  the production plugin and fails when inner calls stop reaching
  `execute.before`, when the deny stops blocking `shell`, or when the allowed
  `write` stops running. It uses its own `allowed-code-mode.txt` target.

## Verified Against

OpenCode v2.3.2, with the generated plugin written by the production
`write_opencode_plugin_files` path into a disposable Git repository and the
real CLI launched as:

```bash
opencode run --standalone --auto --model opencode-go/beam-free '<one bash call>'
```

With the v2 plugin in place the loader emits no warning, and a probe shim on
`PATH` recorded `hook pre-tool-use --runtime opencode` and `hook
post-tool-use --runtime opencode` for the tool call, followed by `hook stop
--runtime opencode` for `session.execution.succeeded`. A deny response blocked
the tool (`Error: <reason>` surfaced and the command did not run), an
`updatedInput` response replaced the arguments (the post hook observed the
mutated arguments), and `EXOMONAD_ROLE=worker` produced `hook worker-exit
--runtime opencode`.

The role-filtering path was then verified end-to-end against the real built
`exomonad` binary and a real `exomonad serve` (no hook shim): a `dev` agent's
`shell` call carrying a `gh` command was denied by `HookPolicy`, a `tl` agent's
`shell` file write (`printf x > file`) was denied by the implementation guard,
and a plain `touch` was allowed. The server trace showed `runtime=OpenCode`,
`tool="shell"`, `exit_code=2`, and the deny reason for the denied calls.

## Interactive `opencode mini` (verified)

`exomonad init` launches a task-less OpenCode companion as
`opencode mini --standalone --model <provider/model#variant> --agent
exomonad-<role>`, so the hooks must work in that interactive form, not only in
`opencode run`. The form was driven non-interactively on v2.3.2 with the free
`opencode-go/beam-free` model: a private tmux server (`tmux -S <scratch>/t.sock`
with `TMUX` and `TMUX_PANE` unset), the production
`write_opencode_plugin_files` package, one prompt that requires shell calls, and
`/exit`. `mini` requires a TTY stdout; a pipe fails with `opencode mini requires
a TTY stdout`.

`--standalone` is required for the process-end hook. Without it `mini` connects
to the shared managed background service, the plugin lives in that service
rather than in the companion, and `/exit` delivered no hook at all: a captured
run with the managed service on its own port produced only the per-turn
`stop`, and `worker-exit` appeared only when the service itself was stopped.
With a private server the plugin lifecycle belongs to the companion and `/exit`
delivers exactly one `worker-exit`.

| Check | Result | Evidence |
|---|---|---|
| (a) pre-tool-use fires with the normalised tool name and input | **YES** | plugin payload `{"tool_name":"shell","args":{"command":"touch mini-allowed.txt"}}`; the real CLI delivered it to the server as `event=PreToolUse tool="shell"` and the WASM log recorded `input={"command":"touch mini-allowed.txt"}` (that is `tool_input`, not `args`) |
| (b) post-tool-use fires with the tool response | **YES** | plugin payload `{"tool_name":"shell",...,"output":{"exitCode":0,"output":"(no output)","wallTimeSeconds":0.001}}`; the server received `event=PostToolUse tool="shell"`, and `normalize_opencode_hook_payload` maps `output` to `tool_response` (pinned by `captured_mini_hook_payloads_normalize_to_tool_input_and_response`) |
| (c) a role deny blocks the tool inside the mini session | **YES** | with the real `exomonad` binary and a real `exomonad serve` (`EXOMONAD_ROLE=dev`, agent `mini-dev`), the shell call `gh --version` was answered by the server with `exit_code=2 response={"continue":false,"stopReason":"BLOCKED: Do not run gh commands from agents. ..."}`; the tool produced no post-tool-use hook and no output, while `touch mini-allowed.txt` was allowed and created its file |
| (d) stop / worker-exit on normal end and on user quit | **YES** | normal end (one completed turn): `hook stop --runtime opencode` with the `session.execution.succeeded` payload. User quit with `/exit` in a worker session: exactly one `hook worker-exit --runtime opencode` from the process `exit` handler. No hook fires on SIGKILL or a crash |

Before the process-end fix, the terminal event was sent as `worker-exit` for
`EXOMONAD_ROLE=worker`. Because `mini` keeps running, that closed the
companion's window after its first turn and nothing was delivered on quit. The
old-code failure is visible in `old-code-red.log` of the #1589 evidence: the
hook log showed `pre-tool-use`, `post-tool-use`, `pre-tool-use`,
`worker-exit` and no `stop` while the session was still alive.

The opt-in `mini_interactive_hooks_when_enabled` test
(`rust/exomonad-core/tests/opencode_plugin_v2.rs`,
`EXOMONAD_OPENCODE_V2_PROBE=1`) reproduces the whole run on the free probe
model. It starts its own `tmux -S` server inside a temporary directory, runs
`opencode mini` with a prompt that produces one allowed and one denied shell
call, asserts the pre/post/stop hooks and the deny (the deny is a probe shim
response, the same technique #1586 used; the real-binary deny above was
measured separately), then types `/exit` and asserts exactly one `worker-exit`.
It skips cleanly when `opencode` or `tmux` is unavailable. Re-run it after an
OpenCode upgrade.

## OpenCode v2 plugin loader (root cause)

The pre-fix `OPENCODE_PLUGIN_TS` exported only the v1 `server` function. On
v2.3.2 the loader rejects that module:

```
level=WARN message="failed to load plugin"
cause="Plugin must export a default definition with an id and an effect or setup function."
```

The loader schema requires `default` to be `{ id, effect }` or `{ id, setup }`,
so the rewrite exports `{ id: "exomonad", setup }` and registers every handler
from the setup context. The same probe now loads without a warning. The
integration test `rust/exomonad-core/tests/opencode_plugin_v2.rs` asserts the
generated files and, when `EXOMONAD_OPENCODE_V2_PROBE=1` is set and the CLI is
available, runs this launch probe and checks the observed hook lines.

## Companion launch note

The task-less interactive companion command
`opencode --model ... --agent exomonad-<role>` is also rejected by v2.3.2:
the top level no longer accepts `--model` or `--agent` (they are `opencode run`
flags), so that path exits 1 with help text. Companion tasks that use
`opencode run --standalone ...` are unaffected.

Fixed in #1584. `opencode_companion_launch_form` selects one form per detected
CLI version: v2.0.22 keeps the top-level `--model`/`--agent` form, and v2.3.2
uses `opencode mini --standalone --model ... --agent exomonad-<role>`. `mini`
is the supported interactive interface in v2.3.2 and accepts the model
(including the `#variant` effort suffix), `--standalone`, and the generated
agent; a PTY launch shows `exomonad-worker · Beam Free [high]` in its status
line. `init` resolves the form before creating a session, and an unknown
version is a named error naming the known versions instead of a guessed form.
This companion form loads the plugin and delivers pre/post tool hooks, role
denies, per-turn `stop`, and the process-end exit hook; see "Interactive
`opencode mini` (verified)" above.
