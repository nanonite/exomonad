//! OpenCode TypeScript plugin adapter.
//!
//! Provides the plugin package contents as compile-time constants. At spawn time,
//! Rust writes these files to `.exo/opencode-plugin/` inside each agent's working
//! directory, then references the package in `opencode.json` via `"plugin": [...]`.
//!
//! ## Bridge Pattern
//!
//! ```text
//! OpenCode v2 (TypeScript plugin, in-process Bun)
//!   └── tool execute.before / execute.after / terminal execution event
//!         └── node:child_process → exomonad hook <event> --runtime opencode
//!               └── UDS → exomonad serve → WASM dispatch
//!                     └── Haskell role handler → HookEnvelope { stdout, exit_code }
//!               └── TypeScript reads stdout, applies allow/deny/modify
//! ```
//!
//! No external npm install is required. Bun resolves `.ts` files natively.
//!
//! ## Plugin API version
//!
//! The generated module targets the OpenCode v2 plugin API: the default export
//! is a definition object `{ id, setup }`, and `setup(ctx)` registers hooks on
//! the host context (`ctx.tool.hook("execute.before", ...)`,
//! `ctx.tool.hook("execute.after", ...)`, `ctx.event.subscribe(...)`). This is
//! the shape used by the bundled `context-mode` v2 plugin.
//!
//! OpenCode v1.14 exposed a different API (`export const server` returning a
//! hooks object). That API is not emitted any more: the v2 loader rejects a
//! module without a default `{ id, setup }` definition, and v1.14.48 is not
//! available to verify a dual-shape module. v1.14 plugin hooks are therefore
//! dropped; see `docs/decisions/opencode-hooks.md`.
//!
//! ## Response shape from WASM (Claude format, used for OpenCode)
//!
//! ```json
//! {
//!   "continue": true,
//!   "hookSpecificOutput": {
//!     "hookEventName": "PreToolUse",
//!     "permissionDecision": "allow" | "deny",
//!     "permissionDecisionReason": "...",
//!     "updatedInput": { ... }
//!   }
//! }
//! ```
//!
//! ## Deny mechanism
//!
//! OpenCode's `execute.before` hook has no built-in deny signal — only input
//! mutation is supported by the API. Denial is implemented by throwing an
//! error, which OpenCode surfaces as a tool execution failure to the agent.
//! `exomonad serve` is unaffected: it already returned the `HookEnvelope`
//! response before the throw occurs.
//!
//! ## Execution sequence (deny path)
//!
//! ```text
//! OpenCode (Bun, in-process TypeScript plugin)
//!   → execute.before fires
//!   → node:child_process → exomonad hook pre-tool-use --runtime opencode
//!       → CLI normalizes the OpenCode payload (tool ids, args, event name)
//!       → subprocess makes one HTTP request to exomonad serve via UDS
//!       → exomonad serve returns HookEnvelope {
//!           stdout: '{"continue":false,...}', exit_code: 2 }
//!       → subprocess prints stdout and exits 2
//!   ← plugin parses stdout even though execFile reports exit 2
//!   → plugin sees permissionDecision = "deny"
//!   → plugin throws new Error("reason")   ← inside Bun/OpenCode only
//!   → OpenCode catches the throw, blocks that tool call
//!   → OpenCode continues running normally
//! ```
pub const OPENCODE_PLUGIN_TS: &str = r#"import { execFile } from "node:child_process";

type HookResponse = {
  continue?: boolean;
  stopReason?: string;
  hookSpecificOutput?: {
    hookEventName?: string;
    permissionDecision?: "allow" | "deny";
    permissionDecisionReason?: string;
    updatedInput?: Record<string, unknown>;
  };
};

function callHook(event: string, payload: unknown): Promise<HookResponse> {
  return new Promise((resolve) => {
    const child = execFile(
      "exomonad",
      ["hook", event, "--runtime", "opencode"],
      { timeout: 30000, maxBuffer: 8 * 1024 * 1024 },
      (_error, stdout) => {
        // A deny exits 2, so stdout must be parsed even when execFile reports
        // an error. Ignoring it would turn every enforced deny into an allow.
        const text = typeof stdout === "string" ? stdout.trim() : "";
        if (text) {
          try {
            const parsed = JSON.parse(text);
            if (parsed && typeof parsed === "object") {
              resolve(parsed);
              return;
            }
          } catch {
            // Fall through to the fail-open response below.
          }
        }
        resolve({ continue: true });
      },
    );
    child.stdin?.end(JSON.stringify(payload));
  });
}

function applyPreToolUse(
  response: HookResponse,
  input: Record<string, unknown>,
): void {
  // Hard block: continue=false means the session should not proceed at all.
  if (response.continue === false) {
    const reason =
      typeof response.stopReason === "string"
        ? response.stopReason
        : "Tool blocked by ExoMonad hook";
    throw new Error(reason);
  }

  const specific = response.hookSpecificOutput;
  if (!specific || specific.hookEventName !== "PreToolUse") return;

  if (specific.permissionDecision === "deny") {
    const reason =
      typeof specific.permissionDecisionReason === "string"
        ? specific.permissionDecisionReason
        : "Tool denied by ExoMonad hook";
    throw new Error(reason);
  }

  if (
    specific.permissionDecision === "allow" &&
    specific.updatedInput != null &&
    typeof specific.updatedInput === "object"
  ) {
    Object.assign(input, specific.updatedInput);
  }
}

async function setup(ctx: any) {
  await ctx.tool.hook("execute.before", async (input: any) => {
    const response = await callHook("pre-tool-use", {
      tool_name: input.tool,
      session_id: input.sessionID,
      call_id: input.id,
      args: input.input,
    });
    applyPreToolUse(response, input.input);
  });

  await ctx.tool.hook("execute.after", async (input: any) => {
    const output =
      input.status === "error"
        ? (input.error?.message ?? "")
        : (input.result?.output ?? "");
    await callHook("post-tool-use", {
      tool_name: input.tool,
      session_id: input.sessionID,
      call_id: input.id,
      args: input.input,
      output,
    });
  });

  // v2 dropped the v1 "session.stopped" event. A one-shot `opencode run`
  // emits the terminal session.execution.* event instead (verified against
  // v2.3.2), so these are the Stop / worker-exit trigger.
  const stop = new AbortController();
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({
        types: [
          "session.execution.succeeded",
          "session.execution.failed",
          "session.execution.interrupted",
        ],
        signal: stop.signal,
      })) {
        const hook =
          process.env.EXOMONAD_ROLE === "worker" ? "worker-exit" : "stop";
        await callHook(hook, event);
      }
    } catch {
      // Subscription cancelled or host event stream failed; nothing to do.
    }
  })();

  return () => stop.abort();
}

export default { id: "exomonad", setup };
export { setup };
"#;

/// The `package.json` for the exomonad OpenCode plugin package.
pub const OPENCODE_PLUGIN_PKG_JSON: &str = r#"{
  "name": "@exomonad/opencode-plugin",
  "version": "2.0.0",
  "type": "module",
  "main": "index.ts"
}
"#;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opencode_plugin_exports_v2_definition() {
        assert!(OPENCODE_PLUGIN_TS.contains("export default { id: \"exomonad\", setup };"));
        assert!(OPENCODE_PLUGIN_TS.contains("async function setup(ctx: any)"));
        assert!(!OPENCODE_PLUGIN_TS.contains("export const server"));
        assert!(!OPENCODE_PLUGIN_TS.contains("export default { server }"));
    }

    #[test]
    fn opencode_plugin_registers_tool_and_execution_hooks() {
        assert!(OPENCODE_PLUGIN_TS.contains("ctx.tool.hook(\"execute.before\""));
        assert!(OPENCODE_PLUGIN_TS.contains("ctx.tool.hook(\"execute.after\""));
        assert!(OPENCODE_PLUGIN_TS.contains("ctx.event.subscribe("));
        assert!(OPENCODE_PLUGIN_TS.contains("\"session.execution.succeeded\""));
        assert!(OPENCODE_PLUGIN_TS.contains("\"session.execution.failed\""));
        assert!(OPENCODE_PLUGIN_TS.contains("\"session.execution.interrupted\""));
    }

    #[test]
    fn opencode_plugin_shells_out_to_hook_command() {
        assert!(OPENCODE_PLUGIN_TS.contains("\"hook\", event, \"--runtime\", \"opencode\""));
    }

    #[test]
    fn opencode_plugin_parses_deny_stdout_despite_nonzero_exit() {
        assert!(OPENCODE_PLUGIN_TS.contains("(_error, stdout)"));
        assert!(OPENCODE_PLUGIN_TS.contains("typeof stdout === \"string\""));
        assert!(!OPENCODE_PLUGIN_TS.contains("if (error) {"));
    }

    #[test]
    fn opencode_plugin_preserves_deny_and_argument_mutation() {
        assert!(OPENCODE_PLUGIN_TS.contains("permissionDecision === \"deny\""));
        assert!(OPENCODE_PLUGIN_TS.contains("updatedInput"));
        assert!(OPENCODE_PLUGIN_TS.contains("Object.assign(input, specific.updatedInput)"));
    }

    #[test]
    fn opencode_worker_stop_routes_to_worker_exit_hook() {
        assert!(OPENCODE_PLUGIN_TS.contains("EXOMONAD_ROLE === \"worker\""));
        assert!(OPENCODE_PLUGIN_TS.contains("? \"worker-exit\" : \"stop\""));
    }
}
