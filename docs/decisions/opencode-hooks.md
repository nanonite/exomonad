# OpenCode Hook Integration

## Context

ExoMonad's hook system gives Claude Code and Gemini agents behavioral guardrails and context steering at tool call boundaries. Claude Code hooks work via `settings.local.json` + `exomonad hook <event>` shell commands. Gemini hooks work the same way via `settings.json`. Both ultimately call `exomonad hook <event> --runtime <runtime>` which reads JSON stdin, forwards to the WASM server, and returns a `HookEnvelope`.

OpenCode agents were excluded from hooks because OpenCode doesn't support the shell-command-in-JSON-config model. The `opencode.json` configuration has no `hooks` key.

## Decision

OpenCode 1.14.48 provides a TypeScript plugin API. A plugin is a local npm-style package (Bun resolves `.ts` natively, no compilation needed) referenced in `opencode.json` via a `"plugin"` array. The plugin exports a `server: Plugin` function that returns a `Hooks` object with lifecycle handlers.

Because plugin handlers receive `input.$` (Bun's shell), they can subprocess-call `exomonad hook <event> --runtime opencode` exactly like the other runtimes. This makes the TypeScript plugin a thin bridge to the existing WASM dispatch path with no new server-side logic.

## Bridge Pattern

```
OpenCode (TypeScript plugin, in-process)
  ├── tool.execute.before
  │     └── Bun $`exomonad hook pre-tool-use --runtime opencode`
  │           └── stdin: { tool_name, session_id, call_id, args }
  │                 └── UDS → exomonad serve → WASM dispatch
  │                       └── Haskell role handler → HookEnvelope { stdout, exit_code }
  │                 └── TypeScript reads stdout, merges into output.args
  │
  ├── tool.execute.after
  │     └── Bun $`exomonad hook post-tool-use --runtime opencode`
  │
  └── event (type = "session.stopped")
        └── Bun $`exomonad hook stop --runtime opencode`
```

All hook logic lives in Haskell WASM — the plugin is purely glue.

## Event Mapping

| OpenCode event | ExoMonad HookEventType | HookDispatch |
|---|---|---|
| `tool.execute.before` | `PreToolUse` | `ToolUse` |
| `tool.execute.after` | `PostToolUse` | `ToolUse` |
| `event` (type=`session.stopped`) | `Stop` | `Stop` |

## Plugin Package

The plugin is embedded as Rust compile-time constants in `rust/exomonad-core/src/opencode_plugin.rs` (`OPENCODE_PLUGIN_TS`, `OPENCODE_PLUGIN_PKG_JSON`). At spawn time, Rust writes these to `.exo/opencode-plugin/` inside the agent's working directory:

```
<agent-dir>/
├── opencode.json          { "mcp": {...}, "plugin": ["./.exo/opencode-plugin"] }
└── .exo/opencode-plugin/
    ├── index.ts            TypeScript bridge (callHook helper + hook handlers)
    └── package.json        { "name": "@exomonad/opencode-plugin", "type": "module" }
```

No external npm install is required. Bun resolves `.ts` natively and `@opencode-ai/plugin` types are bundled with OpenCode.

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

## Tested Against

OpenCode 1.14.48
