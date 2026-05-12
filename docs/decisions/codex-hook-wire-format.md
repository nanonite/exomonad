# Codex Hook Wire Format

Status: accepted

Date: 2026-05-12

Chainlink: #142

## Context

ExoMonad will bridge Codex hooks through `exomonad hook <event> --runtime codex`.
The Codex side is command based: each configured hook command is executed with one
JSON object written to stdin, and Codex interprets stdout, stderr, and the process
exit code.

Confirmed against local Codex sources in `/home/goya/agent-workspace/codex`:

- `codex-rs/hooks/src/events/pre_tool_use.rs`
- `codex-rs/hooks/src/events/post_tool_use.rs`
- `codex-rs/hooks/src/events/stop.rs`
- `codex-rs/hooks/src/schema.rs`
- `codex-rs/hooks/src/engine/output_parser.rs`
- `codex-rs/hooks/src/engine/command_runner.rs`
- `codex-rs/hooks/schema/generated/*.command.*.schema.json`

## Command Execution

Codex starts the hook command in the event cwd, writes the serialized input JSON to
stdin, captures stdout and stderr, and waits for the process to finish. On Unix, if
no custom shell is configured, the command is run through `$SHELL -lc <command>`.

Hook commands are synchronous. Multiple matching handlers for the same event run
concurrently; reports are sorted back into configured order. For `PreToolUse`, if
multiple handlers rewrite input, the rewrite from the handler that finished last is
used.

## Shared Input Fields

These fields are present on the events covered here unless noted otherwise.

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | Codex thread/session id. |
| `turn_id` | string | Codex turn id. |
| `transcript_path` | string or null | Transcript path when available. |
| `cwd` | string | Working directory used to run the hook. |
| `hook_event_name` | string | One of `PreToolUse`, `PostToolUse`, `Stop`. |
| `model` | string | Active model name. |
| `permission_mode` | string | One of `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`. |

## Shared Output Fields

All three outputs use camelCase and reject unknown fields.

| Field | Type | Default | Notes |
|---|---|---|---|
| `continue` | boolean | `true` | `false` means stop for `PostToolUse` and `Stop`; unsupported for `PreToolUse`. |
| `stopReason` | string or null | null | Used with `continue:false`. Unsupported for `PreToolUse`. |
| `suppressOutput` | boolean | `false` | Unsupported for `PreToolUse` and `PostToolUse`; parsed for `Stop` but not used for continuation blocking. |
| `systemMessage` | string or null | null | Recorded as a hook warning entry. |

Empty stdout is a no-op for `PreToolUse`, `PostToolUse`, and `Stop`.

## PreToolUse

Stdin schema:

| Field | Type | Notes |
|---|---|---|
| shared fields | | See above. |
| `tool_name` | string | Canonical tool name, not matcher alias. |
| `tool_input` | any JSON value | Tool input. Shell-like tools use `{ "command": ... }`; MCP tools use resolved JSON args. |
| `tool_use_id` | string | Tool call id. |

Allow with no changes:

```json
{}
```

Block using current legacy-compatible output:

```json
{
  "decision": "block",
  "reason": "Do not run destructive commands"
}
```

Block using hook-specific output:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Do not run destructive commands"
  }
}
```

Rewrite input:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {
      "command": "cargo test --workspace"
    }
  }
}
```

Add model context:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "Prefer the justfile target for this repository."
  }
}
```

Important constraints:

- `decision:"approve"` is explicitly unsupported.
- `decision:"block"` requires a non-empty `reason`.
- `permissionDecision:"allow"` is only accepted when paired with `updatedInput`.
- `updatedInput` without `permissionDecision:"allow"` fails closed.
- `permissionDecision:"ask"` is unsupported.
- `permissionDecision:"deny"` requires non-empty `permissionDecisionReason`.
- `continue:false`, `stopReason`, and `suppressOutput:true` are unsupported.

Exit code semantics:

| Exit code | Meaning |
|---|---|
| `0` | Parse stdout. Empty stdout is allow/no-op. Blocking or rewriting is determined by JSON stdout. |
| `2` | Block tool execution if stderr has non-empty text; stderr becomes the block reason. Empty stderr is a hook failure. |
| other non-zero | Hook failure, not an allow/deny decision. |
| no status or spawn/timeout error | Hook failure. |

## PostToolUse

Stdin schema:

| Field | Type | Notes |
|---|---|---|
| shared fields | | See above. |
| `tool_name` | string | Canonical tool name. |
| `tool_input` | any JSON value | Original tool input. |
| `tool_response` | any JSON value | Tool result. |
| `tool_use_id` | string | Tool call id. |

No-op:

```json
{}
```

Block normal processing and feed feedback to the model:

```json
{
  "decision": "block",
  "reason": "The command failed; inspect stderr before continuing."
}
```

Stop the turn:

```json
{
  "continue": false,
  "stopReason": "Policy violation after tool execution"
}
```

Add model context:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "The previous build output indicates a missing generated file."
  }
}
```

Important constraints:

- `decision:"block"` requires a non-empty `reason`.
- A `reason` without `decision:"block"` fails when `continue` remains true.
- `hookSpecificOutput.updatedMCPToolOutput` exists in the schema but is unsupported.
- `suppressOutput:true` is unsupported.

Exit code semantics:

| Exit code | Meaning |
|---|---|
| `0` | Parse stdout. Empty stdout is no-op. |
| `2` | If stderr has non-empty text, it is feedback to the model. It does not mark the event blocked or stopped. Empty stderr is a hook failure. |
| other non-zero | Hook failure. |
| no status or spawn/timeout error | Hook failure. |

## Stop

Stdin schema:

| Field | Type | Notes |
|---|---|---|
| shared fields | | See above. |
| `stop_hook_active` | boolean | Whether a stop hook is already active. |
| `last_assistant_message` | string or null | Last assistant message when available. |

Allow stop:

```json
{}
```

Block stop and continue the conversation:

```json
{
  "decision": "block",
  "reason": "Run tests before stopping."
}
```

Force stop with a reason:

```json
{
  "continue": false,
  "stopReason": "Task is complete."
}
```

Important constraints:

- `decision:"block"` requires a non-empty `reason`.
- If `continue:false` is present, it overrides `decision:"block"`.
- Invalid JSON on stdout is a hook failure.

Exit code semantics:

| Exit code | Meaning |
|---|---|
| `0` | Parse stdout. Empty stdout is allow stop/no-op. |
| `2` | Block stop if stderr has non-empty text; stderr becomes the continuation prompt. Empty stderr is a hook failure. |
| other non-zero | Hook failure. |
| no status or spawn/timeout error | Hook failure. |

## ExoMonad Mapping Notes

For `PreToolUse`, ExoMonad can map Claude-style allow/deny output to Codex's
hook-specific form. Deny should use `permissionDecision:"deny"` with
`permissionDecisionReason`; rewrites must use `permissionDecision:"allow"` with
`updatedInput`.

For `PostToolUse`, ExoMonad should treat `decision:"block"` as feedback that
interrupts normal processing, and `continue:false` as stop-the-turn behavior.

For `Stop`, ExoMonad should use `decision:"block"` plus `reason` to keep Codex
running, or `{}` to allow Codex to stop. The legacy exit-code-2 behavior is
available, but JSON stdout is easier to test and should be the primary bridge.
