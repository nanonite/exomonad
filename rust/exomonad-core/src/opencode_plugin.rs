//! OpenCode TypeScript plugin adapter.
//!
//! Provides the plugin package contents as compile-time constants. At spawn time,
//! Rust writes these files to `.exo/opencode-plugin/` inside each agent's working
//! directory, then references the package in `opencode.json` via `"plugin": [...]`.
//!
//! ## Bridge Pattern
//!
//! ```text
//! OpenCode (TypeScript plugin, in-process)
//!   └── tool.execute.before / tool.execute.after / event
//!         └── Bun $ shell → exomonad hook <event> --runtime opencode (subprocess)
//!               └── UDS → exomonad serve → WASM dispatch
//!                     └── Haskell role handler → HookEnvelope { stdout, exit_code }
//!               └── TypeScript reads stdout, applies allow/deny/modify
//! ```
//!
//! No external npm install is required. Bun resolves `.ts` files natively and
//! `@opencode-ai/plugin` types are bundled with OpenCode itself.

/// The `index.ts` entry point for the exomonad OpenCode plugin.
pub const OPENCODE_PLUGIN_TS: &str = r#"import type { Plugin } from "@opencode-ai/plugin";

async function callHook(
  shell: any,
  event: string,
  payload: unknown,
): Promise<unknown> {
  try {
    const result =
      await shell`exomonad hook ${event} --runtime opencode`.stdin(
        JSON.stringify(payload),
      );
    const raw = await result.text();
    return JSON.parse(raw.trim());
  } catch {
    return { continue: true };
  }
}

export const server: Plugin = async (input) => ({
  "tool.execute.before": async ({ tool, sessionID, callID }, output) => {
    const payload = {
      tool_name: tool,
      session_id: sessionID,
      call_id: callID,
      args: output.args,
    };
    const result = await callHook(input.$, "pre-tool-use", payload);
    if (result && typeof result === "object" && "args" in result) {
      Object.assign(output, result);
    }
  },

  "tool.execute.after": async ({ tool, sessionID, callID, args }, output) => {
    const payload = {
      tool_name: tool,
      session_id: sessionID,
      call_id: callID,
      args,
      output: output.output,
    };
    await callHook(input.$, "post-tool-use", payload);
  },

  event: async ({ event }) => {
    if (event.type === "session.stopped") {
      await callHook(input.$, "stop", event);
    }
  },
});

export default { server };
"#;

/// The `package.json` for the exomonad OpenCode plugin package.
pub const OPENCODE_PLUGIN_PKG_JSON: &str = r#"{
  "name": "@exomonad/opencode-plugin",
  "version": "1.0.0",
  "type": "module",
  "main": "index.ts"
}
"#;
