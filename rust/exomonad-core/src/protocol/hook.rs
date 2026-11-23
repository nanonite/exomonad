//! Hook event types and builders.
//!
//! Types for Claude Code hook stdin/stdout communication.

use crate::domain::{PermissionMode, SessionId, ToolName, ToolPermission};
use crate::protocol::{HookEventType, Runtime};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

// ============================================================================
// Hook Event Types (from Claude Code stdin)
// ============================================================================

/// Hook event payload received from Claude Code via stdin.
///
/// This matches the JSON schema that Claude Code sends to hook commands.
/// Fields vary by hook type - we capture them all and let the Haskell
/// handler pick what it needs.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HookInput {
    /// Session ID from Claude Code.
    pub session_id: SessionId,

    /// Path to the transcript file.
    #[serde(default)]
    pub transcript_path: String,

    /// Current working directory.
    #[serde(default)]
    pub cwd: String,

    /// Permission mode.
    #[serde(default)]
    pub permission_mode: PermissionMode,

    /// The hook event name (PreToolUse, PostToolUse, etc.).
    pub hook_event_name: String,

    /// Runtime environment. Injected by Rust before WASM call.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime: Option<Runtime>,

    // ----- Tool-related fields (PreToolUse, PostToolUse, PermissionRequest) -----
    /// Tool name for tool-related hooks.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<ToolName>,

    /// Tool input arguments for tool-related hooks.
    #[serde(alias = "tool_parameters")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_input: Option<Value>,

    /// Tool use ID for tool-related hooks.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_use_id: Option<String>,

    /// Tool response (PostToolUse only).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_response: Option<Value>,

    // ----- Other hook-specific fields -----
    /// User prompt text (UserPromptSubmit).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt: Option<String>,

    /// Notification message (Notification).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,

    /// Notification type (Notification).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notification_type: Option<String>,

    /// Whether stop hook is active (Stop, SubagentStop).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop_hook_active: Option<bool>,

    /// Compact trigger (PreCompact).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trigger: Option<String>,

    /// Custom instructions (PreCompact).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub custom_instructions: Option<String>,

    /// Session start source (SessionStart).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,

    /// Session end reason (SessionEnd).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,

    /// Agent's response text (AfterAgent).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt_response: Option<String>,

    /// Event timestamp.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,

    /// LLM request payload (BeforeModel).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub llm_request: Option<Value>,

    /// LLM response chunk (AfterModel).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub llm_response: Option<Value>,
}

// ============================================================================
// Hook Output Types (to Claude Code stdout)
// ============================================================================

/// Common fields for all hook output types.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ClaudePreToolUseOutput {
    /// Whether to continue processing (default: true).
    #[serde(rename = "continue", default = "default_true")]
    pub continue_: bool,

    /// Reason for stopping (when continue_ = false).
    #[serde(skip_serializing_if = "Option::is_none", rename = "stopReason")]
    pub stop_reason: Option<String>,

    /// Whether to suppress output in Claude Code UI.
    #[serde(
        skip_serializing_if = "Option::is_none",
        rename = "suppressOutput",
        default
    )]
    pub suppress_output: Option<bool>,

    /// System message to show to user.
    #[serde(skip_serializing_if = "Option::is_none", rename = "systemMessage")]
    pub system_message: Option<String>,

    /// Hook-specific output fields.
    #[serde(skip_serializing_if = "Option::is_none", rename = "hookSpecificOutput")]
    pub hook_specific_output: Option<HookSpecificOutput>,
}

impl Default for ClaudePreToolUseOutput {
    fn default() -> Self {
        Self {
            continue_: true, // Semantic default: allow continuation
            stop_reason: None,
            suppress_output: None,
            system_message: None,
            hook_specific_output: None,
        }
    }
}

fn default_true() -> bool {
    true
}

/// Hook-specific output fields.
///
/// The inner structure varies by hook type.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "hookEventName")]
pub enum HookSpecificOutput {
    /// PreToolUse hook output.
    PreToolUse {
        /// Permission decision: "allow", "deny", or "ask".
        #[serde(rename = "permissionDecision")]
        permission_decision: ToolPermission,

        /// Reason for the decision.
        #[serde(
            skip_serializing_if = "Option::is_none",
            rename = "permissionDecisionReason"
        )]
        permission_decision_reason: Option<String>,

        /// Modified tool input (only with "allow" decision).
        #[serde(skip_serializing_if = "Option::is_none", rename = "updatedInput")]
        updated_input: Option<Value>,
    },

    /// PostToolUse hook output.
    PostToolUse {
        /// Additional context for Claude about the tool result.
        #[serde(skip_serializing_if = "Option::is_none", rename = "additionalContext")]
        additional_context: Option<String>,
    },

    /// UserPromptSubmit hook output.
    UserPromptSubmit {
        /// Additional context added to conversation.
        #[serde(skip_serializing_if = "Option::is_none", rename = "additionalContext")]
        additional_context: Option<String>,
    },

    /// SessionStart hook output.
    SessionStart {
        /// Context loaded at session start.
        #[serde(skip_serializing_if = "Option::is_none", rename = "additionalContext")]
        additional_context: Option<String>,
    },

    /// PermissionRequest hook output.
    PermissionRequest {
        /// Permission decision.
        decision: PermissionDecision,
    },

    /// Stop hook output.
    Stop {
        /// Decision: "block" to prevent stopping, or None to allow.
        #[serde(skip_serializing_if = "Option::is_none")]
        decision: Option<String>,

        /// Claude-facing guidance when blocked.
        #[serde(skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    },

    /// SubagentStop hook output.
    SubagentStop {
        /// Decision: "block" to prevent stopping, or None to allow.
        #[serde(skip_serializing_if = "Option::is_none")]
        decision: Option<String>,

        /// Claude-facing guidance when blocked.
        #[serde(skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    },

    /// Notification hook output.
    Notification,

    /// PreCompact hook output.
    PreCompact,

    /// SessionEnd hook output.
    SessionEnd,
}

/// Permission decision for PermissionRequest hook.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "behavior")]
pub enum PermissionDecision {
    /// Allow the tool call.
    #[serde(rename = "allow")]
    Allow {
        /// Modified tool input.
        #[serde(skip_serializing_if = "Option::is_none", rename = "updatedInput")]
        updated_input: Option<Value>,
    },

    /// Deny the tool call.
    #[serde(rename = "deny")]
    Deny {
        /// Message explaining denial.
        message: String,

        /// Whether to interrupt the current turn.
        #[serde(default)]
        interrupt: bool,
    },
}

// ============================================================================
// Builder helpers
// ============================================================================

impl ClaudePreToolUseOutput {
    /// Create an "allow" response for PreToolUse.
    pub fn pre_tool_use_allow(reason: Option<String>, modified_input: Option<Value>) -> Self {
        Self {
            continue_: true,
            hook_specific_output: Some(HookSpecificOutput::PreToolUse {
                permission_decision: ToolPermission::Allow,
                permission_decision_reason: reason,
                updated_input: modified_input,
            }),
            ..Default::default()
        }
    }

    /// Create a "deny" response for PreToolUse.
    pub fn pre_tool_use_deny(reason: String) -> Self {
        Self {
            continue_: true,
            hook_specific_output: Some(HookSpecificOutput::PreToolUse {
                permission_decision: ToolPermission::Deny,
                permission_decision_reason: Some(reason),
                updated_input: None,
            }),
            ..Default::default()
        }
    }

    /// Create an "allow" response for PostToolUse with optional context.
    pub fn post_tool_use_allow(additional_context: Option<String>) -> Self {
        Self {
            continue_: true,
            hook_specific_output: Some(HookSpecificOutput::PostToolUse { additional_context }),
            ..Default::default()
        }
    }

    /// Create a "block" response that stops processing.
    pub fn block(reason: String) -> Self {
        Self {
            continue_: false,
            stop_reason: Some(reason),
            ..Default::default()
        }
    }
}

// ============================================================================
// Internal Domain Types (from WASM, translated at edge)
// ============================================================================

/// Stop hook decision from WASM (domain type).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StopDecision {
    /// Allow the agent to stop
    Allow,
    /// Block stopping, send reason as correction prompt
    Block,
}

/// Internal stop hook output from WASM.
/// This is the domain type that Haskell returns. Rust translates to runtime-specific format.
#[derive(Debug, Clone, Deserialize)]
pub struct InternalStopHookOutput {
    /// Decision: allow or block
    pub decision: StopDecision,
    /// Reason for blocking (sent to agent as correction prompt)
    pub reason: Option<String>,
}

impl InternalStopHookOutput {
    /// Translate to Claude Code format.
    pub fn to_claude(&self) -> ClaudeStopHookOutput {
        match self.decision {
            StopDecision::Allow => ClaudeStopHookOutput {
                continue_: true,
                stop_reason: None,
            },
            StopDecision::Block => ClaudeStopHookOutput {
                continue_: false,
                stop_reason: self.reason.clone(),
            },
        }
    }

    /// Translate to runtime-specific format and serialize to JSON.
    pub fn to_runtime_json(&self, runtime: &Runtime) -> String {
        match runtime {
            Runtime::Claude => serde_json::to_string(&self.to_claude())
                .unwrap_or_else(|_| r#"{"continue":true}"#.to_string()),
            Runtime::OpenCode => serde_json::to_string(&self.to_claude())
                .unwrap_or_else(|_| r#"{"continue":true}"#.to_string()),
            Runtime::Codex => serde_json::to_string(&self.to_claude())
                .unwrap_or_else(|_| r#"{"continue":true}"#.to_string()),
        }
    }
}

/// Claude Code stop hook output format.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ClaudeStopHookOutput {
    /// Whether to continue (true = allow stop, false = block)
    #[serde(rename = "continue")]
    pub continue_: bool,
    /// Reason for blocking
    #[serde(skip_serializing_if = "Option::is_none", rename = "stopReason")]
    pub stop_reason: Option<String>,
}

// ============================================================================
// Hook Envelope (server ↔ CLI wire type)
// ============================================================================

/// Wire type for the `/hook` HTTP endpoint.
/// Server returns this; CLI prints `stdout` and exits with `exit_code`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HookEnvelope {
    /// The JSON string to print to stdout (consumed by the active CLI).
    pub stdout: String,
    /// Process exit code (0 = success, 2 = block).
    pub exit_code: i32,
}

// ============================================================================
// Codex Hook Adapter
// ============================================================================

/// Normalize Codex hook stdin into ExoMonad's internal `HookInput` shape.
pub fn normalize_codex_hook_payload(event_type: HookEventType, input: Value) -> Value {
    let mut object = match input {
        Value::Object(object) => object,
        _ => Map::new(),
    };

    insert_default_string(&mut object, "session_id", "codex");
    insert_default_string(&mut object, "cwd", ".");
    insert_default_string(&mut object, "permission_mode", "default");
    normalize_optional_string(&mut object, "transcript_path");

    if !has_non_empty_string(&object, "hook_event_name") {
        object.insert(
            "hook_event_name".to_string(),
            Value::String(codex_hook_event_name(event_type).to_string()),
        );
    }

    if !object.contains_key("tool_name") {
        if let Some(tool) = object.remove("tool") {
            object.insert("tool_name".to_string(), tool);
        }
    }

    if !object.contains_key("tool_input") {
        if let Some(args) = object.remove("args") {
            object.insert("tool_input".to_string(), args);
        }
    }

    Value::Object(object)
}

/// Format a server HookEnvelope as Codex hook stdout/exit semantics.
pub fn format_codex_hook_response(
    event_type: HookEventType,
    envelope: HookEnvelope,
) -> HookEnvelope {
    if envelope.stdout.trim().is_empty() {
        return codex_noop_envelope();
    }

    match event_type {
        HookEventType::PreToolUse => format_codex_pre_tool_use(envelope),
        HookEventType::PostToolUse => format_codex_post_tool_use(envelope),
        HookEventType::Stop => format_codex_stop(envelope),
        _ => envelope,
    }
}

/// Empty Codex stdout is also a no-op, but `{}` is easier to smoke-test.
pub fn codex_noop_envelope() -> HookEnvelope {
    HookEnvelope {
        stdout: "{}".to_string(),
        exit_code: 0,
    }
}

fn codex_hook_event_name(event_type: HookEventType) -> &'static str {
    match event_type {
        HookEventType::PreToolUse => "PreToolUse",
        HookEventType::PostToolUse => "PostToolUse",
        HookEventType::Stop => "Stop",
        HookEventType::SubagentStop => "Stop",
        HookEventType::SessionEnd => "Stop",
        HookEventType::SessionStart => "SessionStart",
        _ => "PreToolUse",
    }
}

// ============================================================================
// OpenCode Hook Adapter
// ============================================================================

/// Normalize an OpenCode v2 plugin payload into ExoMonad's internal `HookInput`
/// shape.
///
/// The v2 plugin sends the provider's own shape:
///
/// ```json
/// {"tool_name":"shell","session_id":"ses_...","call_id":"call-...","args":{"command":"..."}}
/// ```
///
/// Two things make that unparseable and unsteerable on its own:
///
/// * `hook_event_name` is only known from the `hook` command, not the payload.
/// * provider tool ids (`shell`, `edit`, `patch`, `exomonad_approve_pr`, …) are
///   not the canonical names the role hooks match.
///
/// This is the single OpenCode mapping layer. An unrecognised non-MCP tool id
/// would skip every role guardrail, so PreToolUse fails closed through
/// [`opencode_unknown_tool_deny`]. PostToolUse and Stop only log and observe
/// the same id, because there is nothing left to block after the call.
pub fn normalize_opencode_hook_payload(event_type: HookEventType, input: Value) -> Value {
    let mut object = match input {
        Value::Object(object) => object,
        _ => Map::new(),
    };

    if !has_non_empty_string(&object, "session_id") {
        let session_id = opencode_event_session_id(&object).unwrap_or_else(|| "opencode".into());
        object.insert("session_id".to_string(), Value::String(session_id));
    }
    insert_default_string(&mut object, "cwd", ".");
    insert_default_string(&mut object, "permission_mode", "default");
    normalize_optional_string(&mut object, "transcript_path");

    // OpenCode identifies the event on the command line, never in the payload.
    object.insert(
        "hook_event_name".to_string(),
        Value::String(opencode_hook_event_name(event_type).to_string()),
    );

    if !object.contains_key("tool_name") {
        if let Some(tool) = object.remove("tool") {
            object.insert("tool_name".to_string(), tool);
        }
    }

    if let Some(Value::String(tool_name)) = object.get("tool_name") {
        let normalized = normalize_opencode_tool_name(tool_name);
        object.insert("tool_name".to_string(), Value::String(normalized));
    }

    if !object.contains_key("tool_input") {
        if let Some(args) = object.remove("args") {
            object.insert("tool_input".to_string(), args);
        }
    }
    if !object.contains_key("tool_use_id") {
        if let Some(call_id) = object.remove("call_id") {
            object.insert("tool_use_id".to_string(), call_id);
        }
    }
    if !object.contains_key("tool_response") {
        if let Some(output) = object.remove("output") {
            object.insert("tool_response".to_string(), output);
        }
    }

    Value::Object(object)
}

/// Fail closed on a PreToolUse for an OpenCode tool id that is neither a
/// canonical built-in nor an MCP tool id.
///
/// OpenCode can add or rename a built-in without telling ExoMonad, and the role
/// hooks only match the canonical ids in `OPENCODE_CANONICAL_BUILTINS`. An
/// unrecognised id would therefore skip every role guardrail, so the id is
/// denied with a reason that names it and the table a maintainer must update.
///
/// PostToolUse/Stop (and every other event) return `None`: the call already
/// happened (or the event is not a tool gate), so they only observe and log.
pub fn opencode_unknown_tool_deny(
    event_type: HookEventType,
    input: &Value,
) -> Option<HookEnvelope> {
    if event_type != HookEventType::PreToolUse {
        return None;
    }

    let tool_name = opencode_tool_id(input)?;
    if is_known_opencode_tool(tool_name) {
        return None;
    }

    tracing::warn!(
        event = "hook.opencode_unknown_tool",
        tool_name,
        "Unrecognised OpenCode tool id denied on PreToolUse"
    );

    let reason = format!(
        "OpenCode tool id `{tool_name}` is not a known built-in or MCP tool, so no role \
         guardrail can match it. Add it to canonical_opencode_builtin in \
         rust/exomonad-core/src/protocol/hook.rs if OpenCode ships it as a built-in."
    );
    let output = ClaudePreToolUseOutput {
        continue_: false,
        stop_reason: Some(reason.clone()),
        hook_specific_output: Some(HookSpecificOutput::PreToolUse {
            permission_decision: ToolPermission::Deny,
            permission_decision_reason: Some(reason),
            updated_input: None,
        }),
        ..Default::default()
    };

    Some(HookEnvelope {
        stdout: serde_json::to_string(&output)
            .unwrap_or_else(|_| r#"{"continue":false}"#.to_string()),
        exit_code: 2,
    })
}

/// Read the provider tool id from the raw OpenCode payload, accepting the same
/// `tool` alias the normalizer does.
fn opencode_tool_id(input: &Value) -> Option<&str> {
    input
        .get("tool_name")
        .or_else(|| input.get("tool"))
        .and_then(Value::as_str)
}

/// The closed set of ids the adapter knows how to map. MCP tools are not in
/// this table: they carry an underscore (`<server>_<tool>`) and keep their
/// provider id, except the ExoMonad server whose prefix is stripped.
fn is_known_opencode_tool(tool_name: &str) -> bool {
    canonical_opencode_builtin(tool_name).is_some()
        || tool_name.starts_with("exomonad_")
        || tool_name.contains('_')
}

/// Map one OpenCode v2 tool id to the canonical name the WASM role hooks match
/// for `Runtime::OpenCode`.
///
/// `.exo/lib/HookPolicy.hs` matches OpenCode's own ids (`shell`, `edit`,
/// `write`, `patch`, …). This table is the single place that absorbs aliases
/// (`bash` from the v1 plugin, legacy `run_command`) onto those canonical ids,
/// so the Haskell role policy stays runtime-specific without a second mapping.
///
/// Every built-in id observed from `opencode v2.3.2` (`ctx.tool.list()` plus
/// the `execute` code-mode tool) has an explicit entry. ExoMonad's own MCP
/// server is registered as `exomonad`, so OpenCode presents its tools as
/// `exomonad_<tool>` (for example `exomonad_approve_pr`); those are reduced to
/// the bare MCP tool name the role hooks already use. Any other MCP tool keeps
/// its full server-prefixed id.
fn normalize_opencode_tool_name(tool_name: &str) -> String {
    if let Some(canonical) = canonical_opencode_builtin(tool_name) {
        return canonical.to_string();
    }

    if let Some(mcp_tool) = tool_name.strip_prefix("exomonad_") {
        tracing::debug!(
            event = "hook.opencode_mcp_tool",
            server = "exomonad",
            tool_name = mcp_tool,
            "Normalized ExoMonad MCP tool id"
        );
        return mcp_tool.to_string();
    }

    if tool_name.contains('_') {
        tracing::debug!(
            event = "hook.opencode_mcp_tool",
            tool_name,
            "Passing through external MCP tool id"
        );
        return tool_name.to_string();
    }

    tracing::warn!(
        event = "hook.opencode_unknown_tool",
        tool_name,
        "Unrecognised OpenCode tool id; PreToolUse denies it and other events only observe it"
    );
    tool_name.to_string()
}

/// The closed table of OpenCode v2.3.2 provider ids and the canonical ids the
/// WASM role hooks match.
///
/// The left column is the exact id the plugin sends; the right column is what
/// `.exo/lib/HookPolicy.hs` matches for `Runtime::OpenCode`. Add a new built-in
/// here *and* to `test_opencode_canonical_builtin_table_is_complete`; until
/// then PreToolUse denies it rather than letting it skip the guardrails.
const OPENCODE_CANONICAL_BUILTINS: &[(&str, &str)] = &[
    // Shell / command execution. `bash` is the v1 plugin name and
    // `run_command` is a legacy provider id; both map onto v2 `shell`.
    ("shell", "shell"),
    ("bash", "shell"),
    ("run_command", "shell"),
    // File reading and mutation. `multiedit` (a removed provider tool) is
    // folded onto `patch` so an old payload still hits the mutation guard.
    ("read", "read"),
    ("write", "write"),
    ("edit", "edit"),
    ("patch", "patch"),
    ("multiedit", "patch"),
    // Search.
    ("grep", "grep"),
    ("glob", "glob"),
    ("list", "list"),
    ("ls", "list"),
    // Network.
    ("webfetch", "webfetch"),
    ("websearch", "websearch"),
    // Delegation and interaction.
    ("subagent", "subagent"),
    ("task", "subagent"),
    ("question", "question"),
    ("skill", "skill"),
    // Todo tools (the experimental `todowrite` tool when enabled).
    ("todowrite", "todowrite"),
    ("todoread", "todoread"),
    // Code-mode tool; it executes and its inner calls are hooked separately.
    ("execute", "execute"),
    // Other built-ins with no role-guard equivalent. They keep their own
    // names because no role guardrail matches them.
    ("experimental_image_generate", "experimental_image_generate"),
    ("opencode_session_rename", "opencode_session_rename"),
    ("opencode_session_move", "opencode_session_move"),
    ("opencode_worktree_create", "opencode_worktree_create"),
    ("opencode_worktree_list", "opencode_worktree_list"),
    ("opencode_worktree_remove", "opencode_worktree_remove"),
    ("opencode_models", "opencode_models"),
    ("opencode_list_mcp_resources", "opencode_list_mcp_resources"),
    ("opencode_read_mcp_resource", "opencode_read_mcp_resource"),
];

fn canonical_opencode_builtin(tool_name: &str) -> Option<&str> {
    OPENCODE_CANONICAL_BUILTINS
        .iter()
        .find_map(|(provider, canonical)| (*provider == tool_name).then_some(*canonical))
}

fn opencode_hook_event_name(event_type: HookEventType) -> &'static str {
    match event_type {
        HookEventType::PreToolUse | HookEventType::BeforeTool => "PreToolUse",
        HookEventType::PostToolUse => "PostToolUse",
        HookEventType::Stop | HookEventType::AfterAgent => "Stop",
        HookEventType::SubagentStop => "SubagentStop",
        HookEventType::WorkerExit => "WorkerExit",
        HookEventType::SessionStart => "SessionStart",
        HookEventType::SessionEnd => "SessionEnd",
        HookEventType::BeforeModel => "BeforeModel",
        HookEventType::AfterModel => "AfterModel",
        _ => "PreToolUse",
    }
}

/// OpenCode's terminal `session.execution.*` event carries the session in the
/// event envelope rather than a top-level `session_id`.
fn opencode_event_session_id(object: &Map<String, Value>) -> Option<String> {
    object
        .get("data")
        .and_then(|data| data.get("sessionID"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            object
                .get("durable")
                .and_then(|durable| durable.get("aggregateID"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
        })
        .map(str::to_string)
}

fn insert_default_string(object: &mut Map<String, Value>, key: &str, default: &str) {
    if !has_non_empty_string(object, key) {
        object.insert(key.to_string(), Value::String(default.to_string()));
    }
}

fn normalize_optional_string(object: &mut Map<String, Value>, key: &str) {
    match object.get(key) {
        Some(Value::Null) | None => {
            object.insert(key.to_string(), Value::String(String::new()));
        }
        _ => {}
    }
}

fn has_non_empty_string(object: &Map<String, Value>, key: &str) -> bool {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false)
}

fn format_codex_pre_tool_use(envelope: HookEnvelope) -> HookEnvelope {
    let Ok(output) = serde_json::from_str::<ClaudePreToolUseOutput>(&envelope.stdout) else {
        return codex_noop_envelope();
    };

    if !output.continue_ {
        return codex_deny_envelope(
            output
                .stop_reason
                .unwrap_or_else(|| "Hook blocked tool execution".to_string()),
        );
    }

    match output.hook_specific_output {
        Some(HookSpecificOutput::PreToolUse {
            permission_decision,
            permission_decision_reason,
            updated_input,
        }) => match permission_decision {
            crate::domain::ToolPermission::Deny => codex_deny_envelope(
                permission_decision_reason
                    .unwrap_or_else(|| "Hook denied tool execution".to_string()),
            ),
            crate::domain::ToolPermission::Allow => match updated_input {
                Some(updated_input) => codex_updated_input_envelope(updated_input),
                None => codex_noop_envelope(),
            },
            crate::domain::ToolPermission::Ask => {
                codex_deny_envelope(permission_decision_reason.unwrap_or_else(|| {
                    "Interactive permission requests are unsupported".to_string()
                }))
            }
        },
        _ => codex_noop_envelope(),
    }
}

fn format_codex_post_tool_use(envelope: HookEnvelope) -> HookEnvelope {
    let Ok(output) = serde_json::from_str::<ClaudePreToolUseOutput>(&envelope.stdout) else {
        return codex_noop_envelope();
    };

    let mut object = Map::new();
    if !output.continue_ {
        object.insert("continue".to_string(), Value::Bool(false));
        if let Some(reason) = output.stop_reason {
            object.insert("stopReason".to_string(), Value::String(reason));
        }
    }

    if let Some(system_message) = output.system_message {
        object.insert("systemMessage".to_string(), Value::String(system_message));
    }

    if let Some(HookSpecificOutput::PostToolUse {
        additional_context: Some(additional_context),
    }) = output.hook_specific_output
    {
        object.insert(
            "hookSpecificOutput".to_string(),
            serde_json::json!({
                "hookEventName": "PostToolUse",
                "additionalContext": additional_context
            }),
        );
    }

    codex_json_envelope(Value::Object(object))
}

fn format_codex_stop(envelope: HookEnvelope) -> HookEnvelope {
    let Ok(output) = serde_json::from_str::<ClaudeStopHookOutput>(&envelope.stdout) else {
        return codex_noop_envelope();
    };

    if output.continue_ {
        return codex_noop_envelope();
    }

    let mut object = Map::new();
    object.insert("decision".to_string(), Value::String("block".to_string()));
    if let Some(reason) = output.stop_reason {
        object.insert("reason".to_string(), Value::String(reason));
    }
    codex_json_envelope(Value::Object(object))
}

fn codex_deny_envelope(reason: String) -> HookEnvelope {
    codex_json_envelope(serde_json::json!({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason
        }
    }))
}

fn codex_updated_input_envelope(updated_input: Value) -> HookEnvelope {
    codex_json_envelope(serde_json::json!({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input
        }
    }))
}

fn codex_json_envelope(value: Value) -> HookEnvelope {
    HookEnvelope {
        stdout: serde_json::to_string(&value).unwrap_or_else(|_| "{}".to_string()),
        exit_code: 0,
    }
}

// ============================================================================
// Model hook domain types (from WASM, serialized directly)
// ============================================================================

/// Internal BeforeModel hook output from WASM.
/// Same pattern: Haskell ToJSON produces runtime-compatible format.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InternalBeforeModelOutput {
    /// Whether to continue.
    #[serde(rename = "continue", default = "default_true")]
    pub continue_: bool,

    /// Decision: "deny" to block.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decision: Option<String>,

    /// Reason for denying.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,

    /// Hook-specific output (e.g., llm_request or llm_response).
    #[serde(skip_serializing_if = "Option::is_none", rename = "hookSpecificOutput")]
    pub hook_specific_output: Option<Value>,
}

/// Internal AfterModel hook output from WASM.
/// Same pattern: Haskell ToJSON produces runtime-compatible format.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InternalAfterModelOutput {
    /// Whether to continue.
    #[serde(rename = "continue", default = "default_true")]
    pub continue_: bool,

    /// Decision: "deny" to discard chunk.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decision: Option<String>,

    /// Reason for denying.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,

    /// Hook-specific output (e.g., rewritten llm_response).
    #[serde(skip_serializing_if = "Option::is_none", rename = "hookSpecificOutput")]
    pub hook_specific_output: Option<Value>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[test]
    fn test_hook_input_deserialize() {
        let json = r#"{
            "session_id": "abc123",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/home/user/project",
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.txt", "content": "hello"},
            "tool_use_id": "toolu_123"
        }"#;

        let input: HookInput = serde_json::from_str(json).unwrap();
        assert_eq!(input.hook_event_name, "PreToolUse");
        assert_eq!(input.tool_name.as_ref().map(|t| t.as_str()), Some("Write"));
    }

    #[test]
    fn test_hook_output_serialize() {
        let output = ClaudePreToolUseOutput::pre_tool_use_allow(
            Some("Allowed by ExoMonad".to_string()),
            Some(serde_json::json!({"file_path": "/tmp/safe.txt"})),
        );

        let json = serde_json::to_string_pretty(&output).unwrap();
        assert!(json.contains("permissionDecision"));
        assert!(json.contains("allow"));
    }

    // =========================================================================
    // ClaudePreToolUseOutput comprehensive serialization tests
    // =========================================================================

    #[test]
    fn test_hook_output_pre_tool_use_allow_format() {
        let output = ClaudePreToolUseOutput::pre_tool_use_allow(Some("test reason".into()), None);
        let json = serde_json::to_value(&output).unwrap();

        assert_eq!(json["continue"], true);
        assert!(
            json["stopReason"].is_null() || !json.as_object().unwrap().contains_key("stopReason")
        );

        let specific = &json["hookSpecificOutput"];
        assert_eq!(specific["hookEventName"], "PreToolUse");
        assert_eq!(specific["permissionDecision"], "allow");
        assert_eq!(specific["permissionDecisionReason"], "test reason");
    }

    #[test]
    fn test_hook_output_pre_tool_use_deny_format() {
        let output = ClaudePreToolUseOutput::pre_tool_use_deny("not allowed".into());
        let json = serde_json::to_value(&output).unwrap();

        assert_eq!(json["continue"], true); // deny still continues, just blocks this tool
        let specific = &json["hookSpecificOutput"];
        assert_eq!(specific["hookEventName"], "PreToolUse");
        assert_eq!(specific["permissionDecision"], "deny");
        assert_eq!(specific["permissionDecisionReason"], "not allowed");
    }

    #[test]
    fn test_hook_output_pre_tool_use_with_updated_input() {
        let modified = serde_json::json!({"file_path": "/safe/path.txt"});
        let output = ClaudePreToolUseOutput::pre_tool_use_allow(None, Some(modified.clone()));
        let json = serde_json::to_value(&output).unwrap();

        let specific = &json["hookSpecificOutput"];
        assert_eq!(specific["updatedInput"], modified);
    }

    #[test]
    fn test_hook_output_block_format() {
        let output = ClaudePreToolUseOutput::block("session terminated".into());
        let json = serde_json::to_value(&output).unwrap();

        assert_eq!(json["continue"], false);
        assert_eq!(json["stopReason"], "session terminated");
    }

    #[test]
    fn test_hook_output_post_tool_use_format() {
        let output = ClaudePreToolUseOutput::post_tool_use_allow(Some("additional context".into()));
        let json = serde_json::to_value(&output).unwrap();

        assert_eq!(json["continue"], true);
        let specific = &json["hookSpecificOutput"];
        assert_eq!(specific["hookEventName"], "PostToolUse");
        assert_eq!(specific["additionalContext"], "additional context");
    }

    #[test]
    fn test_hook_output_default() {
        let output = ClaudePreToolUseOutput::default();
        let json = serde_json::to_value(&output).unwrap();

        // Default should have continue=true and no hook_specific_output
        assert_eq!(json["continue"], true);
    }

    #[test]
    fn test_normalize_codex_hook_payload_maps_tool_fields() {
        let input = serde_json::json!({
            "session_id": "codex-session",
            "event": "pre-tool-use",
            "tool": "bash",
            "args": {"command": "ls"}
        });

        let normalized = normalize_codex_hook_payload(HookEventType::PreToolUse, input);
        assert_eq!(normalized["hook_event_name"], "PreToolUse");
        assert_eq!(normalized["tool_name"], "bash");
        assert_eq!(
            normalized["tool_input"],
            serde_json::json!({"command": "ls"})
        );
        assert_eq!(normalized["permission_mode"], "default");
    }

    #[test]
    fn test_normalize_codex_session_start_keeps_session_start_event() {
        let normalized =
            normalize_codex_hook_payload(HookEventType::SessionStart, serde_json::json!({}));
        assert_eq!(normalized["hook_event_name"], "SessionStart");
    }

    #[test]
    fn test_normalize_opencode_pre_tool_use_is_parseable_hook_input() {
        // Exact shape recorded from opencode v2.3.2 (#1585 / #1586 evidence).
        let input = serde_json::json!({
            "tool_name": "shell",
            "session_id": "ses_123",
            "call_id": "call-456",
            "args": {"command": "gh auth status"}
        });

        let normalized = normalize_opencode_hook_payload(HookEventType::PreToolUse, input);
        assert_eq!(normalized["hook_event_name"], "PreToolUse");
        assert_eq!(normalized["tool_name"], "shell");
        assert_eq!(
            normalized["tool_input"],
            serde_json::json!({"command": "gh auth status"})
        );
        assert_eq!(normalized["tool_use_id"], "call-456");
        assert_eq!(normalized["session_id"], "ses_123");

        let parsed: HookInput = serde_json::from_value(normalized).expect("parseable HookInput");
        assert_eq!(parsed.tool_name.as_ref().map(|t| t.as_str()), Some("shell"));
    }

    #[test]
    fn test_normalize_opencode_post_tool_use_maps_output() {
        let input = serde_json::json!({
            "tool_name": "read",
            "session_id": "ses_123",
            "call_id": "call-456",
            "args": {"path": "sample.txt"},
            "output": {"exitCode": 0, "output": "alpha\n"}
        });

        let normalized = normalize_opencode_hook_payload(HookEventType::PostToolUse, input);
        assert_eq!(normalized["hook_event_name"], "PostToolUse");
        assert_eq!(normalized["tool_name"], "read");
        assert_eq!(
            normalized["tool_input"],
            serde_json::json!({"path": "sample.txt"})
        );
        assert_eq!(
            normalized["tool_response"],
            serde_json::json!({"exitCode": 0, "output": "alpha\n"})
        );
    }

    #[test]
    fn test_normalize_opencode_stop_event_extracts_session() {
        let input = serde_json::json!({
            "id": "evt_1",
            "type": "session.execution.succeeded",
            "durable": {"aggregateID": "ses_durable", "seq": 25},
            "data": {"sessionID": "ses_data"}
        });

        let normalized = normalize_opencode_hook_payload(HookEventType::Stop, input);
        assert_eq!(normalized["hook_event_name"], "Stop");
        assert_eq!(normalized["session_id"], "ses_data");

        let parsed: HookInput = serde_json::from_value(normalized).expect("parseable HookInput");
        assert_eq!(parsed.session_id.as_str(), "ses_data");
    }

    #[test]
    fn test_normalize_opencode_stop_event_falls_back_to_durable_aggregate() {
        let input = serde_json::json!({
            "type": "session.execution.failed",
            "durable": {"aggregateID": "ses_durable"},
            "data": {}
        });

        let normalized = normalize_opencode_hook_payload(HookEventType::Stop, input);
        assert_eq!(normalized["session_id"], "ses_durable");
    }

    #[test]
    fn test_normalize_opencode_worker_exit_event() {
        let input = serde_json::json!({
            "type": "session.execution.succeeded",
            "durable": {"aggregateID": "ses_durable"}
        });
        let normalized = normalize_opencode_hook_payload(HookEventType::WorkerExit, input);
        assert_eq!(normalized["hook_event_name"], "WorkerExit");
    }

    #[test]
    fn test_normalize_opencode_builtin_tool_names() {
        // Every built-in id returned by opencode v2.3.2 `ctx.tool.list()` plus
        // the `execute` code-mode tool. The canonical name is what the WASM
        // role hooks match.
        let cases = [
            ("shell", "shell"),
            ("bash", "shell"),
            ("run_command", "shell"),
            ("read", "read"),
            ("write", "write"),
            ("edit", "edit"),
            ("patch", "patch"),
            ("multiedit", "patch"),
            ("grep", "grep"),
            ("glob", "glob"),
            ("list", "list"),
            ("ls", "list"),
            ("webfetch", "webfetch"),
            ("websearch", "websearch"),
            ("task", "subagent"),
            ("subagent", "subagent"),
            ("question", "question"),
            ("skill", "skill"),
            ("todowrite", "todowrite"),
            ("todoread", "todoread"),
            ("execute", "execute"),
            ("experimental_image_generate", "experimental_image_generate"),
            ("opencode_session_rename", "opencode_session_rename"),
            ("opencode_session_move", "opencode_session_move"),
            ("opencode_worktree_create", "opencode_worktree_create"),
            ("opencode_worktree_list", "opencode_worktree_list"),
            ("opencode_worktree_remove", "opencode_worktree_remove"),
            ("opencode_models", "opencode_models"),
            ("opencode_list_mcp_resources", "opencode_list_mcp_resources"),
            ("opencode_read_mcp_resource", "opencode_read_mcp_resource"),
        ];

        for (provider_name, canonical) in cases {
            let input = serde_json::json!({
                "tool_name": provider_name,
                "session_id": "ses",
                "args": {}
            });
            let normalized = normalize_opencode_hook_payload(HookEventType::PreToolUse, input);
            assert_eq!(
                normalized["tool_name"], canonical,
                "OpenCode tool id {provider_name} must map to {canonical}"
            );
        }
    }

    #[test]
    fn test_normalize_opencode_exomonad_mcp_tool_names() {
        for (provider_name, expected) in [
            ("exomonad_approve_pr", "approve_pr"),
            ("exomonad_request_changes", "request_changes"),
            ("exomonad_file_pr", "file_pr"),
            ("exomonad_notify_parent", "notify_parent"),
            // The bare name has no underscore; the raw `exomonad_` prefix is
            // what keeps it out of the fail-closed path.
            ("exomonad_merge", "merge"),
        ] {
            let input = serde_json::json!({
                "tool_name": provider_name,
                "session_id": "ses",
                "args": {}
            });
            let normalized =
                normalize_opencode_hook_payload(HookEventType::PreToolUse, input.clone());
            assert_eq!(normalized["tool_name"], expected);
            assert!(
                opencode_unknown_tool_deny(HookEventType::PreToolUse, &input).is_none(),
                "ExoMonad MCP id {provider_name} must not be denied"
            );
        }
    }

    #[test]
    fn test_normalize_opencode_external_mcp_tool_keeps_server_prefix() {
        for tool_name in [
            "tilth_tilth_search",
            "probe_probe_echo",
            "context-mode_context_search",
            "context-mode_ctx_execute",
        ] {
            let input = serde_json::json!({
                "tool_name": tool_name,
                "session_id": "ses",
                "args": {}
            });
            let normalized =
                normalize_opencode_hook_payload(HookEventType::PreToolUse, input.clone());
            assert_eq!(normalized["tool_name"], tool_name);
            assert!(
                opencode_unknown_tool_deny(HookEventType::PreToolUse, &input).is_none(),
                "external MCP id {tool_name} must not be denied"
            );
        }
    }

    #[test]
    fn test_opencode_unknown_non_mcp_tool_is_denied_on_pre_tool_use() {
        let input = serde_json::json!({
            "tool_name": "futuretool",
            "session_id": "ses",
            "args": {}
        });
        let deny = opencode_unknown_tool_deny(HookEventType::PreToolUse, &input)
            .expect("unknown non-MCP id must be denied on PreToolUse");

        assert_eq!(deny.exit_code, 2);
        let envelope: Value = serde_json::from_str(&deny.stdout).expect("deny envelope JSON");
        assert_eq!(envelope["continue"], false);
        assert_eq!(envelope["hookSpecificOutput"]["permissionDecision"], "deny");
        let reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
            .as_str()
            .expect("deny reason");
        assert!(reason.contains("futuretool"), "{reason}");
        assert!(reason.contains("canonical_opencode_builtin"), "{reason}");
        assert_eq!(envelope["stopReason"].as_str(), Some(reason));

        // The mapping itself still preserves the id for observation.
        let normalized = normalize_opencode_hook_payload(HookEventType::PreToolUse, input);
        assert_eq!(normalized["tool_name"], "futuretool");
    }

    /// The table is the closed set. This expectation is deliberately separate
    /// from `OPENCODE_CANONICAL_BUILTINS`, so deleting an entry makes the test
    /// fail instead of shrinking the assertion with the table.
    #[test]
    fn test_opencode_canonical_builtin_table_is_complete() {
        let expected = [
            ("shell", "shell"),
            ("bash", "shell"),
            ("run_command", "shell"),
            ("read", "read"),
            ("write", "write"),
            ("edit", "edit"),
            ("patch", "patch"),
            ("multiedit", "patch"),
            ("grep", "grep"),
            ("glob", "glob"),
            ("list", "list"),
            ("ls", "list"),
            ("webfetch", "webfetch"),
            ("websearch", "websearch"),
            ("subagent", "subagent"),
            ("task", "subagent"),
            ("question", "question"),
            ("skill", "skill"),
            ("todowrite", "todowrite"),
            ("todoread", "todoread"),
            ("execute", "execute"),
            ("experimental_image_generate", "experimental_image_generate"),
            ("opencode_session_rename", "opencode_session_rename"),
            ("opencode_session_move", "opencode_session_move"),
            ("opencode_worktree_create", "opencode_worktree_create"),
            ("opencode_worktree_list", "opencode_worktree_list"),
            ("opencode_worktree_remove", "opencode_worktree_remove"),
            ("opencode_models", "opencode_models"),
            ("opencode_list_mcp_resources", "opencode_list_mcp_resources"),
            ("opencode_read_mcp_resource", "opencode_read_mcp_resource"),
        ];

        assert_eq!(OPENCODE_CANONICAL_BUILTINS, expected.as_slice());
    }

    /// Every id in the real table must pass PreToolUse unchanged (or be folded
    /// onto its canonical alias) and must never be denied.
    #[test]
    fn test_opencode_every_canonical_builtin_passes_and_is_not_denied() {
        for (provider_name, canonical) in OPENCODE_CANONICAL_BUILTINS {
            let input = serde_json::json!({
                "tool_name": provider_name,
                "session_id": "ses",
                "args": {}
            });
            let normalized =
                normalize_opencode_hook_payload(HookEventType::PreToolUse, input.clone());
            assert_eq!(
                normalized["tool_name"], *canonical,
                "table id {provider_name} must map to {canonical}"
            );
            assert!(
                opencode_unknown_tool_deny(HookEventType::PreToolUse, &input).is_none(),
                "table id {provider_name} must not be denied"
            );
        }
    }

    /// An unknown id on an observation-only event is logged but never denied,
    /// because the tool call already happened (PostToolUse) or the event is not
    /// a tool gate (Stop).
    #[test]
    fn test_opencode_unknown_tool_on_post_and_stop_is_logged_not_denied() {
        for event in [HookEventType::PostToolUse, HookEventType::Stop] {
            let captured = Arc::new(Mutex::new(Vec::<String>::new()));
            let guard = tracing::subscriber::set_default(capture_tracing_events(captured.clone()));

            let input = serde_json::json!({
                "tool_name": "futuretool",
                "session_id": "ses",
                "args": {}
            });
            assert!(
                opencode_unknown_tool_deny(event, &input).is_none(),
                "{event:?} must never deny an unknown id"
            );
            let normalized = normalize_opencode_hook_payload(event, input);
            drop(guard);

            assert_eq!(normalized["tool_name"], "futuretool");
            let records = captured.lock().expect("capture lock");
            assert!(
                records.iter().any(|message| {
                    message.contains("hook.opencode_unknown_tool")
                        && message.contains("futuretool")
                        && message.contains("Warn")
                }),
                "{event:?} must log hook.opencode_unknown_tool; got {records:?}"
            );
        }
    }

    /// A tracing subscriber that records each event's level and fields so a
    /// test can prove an observation-only path really logged.
    fn capture_tracing_events(
        sink: Arc<Mutex<Vec<String>>>,
    ) -> impl tracing::Subscriber + Send + Sync + 'static {
        use std::fmt::Write as _;
        use tracing_subscriber::layer::{Context, Layer, SubscriberExt};
        use tracing_subscriber::registry::LookupSpan;

        struct Capture(Arc<Mutex<Vec<String>>>);

        #[derive(Default)]
        struct Fields(String);

        impl tracing::field::Visit for Fields {
            fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
                let _ = write!(self.0, " {}={:?}", field.name(), value);
            }
        }

        impl<S> Layer<S> for Capture
        where
            S: tracing::Subscriber + for<'a> LookupSpan<'a>,
        {
            fn on_event(&self, event: &tracing::Event<'_>, _context: Context<'_, S>) {
                let mut fields = Fields::default();
                event.record(&mut fields);
                self.0.lock().expect("capture lock").push(format!(
                    "LEVEL_{:?}{}",
                    event.metadata().level(),
                    fields.0
                ));
            }
        }

        tracing_subscriber::registry().with(Capture(sink))
    }

    #[test]
    fn test_format_codex_pre_tool_use_deny() {
        let output = ClaudePreToolUseOutput::pre_tool_use_deny("blocked".into());
        let envelope = HookEnvelope {
            stdout: serde_json::to_string(&output).unwrap(),
            exit_code: 0,
        };

        let formatted = format_codex_hook_response(HookEventType::PreToolUse, envelope);
        let json: Value = serde_json::from_str(&formatted.stdout).unwrap();
        let specific = &json["hookSpecificOutput"];
        assert_eq!(formatted.exit_code, 0);
        assert_eq!(specific["hookEventName"], "PreToolUse");
        assert_eq!(specific["permissionDecision"], "deny");
        assert_eq!(specific["permissionDecisionReason"], "blocked");
    }

    #[test]
    fn test_format_codex_stop_block() {
        let output = ClaudeStopHookOutput {
            continue_: false,
            stop_reason: Some("run tests".into()),
        };
        let envelope = HookEnvelope {
            stdout: serde_json::to_string(&output).unwrap(),
            exit_code: 0,
        };

        let formatted = format_codex_hook_response(HookEventType::Stop, envelope);
        let json: Value = serde_json::from_str(&formatted.stdout).unwrap();
        assert_eq!(formatted.exit_code, 0);
        assert_eq!(json["decision"], "block");
        assert_eq!(json["reason"], "run tests");
    }

    #[test]
    fn test_format_codex_stop_block_without_reason_stays_silent() {
        let output = ClaudeStopHookOutput {
            continue_: false,
            stop_reason: None,
        };
        let envelope = HookEnvelope {
            stdout: serde_json::to_string(&output).unwrap(),
            exit_code: 0,
        };

        let formatted = format_codex_hook_response(HookEventType::Stop, envelope);
        let json: Value = serde_json::from_str(&formatted.stdout).unwrap();
        assert_eq!(formatted.exit_code, 0);
        assert_eq!(json["decision"], "block");
        assert!(json.get("reason").is_none());
    }

    // =========================================================================
    // HookInput comprehensive parsing tests
    // =========================================================================

    #[test]
    fn test_hook_input_minimal() {
        let json = r#"{"session_id":"s","hook_event_name":"Stop"}"#;
        let input: HookInput = serde_json::from_str(json).unwrap();
        assert_eq!(input.session_id.as_str(), "s");
        assert_eq!(input.hook_event_name, "Stop");
        assert!(input.tool_name.is_none());
    }

    #[test]
    fn test_hook_input_with_tool_parameters_alias() {
        // Some runtimes use tool_parameters instead of tool_input.
        let json = r#"{"session_id":"s","hook_event_name":"PreToolUse","tool_parameters":{"key":"value"}}"#;
        let input: HookInput = serde_json::from_str(json).unwrap();
        assert!(input.tool_input.is_some());
        assert_eq!(input.tool_input.unwrap()["key"], "value");
    }

    #[test]
    fn test_hook_input_extra_fields_ignored() {
        let json = r#"{"session_id":"s","hook_event_name":"Stop","unknown_field":"ignored","another":123}"#;
        let result: Result<HookInput, _> = serde_json::from_str(json);
        assert!(result.is_ok());
    }

    #[test]
    fn test_hook_input_unknown_permission_mode() {
        let json = r#"{"session_id":"s","hook_event_name":"Stop","permission_mode":"futureMode"}"#;
        let input: HookInput = serde_json::from_str(json).unwrap();
        assert_eq!(input.permission_mode, PermissionMode::Default);
    }

    #[test]
    fn test_hook_input_missing_permission_mode() {
        let json = r#"{"session_id":"s","hook_event_name":"Stop"}"#;
        let input: HookInput = serde_json::from_str(json).unwrap();
        assert_eq!(input.permission_mode, PermissionMode::Default);
    }

    #[test]
    fn test_hook_input_all_fields() {
        let json = r#"{
            "session_id": "sess-123",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/home/user",
            "permission_mode": "plan",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/x"},
            "tool_use_id": "toolu_abc",
            "prompt": "user prompt",
            "message": "notification",
            "stop_hook_active": true
        }"#;
        let input: HookInput = serde_json::from_str(json).unwrap();
        assert_eq!(input.session_id.as_str(), "sess-123");
        assert_eq!(input.cwd, "/home/user");
        assert_eq!(input.permission_mode, PermissionMode::Plan);
        assert_eq!(input.tool_name.as_ref().map(|t| t.as_str()), Some("Write"));
        assert_eq!(input.prompt, Some("user prompt".into()));
        assert_eq!(input.stop_hook_active, Some(true));
    }
}

#[cfg(test)]
mod proptest_tests {
    use super::*;
    use proptest::prelude::*;

    fn arb_session_id() -> impl Strategy<Value = SessionId> {
        "[a-zA-Z0-9]+".prop_map(|s| SessionId::try_from(s).unwrap())
    }

    fn arb_tool_name() -> impl Strategy<Value = ToolName> {
        "[a-zA-Z0-9_]+".prop_map(|s| ToolName::try_from(s).unwrap())
    }

    fn arb_runtime() -> impl Strategy<Value = Runtime> {
        prop_oneof![Just(Runtime::Claude), Just(Runtime::OpenCode)]
    }

    fn arb_permission_mode() -> impl Strategy<Value = PermissionMode> {
        prop_oneof![
            Just(PermissionMode::Default),
            Just(PermissionMode::Plan),
            Just(PermissionMode::AcceptEdits),
            Just(PermissionMode::DontAsk),
            Just(PermissionMode::BypassPermissions),
        ]
    }

    fn arb_json_value() -> impl Strategy<Value = Value> {
        prop_oneof![
            any::<bool>().prop_map(Value::Bool),
            any::<String>().prop_map(Value::String),
        ]
    }

    fn arb_hook_input() -> impl Strategy<Value = HookInput> {
        (
            (
                arb_session_id(),
                any::<String>(),
                any::<String>(),
                arb_permission_mode(),
                any::<String>(),
                prop::option::weighted(0.5, arb_runtime()),
                prop::option::weighted(0.5, arb_tool_name()),
                prop::option::weighted(0.5, arb_json_value()),
                prop::option::weighted(0.5, any::<String>()),
                prop::option::weighted(0.5, arb_json_value()),
            ),
            (
                prop::option::weighted(0.5, any::<String>()),
                prop::option::weighted(0.5, any::<String>()),
                prop::option::weighted(0.5, any::<String>()),
                prop::option::weighted(0.5, any::<bool>()),
            ),
        )
            .prop_map(
                |(
                    (
                        session_id,
                        transcript_path,
                        cwd,
                        permission_mode,
                        hook_event_name,
                        runtime,
                        tool_name,
                        tool_input,
                        tool_use_id,
                        tool_response,
                    ),
                    (prompt, message, notification_type, stop_hook_active),
                )| {
                    HookInput {
                        session_id,
                        transcript_path,
                        cwd,
                        permission_mode,
                        hook_event_name,
                        runtime,
                        tool_name,
                        tool_input,
                        tool_use_id,
                        tool_response,
                        prompt,
                        message,
                        notification_type,
                        stop_hook_active,
                        trigger: None,
                        custom_instructions: None,
                        source: None,
                        reason: None,
                        prompt_response: None,
                        timestamp: None,
                        llm_request: None,
                        llm_response: None,
                    }
                },
            )
    }

    proptest! {
        #[test]
        fn test_hook_input_roundtrip(input in arb_hook_input()) {
            let json = serde_json::to_string(&input).unwrap();
            let back: HookInput = serde_json::from_str(&json).unwrap();
            prop_assert_eq!(back, input);
        }

        #[test]
        fn test_hook_envelope_roundtrip(
            stdout in any::<String>(),
            exit_code in any::<i32>()
        ) {
            let env = HookEnvelope { stdout, exit_code };
            let json = serde_json::to_string(&env).unwrap();
            let back: HookEnvelope = serde_json::from_str(&json).unwrap();
            prop_assert_eq!(back.stdout, env.stdout);
            prop_assert_eq!(back.exit_code, env.exit_code);
        }

        #[test]
        fn test_claude_pre_tool_use_output_roundtrip(
            continue_ in any::<bool>(),
            stop_reason in prop::option::weighted(0.5, any::<String>()),
            suppress_output in prop::option::weighted(0.5, any::<bool>()),
            system_message in prop::option::weighted(0.5, any::<String>())
        ) {
            let output = ClaudePreToolUseOutput {
                continue_,
                stop_reason,
                suppress_output,
                system_message,
                hook_specific_output: None, // Testing base fields first
            };
            let json = serde_json::to_string(&output).unwrap();
            let back: ClaudePreToolUseOutput = serde_json::from_str(&json).unwrap();
            prop_assert_eq!(back.continue_, output.continue_);
            prop_assert_eq!(back.stop_reason, output.stop_reason);
            prop_assert_eq!(back.suppress_output, output.suppress_output);
            prop_assert_eq!(back.system_message, output.system_message);
        }
    }
}
