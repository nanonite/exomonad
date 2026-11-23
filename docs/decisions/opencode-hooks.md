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
  │                 └── UDS → exomonad serve → WASM dispatch
  │                       └── Haskell role handler → HookEnvelope { stdout, exit_code }
  │                 └── TypeScript reads stdout, denies or merges into input
  │
  ├── ctx.tool.hook("execute.after")
  │     └── exomonad hook post-tool-use --runtime opencode
  │
  └── ctx.event.subscribe (terminal session.execution.* event)
        └── exomonad hook stop|worker-exit --runtime opencode
```

All hook logic lives in Haskell WASM — the plugin is purely glue.

## Event Mapping

| OpenCode v2 event | ExoMonad HookEventType | HookDispatch |
|---|---|---|
| `execute.before` | `PreToolUse` | `ToolUse` |
| `execute.after` | `PostToolUse` | `ToolUse` |
| `session.execution.succeeded` | `Stop` (or `WorkerExit` when `EXOMONAD_ROLE=worker`) | `Stop` |
| `session.execution.failed` | `Stop` (or `WorkerExit`) | `Stop` |
| `session.execution.interrupted` | `Stop` (or `WorkerExit`) | `Stop` |

`session.stopped` from the v1 API no longer exists in v2. A one-shot
`opencode run` emits `session.execution.succeeded` at the end and does not emit
`session.idle`, so the terminal `session.execution.*` events are the Stop /
worker-exit trigger.

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
v1.14 plugin hooks and states that limitation here rather than claiming
compatibility. v1.14 users keep the previously released plugin; no v2 code path
sniffs the CLI version.

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
uses `opencode mini --model ... --agent exomonad-<role>`. `mini` is the
supported interactive interface in v2.3.2 and accepts the model (including the
`#variant` effort suffix) and the generated agent; a PTY launch shows
`exomonad-worker · Beam Free [high]` in its status line. `init` resolves the
form before creating a session, and an unknown version is a named error naming
the known versions instead of a guessed form.
