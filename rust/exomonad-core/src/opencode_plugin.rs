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
///
/// ## Response shape from WASM (Claude format, used for OpenCode)
///
/// ```json
/// {
///   "continue": true,
///   "hookSpecificOutput": {
///     "hookEventName": "PreToolUse",
///     "permissionDecision": "allow" | "deny",
///     "permissionDecisionReason": "...",
///     "updatedInput": { ... }
///   }
/// }
/// ```
///
/// ## Deny mechanism
///
/// OpenCode's `tool.execute.before` hook has no built-in deny signal — only
/// `output.args` mutation is supported by the API. Denial is implemented by
/// throwing an error, which OpenCode surfaces as a tool execution failure to
/// the agent. `exomonad serve` is unaffected: it already returned the
/// `HookEnvelope` response before the throw occurs.
///
/// ## Execution sequence (deny path)
///
/// ```text
/// OpenCode (Bun, in-process TypeScript plugin)
///   → tool.execute.before fires
///   → Bun $`exomonad hook pre-tool-use --runtime opencode` (subprocess)
///       → subprocess makes one HTTP request to exomonad serve via UDS
///       → exomonad serve returns HookEnvelope { stdout: '{"continue":true,...}', exit_code: 0 }
///       → subprocess exits 0
///   ← Bun reads subprocess stdout
///   → plugin parses JSON, sees permissionDecision = "deny"
///   → plugin throws new Error("reason")   ← inside Bun/OpenCode only
///   → OpenCode catches the throw, blocks that tool call
///   → OpenCode continues running normally
/// ```
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
    if (!result || typeof result !== "object") return;

    const r = result as Record<string, unknown>;

    // Hard block: continue=false means the session should not proceed at all.
    if (r["continue"] === false) {
      const reason =
        typeof r["stopReason"] === "string"
          ? r["stopReason"]
          : "Tool blocked by ExoMonad hook";
      throw new Error(reason);
    }

    const specific = r["hookSpecificOutput"];
    if (!specific || typeof specific !== "object") return;
    const s = specific as Record<string, unknown>;
    if (s["hookEventName"] !== "PreToolUse") return;

    if (s["permissionDecision"] === "deny") {
      const reason =
        typeof s["permissionDecisionReason"] === "string"
          ? s["permissionDecisionReason"]
          : "Tool denied by ExoMonad hook";
      throw new Error(reason);
    }

    if (
      s["permissionDecision"] === "allow" &&
      s["updatedInput"] != null &&
      typeof s["updatedInput"] === "object"
    ) {
      Object.assign(output.args, s["updatedInput"]);
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
      const hook = process.env.EXOMONAD_ROLE === "worker" ? "worker-exit" : "stop";
      await callHook(input.$, hook, event);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opencode_worker_stop_routes_to_worker_exit_hook() {
        assert!(OPENCODE_PLUGIN_TS.contains("EXOMONAD_ROLE === \"worker\""));
        assert!(OPENCODE_PLUGIN_TS.contains("? \"worker-exit\" : \"stop\""));
    }
}
