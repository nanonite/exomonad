# Plan: OpenCode Hook Integration

## Context

ExoMonad has a working hook system for Claude Code (11 events via `.claude/settings.local.json` + `exomonad hook <event>` CLI) and Gemini (4 events via `.gemini/settings.json`). Both delegate to the same `exomonad hook <event> --runtime <runtime>` subprocess protocol: reads JSON stdin → forwards to WASM server → returns `HookEnvelope { stdout, exit_code }`.

OpenCode agents currently get **no hooks at all** — only MCP config + instructions. The comment in `generate_opencode_tl_settings` even reads "Note: opencode does not support `hooks` or `allowedPaths` keys" because the JSON-hook model that Claude/Gemini use doesn't apply.

OpenCode 1.14.48 does support hooks, but via a **TypeScript plugin** loaded in-process. Plugins are npm modules (or local Bun packages); each plugin exports a `Plugin` async function that receives `input` (with Bun's `$` shell), and returns a `Hooks` object. Because the plugin gets `input.$` (Bun shell), it can subprocess-call `exomonad hook <event> --runtime opencode` exactly like the other runtimes — making this a thin bridge to the existing WASM dispatch path with minimal new Rust code.

---

## Plugin API (verified against OpenCode 1.14.48)

**Registration in `opencode.json`:**
```json
{
  "mcp": { ... },
  "plugin": ["./.exo/opencode-plugin"]
}
```
Local path relative to `opencode.json`. Bun resolves `.ts` files natively — no compilation step needed.

**Plugin package structure:**
```
.exo/opencode-plugin/
├── package.json   { "name": "@exomonad/opencode-plugin", "type": "module", "main": "index.ts" }
└── index.ts
```

**Available hook events (exact strings):**
- `"tool.execute.before"` → receives `{ tool, sessionID, callID }`, output `{ args }` — maps to PreToolUse
- `"tool.execute.after"` → receives `{ tool, sessionID, callID, args }`, output `{ title, output, metadata }` — maps to PostToolUse
- `event` → receives `{ event }` where `event.type` is e.g. `"session.stopped"` — maps to Stop/WorkerExit
- `"shell.env"` → receives `{ cwd, sessionID, callID }`, output `{ env }` — maps to environment injection

**No direct session-created event** — the `event` hook must filter `event.type`. Session registration happens at spawn time in ExoMonad, so no `SessionStart` equivalent is needed for OpenCode.

**Package types import:** `import type { Plugin } from "@opencode-ai/plugin"` (bundled with OpenCode, no separate install for the type import in the plugin's TypeScript)

---

## Step 1: Add `HookRuntime::OpenCode` to protocol

**File**: `rust/exomonad-core/src/protocol/mod.rs`

Add `OpenCode` to the `HookRuntime` enum alongside the existing `Claude` and `Gemini` variants:

```rust
#[serde(rename = "opencode")]
OpenCode,
```

Also extend any `Display`, `clap::ValueEnum`, or `From<&str>` impls to match the existing pattern. After this, `exomonad hook pre-tool-use --runtime opencode` parses correctly.

---

## Step 2: Add OpenCode event dispatch in serve.rs

**File**: `rust/exomonad/src/serve.rs` (around the hook dispatch block, lines 344–442)

In the existing match on `HookRuntime`, add an `OpenCode` arm that normalizes OpenCode event names to ExoMonad's internal `HookEventType`:

- `HookEventType::PreToolUse` → dispatch as `HookDispatch::ToolUse`
- `HookEventType::PostToolUse` → dispatch as `HookDispatch::ToolUse`
- `HookEventType::Stop` → dispatch as `HookDispatch::Stop`
- `HookEventType::SubagentStop` (or `WorkerExit`) → dispatch as `HookDispatch::WorkerExit`

The WASM handlers (`handle_pre_tool_use`, `handle_post_tool_use`, `handle_stop_hook`) already exist. No WASM changes needed — they fire the same Haskell logic regardless of which runtime triggered them.

**Response shape**: The TypeScript plugin interprets the `HookEnvelope.stdout` JSON. For `tool.execute.before`, a deny response (non-zero exit code) should surface as a denied tool call. Map the existing `ClaudePreToolUseOutput` structure to what OpenCode expects in the `output.args` return from the hook function.

---

## Step 3: Write the TypeScript plugin adapter (embedded in Rust)

Add a new file **`rust/exomonad-core/src/opencode_plugin.rs`** (or inline into `spawn.rs`) that holds the plugin as a `const &str`. This is embedded at compile time, written to disk during spawn — no external file to lose track of.

**`index.ts` content:**

```typescript
import type { Plugin } from "@opencode-ai/plugin";

async function callHook(
  shell: any,
  event: string,
  payload: unknown
): Promise<unknown> {
  try {
    const raw = await shell`exomonad hook ${event} --runtime opencode`
      .stdin(JSON.stringify(payload))
      .text();
    return JSON.parse(raw.trim());
  } catch {
    return { continue: true };
  }
}

export const server: Plugin = async (input) => ({
  "tool.execute.before": async ({ tool, sessionID, callID }, output) => {
    const payload = { tool_name: tool, session_id: sessionID, call_id: callID, args: output.args };
    const result = await callHook(input.$, "pre-tool-use", payload);
    if (result && typeof result === "object" && "args" in result) {
      Object.assign(output, result);
    }
  },

  "tool.execute.after": async ({ tool, sessionID, callID, args }, output) => {
    const payload = { tool_name: tool, session_id: sessionID, call_id: callID, args, output: output.output };
    await callHook(input.$, "post-tool-use", payload);
  },

  event: async ({ event }) => {
    if (event.type === "session.stopped") {
      await callHook(input.$, "stop", event);
    }
  },
});

export default { server };
```

**`package.json` content:**
```json
{
  "name": "@exomonad/opencode-plugin",
  "version": "1.0.0",
  "type": "module",
  "main": "index.ts"
}
```

Both are `const &str` in Rust, written as a pair during spawn.

---

## Step 4: Wire plugin into OpenCode spawn paths

### A. `generate_opencode_tl_settings` in `spawn.rs`

**File**: `rust/exomonad-core/src/services/agent_control/spawn.rs`, function at line 511.

Add `"plugin"` key to the returned JSON:

```rust
serde_json::json!({
    "mcp": mcp_servers,
    "instructions": instructions,
    "plugin": ["./.exo/opencode-plugin"],
})
```

Drop the `_binary_path` and `_parent_dir` parameters from their unused (`_`) prefix — the plugin path is always relative to the worktree, so no path injection needed.

### B. Write plugin files alongside opencode.json

In every place that calls `generate_opencode_tl_settings` or writes `opencode.json` for an OpenCode agent, also write the plugin package:

```rust
let plugin_dir = worktree_path.join(".exo/opencode-plugin");
fs::create_dir_all(&plugin_dir).await?;
fs::write(plugin_dir.join("index.ts"), OPENCODE_PLUGIN_TS).await?;
fs::write(plugin_dir.join("package.json"), OPENCODE_PLUGIN_PKG_JSON).await?;
```

Call sites:
1. **`spawn_subtree`** (line 876–888): OpenCode arm
2. **`spawn_worker`** (line 617–634): OpenCode arm
3. **`init.rs`** (line 314–332): root OpenCode TL

For `init.rs`, the `opencode.json` goes to the repo root (`.`), so the plugin path is `.exo/opencode-plugin` relative to CWD — which already works since `init.rs` sets up in `cwd`.

### C. Remove the misleading comment

In `generate_opencode_tl_settings`, remove or update the comment: "Note: opencode does not support `hooks` or `allowedPaths` keys" — it's outdated now.

---

## Step 5: Documentation

### `docs/decisions/opencode-hooks.md` (new ADR)

Document:
- Why TypeScript plugin vs JSON hook config (OpenCode's in-process plugin architecture)
- The bridge pattern: TypeScript `tool.execute.before` → Bun `$` subprocess → `exomonad hook pre-tool-use --runtime opencode` → UDS → WASM dispatch
- Event name mapping table (OpenCode event → ExoMonad HookEventType)
- Plugin package location (`.exo/opencode-plugin/` in each worktree, written by Rust at spawn)
- No external npm install required (Bun runs `.ts` natively; `@opencode-ai/plugin` types are bundled with OpenCode)
- OpenCode version tested: 1.14.48

### `CLAUDE.md` updates

1. In the "Built Infrastructure" table: add OpenCode hooks row
2. Remove/update any text saying OpenCode hooks are unsupported
3. Note plugin location in the OpenCode spawn section

---

## Use case: MCP context steering via pre/post hooks

This is where OpenCode hooks pay off most immediately. The existing WASM `handle_pre_tool_use` already implements role-based tool filtering for Claude Code — OpenCode workers will now get the same. But the real leverage is **context injection at MCP call boundaries**.

**`tool.execute.before` on `file_pr`:**
- Inject a reminder into `output.args` body: "Ensure your PR body describes the change from the reviewer's perspective, not the implementor's."
- Force the PR title format (e.g., prepend `[gh-{issue}]` if missing)
- Block `file_pr` entirely until a preceding `git push` succeeds (check git state in the hook)

**`tool.execute.before` on `notify_parent`:**
- Enforce that the notification includes a status field (`success`, `failure`, `stuck`)
- Rewrite terse messages to the structured `[FIXES PUSHED]` vocabulary the TL expects

**`tool.execute.after` on `merge_pr`:**
- Inject "verify the build passes before proceeding to the next wave" into the post-hook response
- Record merge events for the worktree event watcher

All of this logic lives in Haskell WASM (in the role's hook handlers), not in the TypeScript plugin. The TypeScript plugin is purely the bridge. The WASM already has the tool name and agent role available in the hook context — so tool-specific steering based on role (`dev` vs `tl`) is already supported by the existing dispatch path.

---

## Critical files

| File | Change |
|------|--------|
| `rust/exomonad-core/src/protocol/mod.rs` | Add `HookRuntime::OpenCode` |
| `rust/exomonad/src/serve.rs` | OpenCode event dispatch + normalization |
| `rust/exomonad-core/src/services/agent_control/spawn.rs` | Add `OPENCODE_PLUGIN_TS`/`OPENCODE_PLUGIN_PKG_JSON` consts, write files, add `"plugin"` to opencode.json |
| `rust/exomonad/src/init.rs` | Write plugin files for root OpenCode TL |
| `docs/decisions/opencode-hooks.md` | New ADR |
| `CLAUDE.md` | Update capabilities table |

---

## Verification

```bash
# 1. Build
cargo build -p exomonad -p exomonad-core

# 2. Confirm new --runtime opencode parses
exomonad hook pre-tool-use --runtime opencode <<< '{}'

# 3. Start test session with OpenCode as root TL
exomonad init --opencode-as-tl

# 4. Verify plugin files are written to correct locations
ls .exo/opencode-plugin/      # index.ts + package.json
cat opencode.json              # should contain "plugin": ["./.exo/opencode-plugin"]

# 5. In OpenCode session — trigger any tool call and check server logs
# Server window should show hook events from opencode runtime

# 6. Rust tests
cargo test --workspace
```

---

## Ordering

Steps 1–4 are independent of each other and can be done in sequence by a single implementor. Step 5 (docs) last. Total scope: ~200 lines of Rust + ~50 lines TypeScript.
