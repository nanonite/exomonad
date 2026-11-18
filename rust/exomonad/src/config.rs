//! Configuration discovery from .exo/config.toml and config.local.toml

use anyhow::{Context, Result};
use clap::ValueEnum;
use exomonad_core::services::{normalize_tl_preflight_runtime_path, AgentType};
use exomonad_core::Role;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use tracing::{debug, warn};

pub const REVIEWER_MAX_ROUNDS_ENV: &str = "EXOMONAD_REVIEWER_MAX_ROUNDS";
pub const REVIEWER_MODEL_ENV: &str = "EXOMONAD_REVIEWER_MODEL";
pub const REVIEWER_EFFORT_ENV: &str = "EXOMONAD_REVIEWER_EFFORT_LEVEL";
pub const TL_EFFORT_ENV: &str = "EXOMONAD_TL_EFFORT_LEVEL";
pub const WORKER_EFFORT_ENV: &str = "EXOMONAD_WORKER_EFFORT_LEVEL";
pub const TL_MODEL_ENV: &str = "EXOMONAD_TL_MODEL";
pub const WORKER_MODEL_ENV: &str = "EXOMONAD_WORKER_MODEL";
pub const TL_MODEL_SOURCE_ENV: &str = "EXOMONAD_TL_MODEL_SOURCE";
pub const WORKER_MODEL_SOURCE_ENV: &str = "EXOMONAD_WORKER_MODEL_SOURCE";
pub const REVIEWER_MODEL_SOURCE_ENV: &str = "EXOMONAD_REVIEWER_MODEL_SOURCE";
pub const TL_EFFORT_SOURCE_ENV: &str = "EXOMONAD_TL_EFFORT_SOURCE";
pub const WORKER_EFFORT_SOURCE_ENV: &str = "EXOMONAD_WORKER_EFFORT_SOURCE";
pub const REVIEWER_EFFORT_SOURCE_ENV: &str = "EXOMONAD_REVIEWER_EFFORT_SOURCE";
pub const ROOT_AGENT_SOURCE_ENV: &str = "EXOMONAD_ROOT_AGENT_TYPE_SOURCE";
pub const WORKER_AGENT_SOURCE_ENV: &str = "EXOMONAD_SPAWN_AGENT_TYPE_SOURCE";
pub const REVIEWER_AGENT_SOURCE_ENV: &str = "EXOMONAD_REVIEWER_AGENT_TYPE_SOURCE";
pub use exomonad_core::services::TL_PREFLIGHT_RUNTIME_PATHS_ENV;
pub const DEFAULT_TL_TRANSPORT_TIMEOUT_SECONDS: f64 = 10.0;
pub const DEFAULT_TL_ACTIVE_TAIL_TIMEOUT_SECONDS: f64 = 30.0;
pub const DEFAULT_TL_TASK_TIMEOUT_SECONDS: f64 = 3600.0;
pub const DEFAULT_TL_DISPATCH_RETRY_LIMIT: u32 = 3;
pub const DEFAULT_TL_DISPATCH_RETRY_BASE_DELAY_SECONDS: f64 = 5.0;
pub const DEFAULT_TL_DISPATCH_RETRY_MAX_DELAY_SECONDS: f64 = 60.0;

pub fn parse_positive_u32(value: &str) -> std::result::Result<u32, String> {
    let parsed = value
        .parse::<u32>()
        .map_err(|error| format!("must be a positive integer: {error}"))?;
    if parsed == 0 {
        return Err("must be at least 1".to_string());
    }
    Ok(parsed)
}

fn require_positive_timeout(name: &str, value: f64) -> Result<()> {
    // `is_nan()` rejects NaN explicitly; `<= 0.0` rejects zero and negatives.
    if value.is_nan() || value <= 0.0 {
        anyhow::bail!("{name} must be greater than zero");
    }
    Ok(())
}

fn require_task_timeout(name: &str, value: f64) -> Result<()> {
    if value.is_nan() || value < 0.0 {
        anyhow::bail!("{name} must be zero or greater");
    }
    Ok(())
}

/// Validate the dispatch-retry backoff pair: both delays must be positive and
/// the ceiling must not sit below the first delay, or a backoff could be
/// scheduled *earlier* than the first retry's own floor.
fn require_dispatch_retry_delays(
    base_name: &str,
    base: f64,
    max_name: &str,
    max: f64,
) -> Result<()> {
    require_positive_timeout(base_name, base)?;
    require_positive_timeout(max_name, max)?;
    if max < base {
        anyhow::bail!("{max_name} must be greater than or equal to {base_name}");
    }
    Ok(())
}

/// A zero budget would park every retryable refusal on its first failure,
/// which is the behavior this knob exists to remove.
fn require_dispatch_retry_limit(name: &str, value: u32) -> Result<()> {
    if value == 0 {
        anyhow::bail!("{name} must be at least 1");
    }
    Ok(())
}

fn validate_preflight_runtime_paths(paths: &[String]) -> Result<()> {
    for path in paths {
        normalize_tl_preflight_runtime_path(path).map_err(anyhow::Error::msg)?;
    }
    Ok(())
}

fn parse_agent_type_env(s: &str) -> Option<AgentType> {
    match s.trim().to_ascii_lowercase().as_str() {
        "claude" | "claude-code" => Some(AgentType::Claude),
        "opencode" | "opencode-cli" => Some(AgentType::OpenCode),
        "codex" => Some(AgentType::Codex),
        "shoal" => Some(AgentType::Shoal),
        _ => None,
    }
}

fn parse_agent_type_override(name: &str, value: &str) -> Result<AgentType> {
    parse_agent_type_env(value).ok_or_else(|| anyhow::anyhow!("invalid {name} value `{value}`"))
}

fn present_value(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn resolve_agent_type_value(
    environment: Option<String>,
    environment_source: Option<String>,
    primary: Option<AgentType>,
    primary_source: RouteValueSource,
    secondary: Option<AgentType>,
    secondary_source: RouteValueSource,
    default: AgentType,
    environment_name: &str,
) -> Result<(AgentType, RouteValueSource)> {
    if let Some(value) = present_value(environment) {
        let source =
            parse_route_source_hint(environment_source)?.unwrap_or(RouteValueSource::Environment);
        return Ok((parse_agent_type_override(environment_name, &value)?, source));
    }
    if let Some(value) = primary {
        return Ok((value, primary_source));
    }
    if let Some(value) = secondary {
        return Ok((value, secondary_source));
    }
    Ok((default, RouteValueSource::Default))
}

fn parse_effort_override(name: &str, value: Option<String>) -> Result<Option<EffortLevel>> {
    let Some(value) = present_value(value) else {
        return Ok(None);
    };
    let level = match value.to_ascii_lowercase().as_str() {
        "low" => EffortLevel::Low,
        "medium" => EffortLevel::Medium,
        "high" => EffortLevel::High,
        "xhigh" => EffortLevel::XHigh,
        "max" => EffortLevel::Max,
        _ => anyhow::bail!(
            "invalid {name} value `{value}`; expected low, medium, high, xhigh, or max"
        ),
    };
    Ok(Some(level))
}

fn resolve_model_value(
    environment: Option<String>,
    environment_source: Option<String>,
    local: Option<String>,
    project: Option<String>,
) -> Result<(Option<String>, RouteValueSource)> {
    if let Some(value) = present_value(environment) {
        let source =
            parse_route_source_hint(environment_source)?.unwrap_or(RouteValueSource::Environment);
        return Ok((Some(value), source));
    }
    if let Some(value) = present_value(local) {
        return Ok((Some(value), RouteValueSource::LocalConfig));
    }
    if let Some(value) = present_value(project) {
        return Ok((Some(value), RouteValueSource::ProjectConfig));
    }
    Ok((None, RouteValueSource::Unresolved))
}

fn resolve_effort_with_environment(
    environment: Option<EffortLevel>,
    environment_source: Option<String>,
    local: Option<EffortLevel>,
    project: Option<EffortLevel>,
) -> Result<ResolvedEffort> {
    Ok(match (environment, local, project) {
        (Some(level), _, _) => ResolvedEffort {
            level,
            source: parse_effort_source_hint(environment_source)?
                .unwrap_or(EffortSource::Environment),
        },
        (None, Some(level), _) | (None, None, Some(level)) => ResolvedEffort {
            level,
            source: EffortSource::Config,
        },
        (None, None, None) => ResolvedEffort::MEDIUM_DEFAULT,
    })
}

fn parse_route_source_hint(value: Option<String>) -> Result<Option<RouteValueSource>> {
    let Some(value) = present_value(value) else {
        return Ok(None);
    };
    let source = RouteValueSource::parse(&value)?;
    if source == RouteValueSource::Unresolved {
        anyhow::bail!("an injected route value cannot have unresolved provenance");
    }
    Ok(Some(source))
}

fn parse_effort_source_hint(value: Option<String>) -> Result<Option<EffortSource>> {
    parse_route_source_hint(value)?
        .map(effort_source_from_route_source)
        .transpose()
}

fn effort_source_from_route_source(source: RouteValueSource) -> Result<EffortSource> {
    match source {
        RouteValueSource::Cli => Ok(EffortSource::Cli),
        RouteValueSource::Policy => Ok(EffortSource::Policy),
        RouteValueSource::Environment => Ok(EffortSource::Environment),
        RouteValueSource::LocalConfig | RouteValueSource::ProjectConfig => Ok(EffortSource::Config),
        RouteValueSource::Default => Ok(EffortSource::Default),
        RouteValueSource::HarnessDefault | RouteValueSource::Unresolved => {
            anyhow::bail!("invalid effort provenance `{}`", source.as_str())
        }
    }
}

/// Canonical route role used by init, serve, and role-policy consumers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CanonicalRole {
    Tl,
    Worker,
    Reviewer,
}

impl CanonicalRole {
    /// Resolve supported role aliases without guessing from arbitrary names.
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "tl" | "root" | "subtl" | "sub-tl" | "controller" | "judge" => Ok(Self::Tl),
            "worker" | "dev" | "dev-leaf" | "dev_leaf" | "leaf" | "teammate" => Ok(Self::Worker),
            "reviewer" | "pr-reviewer" | "pr_reviewer" => Ok(Self::Reviewer),
            _ => anyhow::bail!("unsupported runtime role `{value}`"),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Tl => "tl",
            Self::Worker => "worker",
            Self::Reviewer => "reviewer",
        }
    }
}

/// Provenance for one resolved role-route field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteValueSource {
    Cli,
    Policy,
    Environment,
    LocalConfig,
    ProjectConfig,
    Default,
    HarnessDefault,
    Unresolved,
}

impl RouteValueSource {
    pub fn as_str(self) -> &'static str {
        route_source_name(self)
    }

    fn parse(value: &str) -> Result<Self> {
        match value {
            "cli" => Ok(Self::Cli),
            "policy" => Ok(Self::Policy),
            "environment" => Ok(Self::Environment),
            "config" => Ok(Self::ProjectConfig),
            "local_config" => Ok(Self::LocalConfig),
            "project_config" => Ok(Self::ProjectConfig),
            "default" => Ok(Self::Default),
            "harness_default" => Ok(Self::HarnessDefault),
            "unresolved" => Ok(Self::Unresolved),
            _ => anyhow::bail!("invalid route provenance `{value}`"),
        }
    }
}

/// A route selected by the accepted task/role policy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyRouteSelection {
    pub agent_type: AgentType,
    pub model: Option<String>,
    pub effort: Option<EffortLevel>,
}

impl PolicyRouteSelection {
    /// Parse one already-selected `harness[/model]` policy identity.
    pub fn parse(identifier: &str, effort: Option<EffortLevel>) -> Result<Self> {
        let (harness, model) = identifier
            .split_once('/')
            .map_or((identifier, None), |(harness, model)| {
                (harness, Some(model))
            });
        let agent_type = parse_agent_type_override("policy route", harness)?;
        if matches!(agent_type, AgentType::Process) {
            anyhow::bail!("policy route cannot select the process harness");
        }
        let model = model.map(str::trim).filter(|model| !model.is_empty());
        if identifier.contains('/') && model.is_none() {
            anyhow::bail!("policy route `{identifier}` has an empty model identifier");
        }
        Ok(Self {
            agent_type,
            model: model.map(str::to_string),
            effort,
        })
    }
}

/// Explicit per-invocation route overrides supplied by the CLI.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RoleRouteOverrides {
    pub agent_type: Option<AgentType>,
    pub model: Option<String>,
    pub effort: Option<EffortLevel>,
}

/// One fully resolved route, including the source of every selected value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ResolvedRoleRoute {
    pub role: CanonicalRole,
    pub agent_type: AgentType,
    pub model: Option<String>,
    pub effort: ResolvedEffort,
    pub agent_type_source: RouteValueSource,
    pub model_source: RouteValueSource,
    pub configuration_fingerprint: String,
    pub route_fingerprint: String,
}

/// Longest tmux session name, in characters, that ExoMonad will use.
///
/// Enforced at the config-load boundary: a longer name is rejected, never
/// shortened. Truncation would leave the server's `TmuxIpc` probing a session
/// that is not the one the configured name refers to, so every
/// `routing_liveness` check (`window_exists`,
/// `routing_target_process_alive`) would report every agent dead and read as a
/// product fault rather than as a naming mistake.
pub const TMUX_SESSION_MAX_CHARS: usize = 36;

/// Project-relative path of the shared config file.
const PROJECT_CONFIG_PATH: &str = ".exo/config.toml";

/// Project-relative path of the per-worktree config file.
const LOCAL_CONFIG_PATH: &str = ".exo/config.local.toml";

/// Which input supplied a tmux session name. Recorded so a rejected name can
/// say which file has to change.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionNameSource {
    /// `.exo/config.local.toml`, which wins over the project file.
    LocalConfig,
    /// `.exo/config.toml`.
    ProjectConfig,
    /// The project directory's own name, used when neither file sets one.
    ProjectDirectory,
    /// The `exomonad init --session` override.
    CommandLine,
}

/// External MCP server configuration (HTTP or stdio).
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "type")]
pub enum McpServerConfig {
    #[serde(rename = "http")]
    Http {
        url: String,
        #[serde(default)]
        headers: std::collections::HashMap<String, String>,
    },
    #[serde(rename = "stdio")]
    Stdio {
        command: String,
        #[serde(default)]
        args: Vec<String>,
    },
}

/// Companion agent spawned alongside the TL during init.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CompanionConfig {
    /// Agent name (used for tmux window, identity files).
    pub name: String,
    /// WASM role for MCP tools (default: "worker").
    #[serde(default = "default_companion_role")]
    pub role: String,
    /// Agent type. Determines how MCP config is wired.
    /// Optional at parse time; init warns and defaults to Claude if missing.
    pub agent_type: Option<AgentType>,
    /// Command to launch (e.g., "claude --dangerously-skip-permissions -c").
    pub command: String,
    /// Task/prompt passed as positional arg to the command. None = interactive (no initial prompt).
    pub task: Option<String>,
    /// Model override (e.g., "haiku", "sonnet"). Passed as --model flag to the selected CLI.
    pub model: Option<String>,
}

fn default_companion_role() -> String {
    "worker".to_string()
}

/// Configuration for routing LLM calls through OpenRouter.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct OpenRouterConfig {
    /// When false (default), all calls go direct to Anthropic/Google.
    #[serde(default)]
    pub enabled: bool,
    /// API key. Falls back to OPENROUTER_API_KEY env var if absent.
    pub api_key: Option<String>,
}

impl OpenRouterConfig {
    /// Resolve the API key: config field first, then OPENROUTER_API_KEY env var.
    pub fn resolved_api_key(&self) -> Option<String> {
        self.api_key
            .clone()
            .or_else(|| std::env::var("OPENROUTER_API_KEY").ok())
    }
}

/// Configuration for the PR reviewer agent.
#[derive(Debug, Clone, Deserialize)]
pub struct ReviewerConfig {
    /// Agent type for the reviewer. Accepts "claude" or "opencode". Default: claude.
    #[serde(default = "default_reviewer_agent_type")]
    pub agent_type: AgentType,
    /// Optional reviewer effort override from the [reviewer] table.
    pub effort_level: Option<EffortLevel>,
    /// Model string passed to the reviewer agent
    /// (e.g. "claude-haiku-4-5-20251001", "anthropic/claude-haiku-4-5").
    /// `None` means the harness picks its own native default; it must never
    /// inherit a worker or TL model.
    pub model: Option<String>,
    /// Context file paths injected into the reviewer's session.
    #[serde(default)]
    pub context: Vec<String>,
}

fn default_reviewer_agent_type() -> AgentType {
    AgentType::Claude
}

impl Default for ReviewerConfig {
    fn default() -> Self {
        Self {
            agent_type: AgentType::Claude,
            effort_level: None,
            model: None,
            context: vec![],
        }
    }
}

/// Opencode agent configuration.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct OpencodeConfig {
    /// When true, use the embedded API key in the opencode binary.
    /// When false, redirect opencode to use the OpenRouter API key
    /// (from openrouter config or OPENROUTER_API_KEY env var).
    #[serde(default)]
    pub use_embedded_key: bool,
    /// Model for the root TL when running OpenCode (e.g. "anthropic/claude-sonnet-4-5").
    /// `None` means let opencode pick its default.
    pub tl_model: Option<String>,
    /// Model for spawned workers when running OpenCode.
    /// `None` means let opencode pick its default.
    pub worker_model: Option<String>,
}

/// Raw configuration from file (supports both config.toml and config.local.toml fields).
#[derive(Debug, Clone, Deserialize, Default)]
pub struct RawConfig {
    /// Project directory for git operations.
    pub project_dir: Option<PathBuf>,

    /// Specific role for this worktree (local config).
    pub role: Option<Role>,

    /// Project-wide default role.
    pub default_role: Option<Role>,

    /// Canonical tmux session name for this project.
    ///
    /// At most [`TMUX_SESSION_MAX_CHARS`] characters; a longer value is
    /// rejected when the config is loaded rather than shortened.
    pub tmux_session: Option<String>,

    /// TCP port for public webhook and health endpoints.
    pub port: Option<u16>,

    /// Base directory for worktrees (default: .exo/worktrees).
    pub worktree_base: Option<PathBuf>,

    /// Shell command to wrap environment (e.g. "nix develop"). TL tab runs this as shell.
    pub shell_command: Option<String>,

    /// WASM directory override (default: .exo/wasm/).
    pub wasm_dir: Option<PathBuf>,

    /// Agent type for the root (TL) tab.
    pub root_agent_type: Option<AgentType>,
    /// Default effort level for the root TL.
    pub tl_effort_level: Option<EffortLevel>,

    /// Agent type for spawned workers/teammates.
    pub spawn_agent_type: Option<AgentType>,
    /// Default effort level for spawned workers and agent companions.
    pub worker_effort_level: Option<EffortLevel>,
    /// Model for spawned workers and leaves, independent of harness.
    pub worker_model: Option<String>,

    /// Optional flake reference to use when building WASM plugin via nix.
    pub flake_ref: Option<String>,

    /// Name of the WASM module (default: "devswarm"). Used to find wasm-guest-{name}.wasm.
    pub wasm_name: Option<String>,

    /// Extra MCP servers to include in agent settings (e.g. metacog).
    #[serde(default)]
    pub extra_mcp_servers: std::collections::HashMap<String, McpServerConfig>,

    /// Initial prompt for the root agent.
    pub initial_prompt: Option<String>,

    /// Custom command for the root TL window (overrides agent_type-based default).
    /// Use for development (e.g., `cargo run -p shoal-agent -- --exo root`).
    pub root_command: Option<String>,

    /// Retained for configuration compatibility; no longer changes harness behavior.
    #[serde(default)]
    pub yolo: bool,

    /// Companion agents to spawn alongside the TL.
    #[serde(default)]
    pub companions: Vec<CompanionConfig>,

    /// OTLP gRPC endpoint (e.g. "http://localhost:4317").
    /// If absent, OTel export is disabled (fmt-only tracing).
    pub otlp_endpoint: Option<String>,

    /// Model override for the root TL agent (e.g., "sonnet", "haiku"). Passed as --model flag.
    pub model: Option<String>,

    /// GitHub poller interval in seconds (default: 60).
    pub poll_interval: Option<u64>,

    /// Unread inbox poke interval in seconds (default: 300).
    pub inbox_poke_interval: Option<u64>,

    /// Orphan reconciler interval in seconds (default: 60).
    pub orphan_reconciler_interval_secs: Option<u64>,

    /// TL loop server transport timeout in seconds (default: 10).
    pub tl_transport_timeout_seconds: Option<f64>,

    /// TL loop ledger active-tail timeout in seconds (default: 30).
    pub tl_active_tail_timeout_seconds: Option<f64>,

    /// Project default per-task ceiling in seconds (default: 3600; zero disables).
    pub tl_task_timeout_seconds: Option<f64>,

    /// Bounded retry budget for a retryable leaf-dispatch refusal (default: 3).
    pub tl_dispatch_retry_limit: Option<u32>,

    /// First dispatch-retry backoff in seconds (default: 5.0).
    pub tl_dispatch_retry_base_delay_seconds: Option<f64>,

    /// Ceiling on the dispatch-retry backoff in seconds (default: 60.0).
    pub tl_dispatch_retry_max_delay_seconds: Option<f64>,

    /// Additional relative runtime paths ignored by TL spawn preflight.
    /// Built-in ExoMonad paths are always excluded; local role config overrides project config.
    pub tl_preflight_runtime_paths: Option<Vec<String>>,

    /// OpenRouter routing configuration.
    #[serde(default)]
    pub openrouter: Option<OpenRouterConfig>,

    /// Opencode agent configuration.
    #[serde(default)]
    pub opencode: Option<OpencodeConfig>,

    /// Use OpenCode as the root TL agent instead of Claude.
    /// Can be set via CLI flag --opencode-as-tl.
    #[serde(default)]
    pub opencode_as_tl: Option<bool>,

    /// Forgejo base URL (e.g. "http://localhost:3000").
    pub forgejo_url: Option<String>,

    /// Forgejo token for API/webhook setup.
    pub forgejo_token: Option<String>,

    /// Forgejo token for reviewer API calls. Must belong to a different user from the PR author token.
    pub forgejo_reviewer_token: Option<String>,

    /// Shared secret used to verify Forgejo webhook signatures.
    pub forgejo_webhook_secret: Option<String>,

    /// SSH port for Forgejo git remotes. Defaults to 2222 for localhost, otherwise 22.
    pub forgejo_ssh_port: Option<u16>,

    /// PR reviewer agent configuration.
    #[serde(default)]
    pub reviewer: Option<ReviewerConfig>,
}

/// Final resolved configuration.
#[derive(Debug, Clone)]
pub struct Config {
    pub project_dir: PathBuf,
    pub role: Role,
    /// Canonical tmux session name (required after discovery).
    ///
    /// Resolved from `config.local.toml`, then `config.toml`, then the project
    /// directory's own name, and validated at load: see
    /// [`TMUX_SESSION_MAX_CHARS`].
    pub tmux_session: String,
    /// TCP port for public webhook and health endpoints.
    pub port: u16,
    /// Base directory for worktrees.
    pub worktree_base: PathBuf,
    /// Shell command to wrap environment (e.g. "nix develop").
    pub shell_command: Option<String>,
    /// Resolved WASM directory.
    pub wasm_dir: PathBuf,
    /// Agent type for the root (TL) tab.
    pub root_agent_type: AgentType,
    pub root_agent_type_source: RouteValueSource,
    /// Resolved effort level for the root TL.
    pub tl_effort_level: ResolvedEffort,
    /// Agent type for spawned workers/teammates.
    pub spawn_agent_type: AgentType,
    pub spawn_agent_type_source: RouteValueSource,
    /// Resolved effort level for spawned workers and agent companions.
    pub worker_effort_level: ResolvedEffort,
    /// Model override for spawned workers, independent of harness.
    pub worker_model: Option<String>,
    pub worker_model_source: RouteValueSource,
    /// Resolved effort level for reviewers.
    pub reviewer_effort_level: ResolvedEffort,
    /// Flake reference to use when building WASM plugin via nix.
    pub flake_ref: Option<String>,
    /// Name of the WASM module (default: "devswarm").
    pub wasm_name: String,
    /// Extra MCP servers to include in agent settings.
    pub extra_mcp_servers: std::collections::HashMap<String, McpServerConfig>,
    /// Initial prompt for the root agent.
    pub initial_prompt: Option<String>,
    /// Retained for configuration compatibility; no longer changes harness behavior.
    pub yolo: bool,
    /// Companion agents to spawn alongside the TL.
    pub companions: Vec<CompanionConfig>,
    /// Custom command for the root TL window (overrides agent_type-based default).
    pub root_command: Option<String>,
    /// OTLP gRPC endpoint (e.g. "http://localhost:4317").
    /// If absent, OTel export is disabled (fmt-only tracing).
    pub otlp_endpoint: Option<String>,
    /// Model override for the root TL agent (e.g., "sonnet", "haiku"). Passed as --model flag.
    pub model: Option<String>,
    pub model_source: RouteValueSource,

    /// GitHub poller interval in seconds (default: 60).
    pub poll_interval: Option<u64>,

    /// Unread inbox poke interval in seconds (default: 300).
    pub inbox_poke_interval: Option<u64>,

    /// Orphan reconciler interval in seconds (default: 60).
    pub orphan_reconciler_interval_secs: Option<u64>,

    /// TL loop server transport timeout in seconds.
    pub tl_transport_timeout_seconds: f64,

    /// TL loop ledger active-tail timeout in seconds.
    pub tl_active_tail_timeout_seconds: f64,

    /// Project default per-task ceiling in seconds; zero means no ceiling.
    pub tl_task_timeout_seconds: f64,

    /// Bounded retry budget for a retryable leaf-dispatch refusal.
    pub tl_dispatch_retry_limit: u32,

    /// First dispatch-retry backoff in seconds.
    pub tl_dispatch_retry_base_delay_seconds: f64,

    /// Ceiling on the dispatch-retry backoff in seconds.
    pub tl_dispatch_retry_max_delay_seconds: f64,

    /// Additional relative runtime paths ignored by TL spawn preflight.
    pub tl_preflight_runtime_paths: Vec<String>,

    /// OpenRouter routing configuration.
    pub openrouter: OpenRouterConfig,

    /// Opencode agent configuration.
    pub opencode: OpencodeConfig,
    pub opencode_tl_model_source: RouteValueSource,
    pub opencode_worker_model_source: RouteValueSource,

    /// Use OpenCode as the root TL agent instead of Claude.
    pub opencode_as_tl: bool,

    /// Forgejo base URL (e.g. "http://localhost:3000").
    pub forgejo_url: Option<String>,

    /// Forgejo token for API/webhook setup.
    pub forgejo_token: Option<String>,

    /// Forgejo token for reviewer API calls. Must belong to a different user from the PR author token.
    pub forgejo_reviewer_token: Option<String>,

    /// Shared secret used to verify Forgejo webhook signatures.
    pub forgejo_webhook_secret: Option<String>,

    /// SSH port for Forgejo git remotes. Defaults to 2222 for localhost, otherwise 22.
    pub forgejo_ssh_port: Option<u16>,

    /// PR reviewer agent configuration.
    pub reviewer: ReviewerConfig,
    pub reviewer_agent_type_source: RouteValueSource,
    pub reviewer_model_source: RouteValueSource,
}

impl Config {
    /// Discover configuration by merging local and global project config.
    ///
    /// Searches upward from CWD for `.exo/config.toml`.
    ///
    /// Resolution Order:
    /// 1. config.local.toml (role)
    /// 2. config.toml (default_role, project_dir)
    /// 3. Environment defaults
    pub fn discover() -> Result<Self> {
        Self::discover_from(&find_project_root()?, &|name| std::env::var(name).ok())
    }

    /// Resolve configuration from a known project root and an injected environment.
    /// This is also the production path used by `discover`, with environment access
    /// isolated here so route precedence can be verified without mutating process state.
    pub fn discover_from(
        project_root: &Path,
        environment: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self> {
        let project_root = project_root.to_path_buf();

        let local_path = project_root.join(LOCAL_CONFIG_PATH);
        let global_path = project_root.join(PROJECT_CONFIG_PATH);

        let local_raw = if local_path.exists() {
            debug!(path = %local_path.display(), "Loaded local config");
            Self::load_raw(&local_path)?
        } else {
            RawConfig::default()
        };

        let global_raw = if global_path.exists() {
            debug!(path = %global_path.display(), "Loaded global config");
            Self::load_raw(&global_path)?
        } else {
            RawConfig::default()
        };

        // Resolve role: local.role > global.default_role > TL
        let role = local_raw
            .role
            .or(global_raw.default_role)
            .unwrap_or_else(Role::tl);

        // Resolve project_dir: global.project_dir > project_root
        let project_dir = global_raw
            .project_dir
            .or(local_raw.project_dir)
            .map(|p| {
                if p.is_absolute() {
                    p
                } else {
                    project_root.join(p)
                }
            })
            .unwrap_or_else(|| project_root.clone());

        // Resolve tmux_session: local config > project config > directory name.
        // A name over the limit is rejected here, never truncated: a shortened
        // name would make the server probe a session nobody created.
        let tmux_session = resolve_tmux_session(
            local_raw.tmux_session,
            global_raw.tmux_session,
            &project_root,
        )?
        .name;

        // Resolve TCP port: local > global > default.
        let port = local_raw.port.or(global_raw.port).unwrap_or(7433);

        // Resolve worktree_base: global > local > default (.exo/worktrees)
        let worktree_base = global_raw
            .worktree_base
            .or(local_raw.worktree_base)
            .map(|p| {
                if p.is_absolute() {
                    p
                } else {
                    project_root.join(p)
                }
            })
            .unwrap_or_else(|| project_root.join(".exo/worktrees"));

        // Resolve shell_command: local > global
        let shell_command = local_raw.shell_command.or(global_raw.shell_command);

        // Resolve wasm_dir: config > .exo/wasm/ (project-local)
        let wasm_dir = global_raw
            .wasm_dir
            .or(local_raw.wasm_dir)
            .map(|p| {
                if p.is_absolute() {
                    p
                } else {
                    project_root.join(p)
                }
            })
            .unwrap_or_else(|| project_root.join(".exo/wasm"));

        // Root harness retains its existing env > project > local precedence.
        let (root_agent_type, root_agent_type_source) = resolve_agent_type_value(
            environment("EXOMONAD_ROOT_AGENT_TYPE"),
            environment(ROOT_AGENT_SOURCE_ENV),
            global_raw.root_agent_type,
            RouteValueSource::ProjectConfig,
            local_raw.root_agent_type,
            RouteValueSource::LocalConfig,
            AgentType::Claude,
            "EXOMONAD_ROOT_AGENT_TYPE",
        )?;

        // Worker harness retains its existing env > local > project precedence.
        let (spawn_agent_type, spawn_agent_type_source) = resolve_agent_type_value(
            environment("EXOMONAD_SPAWN_AGENT_TYPE"),
            environment(WORKER_AGENT_SOURCE_ENV),
            local_raw.spawn_agent_type,
            RouteValueSource::LocalConfig,
            global_raw.spawn_agent_type,
            RouteValueSource::ProjectConfig,
            AgentType::default(),
            "EXOMONAD_SPAWN_AGENT_TYPE",
        )?;

        let tl_effort_level = resolve_effort_with_environment(
            parse_effort_override(TL_EFFORT_ENV, environment(TL_EFFORT_ENV))?,
            environment(TL_EFFORT_SOURCE_ENV),
            local_raw.tl_effort_level,
            global_raw.tl_effort_level,
        )?;
        let worker_effort_level = resolve_effort_with_environment(
            parse_effort_override(WORKER_EFFORT_ENV, environment(WORKER_EFFORT_ENV))?,
            environment(WORKER_EFFORT_SOURCE_ENV),
            local_raw.worker_effort_level,
            global_raw.worker_effort_level,
        )?;

        // Resolve flake_ref: local > global > fallback to None
        let flake_ref = local_raw.flake_ref.or(global_raw.flake_ref);

        // Merge extra_mcp_servers: global first, local overrides
        let mut extra_mcp_servers = global_raw.extra_mcp_servers;
        extra_mcp_servers.extend(local_raw.extra_mcp_servers);

        let wasm_name = local_raw
            .wasm_name
            .or(global_raw.wasm_name)
            .or_else(|| detect_role_name(&project_dir))
            .unwrap_or_else(|| "devswarm".to_string());

        let initial_prompt = local_raw.initial_prompt.or(global_raw.initial_prompt);

        // Resolve yolo: local > global > false
        let yolo = local_raw.yolo || global_raw.yolo;

        let companions = if !local_raw.companions.is_empty() {
            local_raw.companions
        } else {
            global_raw.companions
        };

        // Resolve root_command: local > global
        let root_command = local_raw.root_command.or(global_raw.root_command);

        // Resolve otlp_endpoint: local > global
        let otlp_endpoint = local_raw.otlp_endpoint.or(global_raw.otlp_endpoint);

        // Root and worker models are role-scoped and harness-independent.
        let (model, model_source) = resolve_model_value(
            environment(TL_MODEL_ENV),
            environment(TL_MODEL_SOURCE_ENV),
            local_raw.model,
            global_raw.model,
        )?;
        let (mut worker_model, mut worker_model_source) = resolve_model_value(
            environment(WORKER_MODEL_ENV),
            environment(WORKER_MODEL_SOURCE_ENV),
            local_raw.worker_model,
            global_raw.worker_model,
        )?;

        // Resolve poll_interval: local > global
        let poll_interval = local_raw.poll_interval.or(global_raw.poll_interval);
        let inbox_poke_interval = local_raw
            .inbox_poke_interval
            .or(global_raw.inbox_poke_interval);
        let orphan_reconciler_interval_secs = local_raw
            .orphan_reconciler_interval_secs
            .or(global_raw.orphan_reconciler_interval_secs);

        let tl_transport_timeout_seconds = local_raw
            .tl_transport_timeout_seconds
            .or(global_raw.tl_transport_timeout_seconds)
            .unwrap_or(DEFAULT_TL_TRANSPORT_TIMEOUT_SECONDS);
        let tl_active_tail_timeout_seconds = local_raw
            .tl_active_tail_timeout_seconds
            .or(global_raw.tl_active_tail_timeout_seconds)
            .unwrap_or(DEFAULT_TL_ACTIVE_TAIL_TIMEOUT_SECONDS);
        let tl_task_timeout_seconds = local_raw
            .tl_task_timeout_seconds
            .or(global_raw.tl_task_timeout_seconds)
            .unwrap_or(DEFAULT_TL_TASK_TIMEOUT_SECONDS);

        let tl_dispatch_retry_limit = local_raw
            .tl_dispatch_retry_limit
            .or(global_raw.tl_dispatch_retry_limit)
            .unwrap_or(DEFAULT_TL_DISPATCH_RETRY_LIMIT);
        let tl_dispatch_retry_base_delay_seconds = local_raw
            .tl_dispatch_retry_base_delay_seconds
            .or(global_raw.tl_dispatch_retry_base_delay_seconds)
            .unwrap_or(DEFAULT_TL_DISPATCH_RETRY_BASE_DELAY_SECONDS);
        let tl_dispatch_retry_max_delay_seconds = local_raw
            .tl_dispatch_retry_max_delay_seconds
            .or(global_raw.tl_dispatch_retry_max_delay_seconds)
            .unwrap_or(DEFAULT_TL_DISPATCH_RETRY_MAX_DELAY_SECONDS);

        let tl_preflight_runtime_paths = local_raw
            .tl_preflight_runtime_paths
            .or(global_raw.tl_preflight_runtime_paths)
            .unwrap_or_default();
        validate_preflight_runtime_paths(&tl_preflight_runtime_paths)?;

        for (name, value) in [
            ("tl_transport_timeout_seconds", tl_transport_timeout_seconds),
            (
                "tl_active_tail_timeout_seconds",
                tl_active_tail_timeout_seconds,
            ),
        ] {
            require_positive_timeout(name, value)?;
        }
        require_task_timeout("tl_task_timeout_seconds", tl_task_timeout_seconds)?;
        require_dispatch_retry_limit("tl_dispatch_retry_limit", tl_dispatch_retry_limit)?;
        require_dispatch_retry_delays(
            "tl_dispatch_retry_base_delay_seconds",
            tl_dispatch_retry_base_delay_seconds,
            "tl_dispatch_retry_max_delay_seconds",
            tl_dispatch_retry_max_delay_seconds,
        )?;

        // Resolve openrouter: local > global > default
        let openrouter = local_raw
            .openrouter
            .or(global_raw.openrouter)
            .unwrap_or_default();

        // Legacy OpenCode-only model fields remain supported as a fallback for
        // OpenCode routes. Generic role settings above always take precedence.
        let opencode_tl_model_source = if local_raw
            .opencode
            .as_ref()
            .and_then(|value| value.tl_model.as_ref())
            .is_some()
        {
            RouteValueSource::LocalConfig
        } else if global_raw
            .opencode
            .as_ref()
            .and_then(|value| value.tl_model.as_ref())
            .is_some()
        {
            RouteValueSource::ProjectConfig
        } else {
            RouteValueSource::Unresolved
        };
        let opencode_worker_model_source = if local_raw
            .opencode
            .as_ref()
            .and_then(|value| value.worker_model.as_ref())
            .is_some()
        {
            RouteValueSource::LocalConfig
        } else if global_raw
            .opencode
            .as_ref()
            .and_then(|value| value.worker_model.as_ref())
            .is_some()
        {
            RouteValueSource::ProjectConfig
        } else {
            RouteValueSource::Unresolved
        };
        let opencode = local_raw
            .opencode
            .or(global_raw.opencode)
            .unwrap_or_default();
        if worker_model.is_none() && spawn_agent_type == AgentType::OpenCode {
            if let Some(model) = present_value(opencode.worker_model.clone()) {
                worker_model = Some(model);
                worker_model_source = opencode_worker_model_source;
            }
        }

        // Resolve opencode_as_tl: local > global > false
        let opencode_as_tl = local_raw
            .opencode_as_tl
            .or(global_raw.opencode_as_tl)
            .unwrap_or(false);

        let forgejo_url = local_raw.forgejo_url.or(global_raw.forgejo_url);
        let forgejo_token = local_raw.forgejo_token.or(global_raw.forgejo_token);
        let forgejo_reviewer_token = local_raw
            .forgejo_reviewer_token
            .or(global_raw.forgejo_reviewer_token);
        let forgejo_webhook_secret = local_raw
            .forgejo_webhook_secret
            .or(global_raw.forgejo_webhook_secret);
        let forgejo_ssh_port = local_raw.forgejo_ssh_port.or(global_raw.forgejo_ssh_port);

        // Reviewer settings are independent of both TL and worker settings.
        let reviewer_agent_env = environment("EXOMONAD_REVIEWER_AGENT_TYPE");
        let (reviewer_agent_type, reviewer_agent_type_source) = resolve_agent_type_value(
            reviewer_agent_env,
            environment(REVIEWER_AGENT_SOURCE_ENV),
            local_raw.reviewer.as_ref().map(|value| value.agent_type),
            RouteValueSource::LocalConfig,
            global_raw.reviewer.as_ref().map(|value| value.agent_type),
            RouteValueSource::ProjectConfig,
            AgentType::Claude,
            "EXOMONAD_REVIEWER_AGENT_TYPE",
        )?;
        let (reviewer_model, reviewer_model_source) = resolve_model_value(
            environment(REVIEWER_MODEL_ENV),
            environment(REVIEWER_MODEL_SOURCE_ENV),
            local_raw
                .reviewer
                .as_ref()
                .and_then(|value| value.model.clone()),
            global_raw
                .reviewer
                .as_ref()
                .and_then(|value| value.model.clone()),
        )?;
        let reviewer_effort_override =
            parse_effort_override(REVIEWER_EFFORT_ENV, environment(REVIEWER_EFFORT_ENV))?;
        let reviewer_effort_level = resolve_effort_with_environment(
            reviewer_effort_override,
            environment(REVIEWER_EFFORT_SOURCE_ENV),
            local_raw
                .reviewer
                .as_ref()
                .and_then(|value| value.effort_level),
            global_raw
                .reviewer
                .as_ref()
                .and_then(|value| value.effort_level),
        )?;
        let mut reviewer = local_raw
            .reviewer
            .or(global_raw.reviewer)
            .unwrap_or_default();
        reviewer.agent_type = reviewer_agent_type;
        reviewer.model = reviewer_model;
        reviewer.effort_level = Some(reviewer_effort_level.level);

        Ok(Self {
            project_dir,
            role,
            tmux_session,
            port,
            worktree_base,
            shell_command,
            wasm_dir,
            root_agent_type,
            root_agent_type_source,
            tl_effort_level,
            spawn_agent_type,
            spawn_agent_type_source,
            worker_effort_level,
            worker_model,
            worker_model_source,
            reviewer_effort_level,
            flake_ref,
            wasm_name,
            extra_mcp_servers,
            initial_prompt,
            yolo,
            companions,
            root_command,
            otlp_endpoint,
            model,
            model_source,
            poll_interval,
            inbox_poke_interval,
            orphan_reconciler_interval_secs,
            tl_transport_timeout_seconds,
            tl_active_tail_timeout_seconds,
            tl_task_timeout_seconds,
            tl_dispatch_retry_limit,
            tl_dispatch_retry_base_delay_seconds,
            tl_dispatch_retry_max_delay_seconds,
            tl_preflight_runtime_paths,
            openrouter,
            opencode,
            opencode_tl_model_source,
            opencode_worker_model_source,
            opencode_as_tl,
            forgejo_url,
            forgejo_token,
            forgejo_reviewer_token,
            forgejo_webhook_secret,
            forgejo_ssh_port,
            reviewer,
            reviewer_agent_type_source,
            reviewer_model_source,
        })
    }

    fn load_raw(path: &Path) -> Result<RawConfig> {
        debug!(path = %path.display(), "Loading raw config");
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("Failed to read config file: {}", path.display()))?;

        let config: RawConfig = toml::from_str(&content)
            .with_context(|| format!("Failed to parse config file: {}", path.display()))?;

        Ok(config)
    }
}

#[cfg(test)]
mod role_routes_tests {
    use super::*;
    use std::collections::HashMap;

    fn discover_config(contents: &str, values: &[(&str, &str)]) -> Config {
        let temp = tempfile::tempdir().unwrap();
        let exo = temp.path().join(".exo");
        std::fs::create_dir_all(&exo).unwrap();
        std::fs::write(exo.join("config.toml"), contents).unwrap();
        let values: HashMap<String, String> = values
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect();
        Config::discover_from(temp.path(), &|name| values.get(name).cloned()).unwrap()
    }

    #[test]
    fn accepted_task_model_and_effort_beat_host_harness_defaults() {
        let config = discover_config(
            "spawn_agent_type = 'codex'\nworker_effort_level = 'medium'\n",
            &[],
        );
        let policy =
            PolicyRouteSelection::parse("codex/gpt-6-luna", Some(EffortLevel::XHigh)).unwrap();
        let route = config
            .resolve_role_route(
                "dev-leaf",
                Some(&policy),
                &RoleRouteOverrides::default(),
                Some("gpt-6.1-sol".to_string()),
            )
            .unwrap();

        assert_eq!(route.role, CanonicalRole::Worker);
        assert_eq!(route.agent_type, AgentType::Codex);
        assert_eq!(route.model.as_deref(), Some("gpt-6-luna"));
        assert_eq!(route.effort.level, EffortLevel::XHigh);
        assert_eq!(route.model_source, RouteValueSource::Policy);
        assert_eq!(route.effort.source, EffortSource::Policy);
        assert!(route.configuration_fingerprint.starts_with("sha256:v2:"));
        assert!(route.route_fingerprint.starts_with("sha256:v1:"));
    }

    #[test]
    fn generic_worker_environment_applies_to_codex_and_effort_has_provenance() {
        let config = discover_config(
            "spawn_agent_type = 'codex'\nworker_model = 'gpt-config-model'\nworker_effort_level = 'medium'\n",
            &[
                (WORKER_MODEL_ENV, "gpt-env-model"),
                (WORKER_EFFORT_ENV, "xhigh"),
            ],
        );
        let route = config
            .resolve_role_route("worker", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        assert_eq!(route.model.as_deref(), Some("gpt-env-model"));
        assert_eq!(route.model_source, RouteValueSource::Environment);
        assert_eq!(route.effort.level, EffortLevel::XHigh);
        assert_eq!(route.effort.source, EffortSource::Environment);
    }

    #[test]
    fn role_aliases_keep_tl_worker_and_reviewer_routes_independent() {
        let config = discover_config(
            "root_agent_type = 'claude'\nmodel = 'claude-tl'\nspawn_agent_type = 'opencode'\nworker_model = 'openai/worker'\nworker_effort_level = 'xhigh'\n[reviewer]\nagent_type = 'codex'\nmodel = 'gpt-reviewer'\neffort_level = 'low'\n",
            &[],
        );
        let defaults = RoleRouteOverrides::default();
        let tl = config
            .resolve_role_route("subTL", None, &defaults, None)
            .unwrap();
        let worker = config
            .resolve_role_route("dev-leaf", None, &defaults, None)
            .unwrap();
        let reviewer = config
            .resolve_role_route("pr-reviewer", None, &defaults, None)
            .unwrap();

        assert_eq!(
            (tl.agent_type, tl.model.as_deref()),
            (AgentType::Claude, Some("claude-tl"))
        );
        assert_eq!(
            (worker.agent_type, worker.model.as_deref()),
            (AgentType::OpenCode, Some("openai/worker"))
        );
        assert_eq!(worker.effort.level, EffortLevel::XHigh);
        assert_eq!(
            (reviewer.agent_type, reviewer.model.as_deref()),
            (AgentType::Codex, Some("gpt-reviewer"))
        );
        assert_eq!(reviewer.effort.level, EffortLevel::Low);
        assert_ne!(tl.route_fingerprint, worker.route_fingerprint);
        assert_ne!(worker.route_fingerprint, reviewer.route_fingerprint);
    }

    #[test]
    fn claude_and_opencode_worker_routes_ignore_contradictory_host_defaults() {
        for (harness, model, effort) in [
            ("claude", "claude-sonnet-4-5", "high"),
            ("opencode", "opencode-go/deepseek-v4.1-flash", "medium"),
        ] {
            let config = discover_config(
                &format!(
                    "spawn_agent_type = '{harness}'\nworker_model = '{model}'\nworker_effort_level = '{effort}'\n"
                ),
                &[],
            );
            let route = config
                .resolve_role_route(
                    "dev-leaf",
                    None,
                    &RoleRouteOverrides::default(),
                    Some("gpt-6.1-sol".to_string()),
                )
                .unwrap();
            assert_eq!(route.role, CanonicalRole::Worker);
            let expected_agent = match harness {
                "claude" => AgentType::Claude,
                "opencode" => AgentType::OpenCode,
                _ => unreachable!(),
            };
            assert_eq!(route.agent_type, expected_agent);
            assert_eq!(route.model.as_deref(), Some(model));
            assert_eq!(route.model_source, RouteValueSource::ProjectConfig);
            assert_eq!(route.effort.level.to_string(), effort);
        }
    }

    #[test]
    fn cli_overrides_are_explicit_and_empty_policy_models_are_rejected() {
        let config = discover_config("spawn_agent_type = 'codex'\n", &[]);
        let route = config
            .resolve_role_route(
                "dev",
                None,
                &RoleRouteOverrides {
                    agent_type: Some(AgentType::Claude),
                    model: Some("claude-selected".to_string()),
                    effort: Some(EffortLevel::High),
                },
                Some("gpt-host-default".to_string()),
            )
            .unwrap();
        assert_eq!(route.agent_type, AgentType::Claude);
        assert_eq!(route.model.as_deref(), Some("claude-selected"));
        assert_eq!(route.agent_type_source, RouteValueSource::Cli);
        assert_eq!(route.model_source, RouteValueSource::Cli);
        assert_eq!(route.effort.source, EffortSource::Cli);
        assert!(PolicyRouteSelection::parse("codex/ ", None).is_err());
        assert!(config
            .resolve_role_route(
                "worker",
                None,
                &RoleRouteOverrides {
                    model: Some("  ".to_string()),
                    ..RoleRouteOverrides::default()
                },
                None,
            )
            .is_err());
    }

    #[test]
    fn route_fingerprints_are_stable_and_change_with_role_configuration() {
        let first = discover_config("spawn_agent_type = 'codex'\nworker_model = 'gpt-a'\n", &[]);
        let repeated = discover_config("spawn_agent_type = 'codex'\nworker_model = 'gpt-a'\n", &[]);
        let changed = discover_config("spawn_agent_type = 'codex'\nworker_model = 'gpt-b'\n", &[]);
        assert_eq!(
            first.routing_configuration_fingerprint(),
            repeated.routing_configuration_fingerprint()
        );
        assert_ne!(
            first.routing_configuration_fingerprint(),
            changed.routing_configuration_fingerprint()
        );
    }

    /// The project settings used by the live reproduction: TL/reviewer
    /// `opencode-go/glm-5.3-flash` at high, worker
    /// `opencode-go/deepseek-v4.1-flash` at high.
    const LIVE_PROJECT_CONFIG: &str = "root_agent_type = 'opencode'\nmodel = 'opencode-go/glm-5.3-flash'\ntl_effort_level = 'high'\nspawn_agent_type = 'opencode'\nworker_model = 'opencode-go/deepseek-v4.1-flash'\nworker_effort_level = 'high'\n[reviewer]\nagent_type = 'opencode'\nmodel = 'opencode-go/glm-5.3-flash'\neffort_level = 'high'\n";

    fn mark_worker_and_reviewer_sources_as_cli(config: &mut Config) {
        config.spawn_agent_type_source = RouteValueSource::Cli;
        config.worker_model_source = RouteValueSource::Cli;
        config.opencode_worker_model_source = RouteValueSource::Cli;
        config.worker_effort_level.source = EffortSource::Cli;
        config.reviewer_agent_type_source = RouteValueSource::Cli;
        config.reviewer_model_source = RouteValueSource::Cli;
        config.reviewer_effort_level.source = EffortSource::Cli;
    }

    #[test]
    fn repeated_cli_values_match_the_project_loaded_effective_identity() {
        let project = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let mut cli = project.clone();
        mark_worker_and_reviewer_sources_as_cli(&mut cli);

        assert_eq!(
            project.routing_configuration_fingerprint(),
            cli.routing_configuration_fingerprint(),
            "provenance is evidence, not identity"
        );
        assert!(project
            .routing_configuration_fingerprint()
            .starts_with("sha256:v2:"));

        let project_worker = project
            .resolve_role_route("worker", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        let cli_worker = cli
            .resolve_role_route("worker", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        assert_eq!(
            project_worker.configuration_fingerprint,
            cli_worker.configuration_fingerprint
        );
        // The route receipt keeps its source-aware v1 binding as audit evidence.
        assert_ne!(
            project_worker.route_fingerprint,
            cli_worker.route_fingerprint
        );
        assert!(project_worker.route_fingerprint.starts_with("sha256:v1:"));
    }

    #[test]
    fn every_effective_role_value_changes_the_identity() {
        let baseline = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let baseline_identity = baseline.routing_configuration_fingerprint();

        let changes: Vec<(&str, Box<dyn Fn(&mut Config)>)> = vec![
            (
                "tl harness",
                Box::new(|c| c.root_agent_type = AgentType::Codex),
            ),
            ("tl model", Box::new(|c| c.model = Some("other-tl".into()))),
            (
                "tl effort",
                Box::new(|c| c.tl_effort_level = ResolvedEffort::from_cli(EffortLevel::Max)),
            ),
            (
                "worker harness",
                Box::new(|c| c.spawn_agent_type = AgentType::Codex),
            ),
            (
                "worker model",
                Box::new(|c| c.worker_model = Some("other-worker".into())),
            ),
            (
                "worker effort",
                Box::new(|c| c.worker_effort_level = ResolvedEffort::from_cli(EffortLevel::Low)),
            ),
            (
                "reviewer harness",
                Box::new(|c| c.reviewer.agent_type = AgentType::Claude),
            ),
            (
                "reviewer model",
                Box::new(|c| c.reviewer.model = Some("other-reviewer".into())),
            ),
            (
                "reviewer effort",
                Box::new(|c| c.reviewer_effort_level = ResolvedEffort::from_cli(EffortLevel::Low)),
            ),
        ];
        for (name, change) in changes {
            let mut changed = baseline.clone();
            change(&mut changed);
            assert_ne!(
                baseline_identity,
                changed.routing_configuration_fingerprint(),
                "changing the {name} must change the accepted identity"
            );
        }
    }

    #[test]
    fn provenance_records_sources_without_changing_the_identity() {
        let project = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let mut cli = project.clone();
        mark_worker_and_reviewer_sources_as_cli(&mut cli);

        let project_provenance = project.routing_configuration_provenance();
        let cli_provenance = cli.routing_configuration_provenance();
        assert_eq!(project_provenance.worker.harness, "project_config");
        assert_eq!(project_provenance.worker.model, "project_config");
        assert_eq!(project_provenance.worker.effort, "config");
        assert_eq!(cli_provenance.worker.harness, "cli");
        assert_eq!(cli_provenance.worker.model, "cli");
        assert_eq!(cli_provenance.worker.effort, "cli");
        assert_eq!(cli_provenance.reviewer.harness, "cli");
        assert_eq!(
            project.routing_configuration_fingerprint(),
            cli.routing_configuration_fingerprint()
        );
    }

    #[test]
    fn old_v1_configuration_values_are_never_the_current_identity() {
        let config = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let identity = config.routing_configuration_fingerprint();
        assert!(identity.starts_with("sha256:v2:"));
        assert!(!identity.starts_with("sha256:v1:"));
        assert_ne!(
            identity,
            "sha256:v1:0381301c6d4d364fd4a3de0a062c65d5fcda0f850d4d7013bf4422f63bd345a4"
        );
        assert_ne!(
            identity,
            "sha256:v1:73ef11c4b8f80a2e599e13d7a045e6a819a868034a6fccd84e740d2a2715e041"
        );
    }

    #[test]
    fn worker_and_reviewer_aliases_resolve_the_same_role_values() {
        let config = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let worker = config
            .resolve_role_route("worker", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        for alias in ["dev", "dev-leaf", "dev_leaf", "leaf", "teammate"] {
            let alias_route = config
                .resolve_role_route(alias, None, &RoleRouteOverrides::default(), None)
                .unwrap();
            assert_eq!(alias_route.role, CanonicalRole::Worker);
            assert_eq!(alias_route.agent_type, worker.agent_type);
            assert_eq!(alias_route.model, worker.model);
            assert_eq!(alias_route.effort.level, worker.effort.level);
            assert_eq!(
                alias_route.configuration_fingerprint,
                worker.configuration_fingerprint
            );
        }
        let reviewer = config
            .resolve_role_route("reviewer", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        assert_eq!(reviewer.role, CanonicalRole::Reviewer);
        assert_eq!(reviewer.model.as_deref(), Some("opencode-go/glm-5.3-flash"));
    }

    #[test]
    fn changing_one_role_does_not_leak_into_another_roles_resolved_route() {
        let baseline = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let mut changed = baseline.clone();
        changed.worker_model = Some("other-worker".into());
        let baseline_tl = baseline
            .resolve_role_route("tl", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        let changed_tl = changed
            .resolve_role_route("tl", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        let baseline_reviewer = baseline
            .resolve_role_route("reviewer", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        let changed_reviewer = changed
            .resolve_role_route("reviewer", None, &RoleRouteOverrides::default(), None)
            .unwrap();
        assert_eq!(baseline_tl.agent_type, changed_tl.agent_type);
        assert_eq!(baseline_tl.model, changed_tl.model);
        assert_eq!(baseline_reviewer.agent_type, changed_reviewer.agent_type);
        assert_eq!(baseline_reviewer.model, changed_reviewer.model);
        assert_ne!(
            baseline.routing_configuration_fingerprint(),
            changed.routing_configuration_fingerprint()
        );
    }

    #[test]
    fn all_three_runtime_adapters_have_a_distinct_stable_identity() {
        let mut seen = std::collections::HashSet::new();
        for harness in ["claude", "codex", "opencode"] {
            let contents = format!(
                "root_agent_type = '{harness}'\nmodel = 'root-{harness}'\nspawn_agent_type = '{harness}'\nworker_model = 'worker-{harness}'\n[reviewer]\nagent_type = '{harness}'\nmodel = 'reviewer-{harness}'\n"
            );
            let first = discover_config(&contents, &[]);
            let second = discover_config(&contents, &[]);
            assert_eq!(
                first.routing_configuration_fingerprint(),
                second.routing_configuration_fingerprint()
            );
            assert!(seen.insert(first.routing_configuration_fingerprint()));
        }
    }

    #[test]
    fn local_config_and_environment_precedence_are_reflected_in_the_identity() {
        let project = discover_config(LIVE_PROJECT_CONFIG, &[]);
        let environment = discover_config(
            LIVE_PROJECT_CONFIG,
            &[(WORKER_MODEL_ENV, "env-worker"), (WORKER_EFFORT_ENV, "max")],
        );
        assert_ne!(
            project.routing_configuration_fingerprint(),
            environment.routing_configuration_fingerprint()
        );
        assert_eq!(
            environment.routing_configuration_provenance().worker.model,
            "environment"
        );
        assert_eq!(
            environment.routing_configuration_provenance().worker.effort,
            "environment"
        );
    }

    #[test]
    fn server_environment_round_trip_preserves_route_provenance_and_fingerprint() {
        let project = tempfile::tempdir().unwrap();
        let exo = project.path().join(".exo");
        std::fs::create_dir_all(&exo).unwrap();
        std::fs::write(
            exo.join("config.toml"),
            "root_agent_type = 'claude'\nmodel = 'claude-tl'\ntl_effort_level = 'high'\nspawn_agent_type = 'codex'\nworker_model = 'gpt-worker'\nworker_effort_level = 'xhigh'\n[reviewer]\nagent_type = 'opencode'\nmodel = 'opencode/review'\neffort_level = 'low'\n",
        )
        .unwrap();
        let original = Config::discover_from(project.path(), &|_| None).unwrap();
        let environment: HashMap<String, String> = [
            ("EXOMONAD_ROOT_AGENT_TYPE", "claude"),
            (ROOT_AGENT_SOURCE_ENV, "project_config"),
            (TL_MODEL_ENV, "claude-tl"),
            (TL_MODEL_SOURCE_ENV, "project_config"),
            (TL_EFFORT_ENV, "high"),
            (TL_EFFORT_SOURCE_ENV, "config"),
            ("EXOMONAD_SPAWN_AGENT_TYPE", "codex"),
            (WORKER_AGENT_SOURCE_ENV, "project_config"),
            (WORKER_MODEL_ENV, "gpt-worker"),
            (WORKER_MODEL_SOURCE_ENV, "project_config"),
            (WORKER_EFFORT_ENV, "xhigh"),
            (WORKER_EFFORT_SOURCE_ENV, "config"),
            ("EXOMONAD_REVIEWER_AGENT_TYPE", "opencode"),
            (REVIEWER_AGENT_SOURCE_ENV, "project_config"),
            (REVIEWER_MODEL_ENV, "opencode/review"),
            (REVIEWER_MODEL_SOURCE_ENV, "project_config"),
            (REVIEWER_EFFORT_ENV, "low"),
            (REVIEWER_EFFORT_SOURCE_ENV, "config"),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
        let reloaded =
            Config::discover_from(project.path(), &|key| environment.get(key).cloned()).unwrap();

        assert_eq!(
            original.routing_configuration_fingerprint(),
            reloaded.routing_configuration_fingerprint()
        );
        for role in ["subtl", "dev-leaf", "reviewer"] {
            assert_eq!(
                original
                    .resolve_role_route(role, None, &RoleRouteOverrides::default(), None)
                    .unwrap(),
                reloaded
                    .resolve_role_route(role, None, &RoleRouteOverrides::default(), None)
                    .unwrap()
            );
        }
    }

    #[test]
    fn invalid_route_environment_is_not_silently_ignored() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(temp.path().join(".exo")).unwrap();
        let error = Config::discover_from(temp.path(), &|name| {
            (name == "EXOMONAD_SPAWN_AGENT_TYPE").then(|| "unknown-provider".to_string())
        })
        .unwrap_err();
        assert!(error.to_string().contains("EXOMONAD_SPAWN_AGENT_TYPE"));
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            project_dir: PathBuf::from("."),
            role: Role::tl(),
            tmux_session: "default".to_string(),
            port: 7433,
            worktree_base: PathBuf::from(".exo/worktrees"),
            shell_command: None,
            wasm_dir: PathBuf::from(".exo/wasm"),
            root_agent_type: AgentType::Claude,
            root_agent_type_source: RouteValueSource::Default,
            tl_effort_level: ResolvedEffort::MEDIUM_DEFAULT,
            spawn_agent_type: AgentType::default(),
            spawn_agent_type_source: RouteValueSource::Default,
            worker_effort_level: ResolvedEffort::MEDIUM_DEFAULT,
            worker_model: None,
            worker_model_source: RouteValueSource::Unresolved,
            reviewer_effort_level: ResolvedEffort::MEDIUM_DEFAULT,
            flake_ref: None,
            wasm_name: "devswarm".to_string(),
            extra_mcp_servers: std::collections::HashMap::new(),
            initial_prompt: None,
            yolo: false,
            companions: Vec::new(),
            root_command: None,
            otlp_endpoint: None,
            model: None,
            model_source: RouteValueSource::Unresolved,
            poll_interval: None,
            inbox_poke_interval: None,
            orphan_reconciler_interval_secs: None,
            tl_transport_timeout_seconds: DEFAULT_TL_TRANSPORT_TIMEOUT_SECONDS,
            tl_active_tail_timeout_seconds: DEFAULT_TL_ACTIVE_TAIL_TIMEOUT_SECONDS,
            tl_task_timeout_seconds: DEFAULT_TL_TASK_TIMEOUT_SECONDS,
            tl_dispatch_retry_limit: DEFAULT_TL_DISPATCH_RETRY_LIMIT,
            tl_dispatch_retry_base_delay_seconds: DEFAULT_TL_DISPATCH_RETRY_BASE_DELAY_SECONDS,
            tl_dispatch_retry_max_delay_seconds: DEFAULT_TL_DISPATCH_RETRY_MAX_DELAY_SECONDS,
            tl_preflight_runtime_paths: Vec::new(),
            openrouter: OpenRouterConfig::default(),
            opencode: OpencodeConfig::default(),
            opencode_tl_model_source: RouteValueSource::Unresolved,
            opencode_worker_model_source: RouteValueSource::Unresolved,
            opencode_as_tl: false,
            forgejo_url: None,
            forgejo_token: None,
            forgejo_reviewer_token: None,
            forgejo_webhook_secret: None,
            forgejo_ssh_port: None,
            reviewer: ReviewerConfig::default(),
            reviewer_agent_type_source: RouteValueSource::Default,
            reviewer_model_source: RouteValueSource::Unresolved,
        }
    }
}

/// Walk up from CWD to find the project root containing `.exo/`.
/// Looks for `.exo/config.toml` first, then `.exo/` directory.
/// Falls back to CWD if neither found (bootstrap case).
fn find_project_root() -> Result<PathBuf> {
    let start = std::env::current_dir()?;
    let mut current = start.as_path();
    loop {
        if current.join(".exo/config.toml").exists() || current.join(".exo").is_dir() {
            return Ok(current.to_path_buf());
        }
        match current.parent() {
            Some(parent) => current = parent,
            None => {
                debug!("No .exo/ found, using CWD as project root");
                return Ok(start);
            }
        }
    }
}

/// Auto-detect role name from `.exo/roles/`. If exactly one role dir exists, use it.
fn detect_role_name(project_dir: &Path) -> Option<String> {
    let roles_dir = project_dir.join(".exo/roles");
    let entries: Vec<_> = std::fs::read_dir(&roles_dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .collect();
    if entries.len() == 1 {
        let name = entries[0].file_name().to_string_lossy().to_string();
        debug!(role = %name, "Auto-detected role from .exo/roles/");
        Some(name)
    } else {
        None
    }
}

/// A tmux session name ExoMonad refuses to use.
///
/// Reported instead of a shortened name so the configured value, its length,
/// the limit, and the place that has to change all appear in one error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionNameTooLong {
    /// The value exactly as configured.
    pub configured: String,
    /// Which input supplied the value.
    pub source: SessionNameSource,
    /// The file or directory the value was read from.
    pub origin: PathBuf,
}

impl SessionNameTooLong {
    /// Length of the configured value, in characters.
    pub fn length(&self) -> usize {
        self.configured.chars().count()
    }

    /// The limit the value exceeded, in characters.
    pub fn limit(&self) -> usize {
        TMUX_SESSION_MAX_CHARS
    }
}

impl std::fmt::Display for SessionNameTooLong {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let origin = match self.source {
            SessionNameSource::ProjectDirectory => {
                format!(
                    "derived from the project directory name in {}",
                    self.origin.display()
                )
            }
            SessionNameSource::CommandLine => {
                format!("passed on the command line as {}", self.origin.display())
            }
            _ => format!("configured in {}", self.origin.display()),
        };
        write!(
            f,
            "tmux_session {:?} is {} characters, longer than the {}-character tmux session \
             name limit ({}); shorten it. A longer name is rejected here rather than truncated, \
             because truncation makes the server probe a different session than the configured \
             name and reports every agent as dead",
            self.configured,
            self.length(),
            self.limit(),
            origin
        )
    }
}

impl std::error::Error for SessionNameTooLong {}

/// The tmux session name ExoMonad resolved, and the configured value behind it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedSessionName {
    /// The name every consumer must use, after the documented `.` -> `_` rewrite.
    pub name: String,
    /// The value exactly as configured.
    pub configured: String,
    /// Which input supplied the value.
    pub source: SessionNameSource,
    /// The file or directory the value was read from.
    pub origin: PathBuf,
}

/// Validate one configured session name and apply the documented `.` -> `_`
/// rewrite (tmux forbids dots in session names).
///
/// The rewrite preserves length, so only the length is checked. Dots are
/// rewritten rather than rejected because the project directory name is the
/// fallback source and a directory may carry one; the rewrite is logged, so it
/// is never a silent transformation.
pub fn validate_session_name(
    configured: String,
    source: SessionNameSource,
    origin: PathBuf,
) -> Result<ResolvedSessionName> {
    if configured.chars().count() > TMUX_SESSION_MAX_CHARS {
        return Err(anyhow::Error::new(SessionNameTooLong {
            configured,
            source,
            origin,
        }));
    }

    let name = configured.replace('.', "_");
    if name != configured {
        warn!(
            configured = %configured,
            session = %name,
            origin = %origin.display(),
            "Rewrote '.' to '_' in tmux_session; tmux forbids dots in session names"
        );
    }

    Ok(ResolvedSessionName {
        name,
        configured,
        source,
        origin,
    })
}

/// Resolve the tmux session name: `config.local.toml` > `config.toml` > the
/// project directory's own name. Fails closed on a name over the limit.
fn resolve_tmux_session(
    local: Option<String>,
    project: Option<String>,
    project_root: &Path,
) -> Result<ResolvedSessionName> {
    match (local, project) {
        (Some(name), _) => validate_session_name(
            name,
            SessionNameSource::LocalConfig,
            project_root.join(LOCAL_CONFIG_PATH),
        ),
        (None, Some(name)) => validate_session_name(
            name,
            SessionNameSource::ProjectConfig,
            project_root.join(PROJECT_CONFIG_PATH),
        ),
        (None, None) => {
            let name = project_root
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("exomonad")
                .to_string();
            validate_session_name(
                name,
                SessionNameSource::ProjectDirectory,
                project_root.to_path_buf(),
            )
        }
    }
}

/// Provenance of one role's effective harness/model/effort. Evidence only:
/// which source supplied each value never participates in the configuration
/// identity, so an explicit CLI flag that repeats the project value is the
/// same accepted configuration.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RoleRouteSourceSummary {
    pub harness: String,
    pub model: String,
    pub effort: String,
}

/// Safe, auditable provenance for the three role-routing identities.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RoleRoutingProvenance {
    pub tl: RoleRouteSourceSummary,
    pub worker: RoleRouteSourceSummary,
    pub reviewer: RoleRouteSourceSummary,
}

impl Config {
    /// True if any configured role — root, spawned workers/leaves, the
    /// reviewer, or a companion — resolves to the Codex harness. Gates the
    /// Codex sandbox capability preflight: a Claude/OpenCode-only project
    /// has no reason to run it.
    pub fn uses_codex_anywhere(&self) -> bool {
        self.root_agent_type == AgentType::Codex
            || self.spawn_agent_type == AgentType::Codex
            || self.reviewer.agent_type == AgentType::Codex
            || self
                .companions
                .iter()
                .any(|companion| companion.agent_type == Some(AgentType::Codex))
    }

    /// Canonical identity of the effective role-routing configuration.
    ///
    /// The identity hashes only each role's effective harness, model, and
    /// effort in deterministic role order. Provenance (which config layer or
    /// CLI flag supplied a value) is recorded separately by
    /// [`Config::routing_configuration_provenance`]: it is audit evidence, not
    /// part of the accepted identity. This lets `serve` loaded from project
    /// configuration and `init` repeating the same values as explicit flags
    /// agree on one identity while a genuinely different value still changes
    /// it.
    ///
    /// The version tag is deliberate: `sha256:v1:` values are historical
    /// receipts and are never reinterpreted as this identity.
    pub fn routing_configuration_fingerprint(&self) -> String {
        let (tl_agent, _, tl_model, _, tl_effort) = self.configured_route(CanonicalRole::Tl);
        let (worker_agent, _, worker_model, _, worker_effort) =
            self.configured_route(CanonicalRole::Worker);
        let (reviewer_agent, _, reviewer_model, _, reviewer_effort) =
            self.configured_route(CanonicalRole::Reviewer);
        digest_effective_role_routes(
            [
                ("tl", tl_agent, tl_model, tl_effort.level.to_string()),
                (
                    "worker",
                    worker_agent,
                    worker_model,
                    worker_effort.level.to_string(),
                ),
                (
                    "reviewer",
                    reviewer_agent,
                    reviewer_model,
                    reviewer_effort.level.to_string(),
                ),
            ]
            .into_iter()
            .flat_map(|(role, agent, model, effort)| {
                [
                    role.to_string(),
                    agent_type_name(agent).to_string(),
                    model_hash_value(model.as_deref()),
                    effort,
                ]
            }),
        )
    }

    /// Provenance of each effective route value, with no secrets, raw
    /// environment values, or model credentials. This is safe to log and to
    /// expose on the server health surface.
    pub fn routing_configuration_provenance(&self) -> RoleRoutingProvenance {
        let source = |harness: RouteValueSource, model: RouteValueSource, effort: &str| {
            RoleRouteSourceSummary {
                harness: route_source_name(harness).to_string(),
                model: route_source_name(model).to_string(),
                effort: effort.to_string(),
            }
        };
        let (_, tl_model_source) = configured_model_for_harness(
            self.model.clone(),
            self.model_source,
            self.opencode.tl_model.clone(),
            self.opencode_tl_model_source,
            self.root_agent_type,
        );
        let (_, worker_model_source) = configured_model_for_harness(
            self.worker_model.clone(),
            self.worker_model_source,
            self.opencode.worker_model.clone(),
            self.opencode_worker_model_source,
            self.spawn_agent_type,
        );
        RoleRoutingProvenance {
            tl: source(
                self.root_agent_type_source,
                tl_model_source,
                &self.tl_effort_level.source.to_string(),
            ),
            worker: source(
                self.spawn_agent_type_source,
                worker_model_source,
                &self.worker_effort_level.source.to_string(),
            ),
            reviewer: source(
                self.reviewer_agent_type_source,
                self.reviewer_model_source,
                &self.reviewer_effort_level.source.to_string(),
            ),
        }
    }

    /// Resolve one runtime role using CLI overrides, an accepted task-policy
    /// route, role configuration, and finally an explicitly permitted harness
    /// default. A policy-selected model always wins over host-wide defaults.
    pub fn resolve_role_route(
        &self,
        role: &str,
        policy: Option<&PolicyRouteSelection>,
        overrides: &RoleRouteOverrides,
        inherited_model: Option<String>,
    ) -> Result<ResolvedRoleRoute> {
        let role = CanonicalRole::parse(role)?;
        validate_optional_model("CLI model override", overrides.model.as_deref())?;
        if let Some(policy) = policy {
            validate_optional_model("policy model", policy.model.as_deref())?;
        }
        let (base_agent, base_agent_source, base_model, base_model_source, base_effort) =
            self.configured_route(role);
        let agent_type = overrides
            .agent_type
            .or_else(|| policy.map(|route| route.agent_type))
            .unwrap_or(base_agent);
        let agent_type_source = if overrides.agent_type.is_some() {
            RouteValueSource::Cli
        } else if policy.is_some() {
            RouteValueSource::Policy
        } else {
            base_agent_source
        };
        if let (Some(policy), Some(cli_harness)) = (policy, overrides.agent_type) {
            if cli_harness != policy.agent_type
                && policy.model.is_some()
                && overrides.model.is_none()
            {
                anyhow::bail!(
                    "CLI harness `{}` conflicts with the accepted policy model route for `{}`; supply a matching --model override or resolve the policy conflict",
                    agent_type_name(cli_harness), role.as_str()
                );
            }
        }

        let (model, model_source) = if let Some(model) = present_value(overrides.model.clone()) {
            (Some(model), RouteValueSource::Cli)
        } else if let Some(model) = policy.and_then(|route| present_value(route.model.clone())) {
            (Some(model), RouteValueSource::Policy)
        } else if agent_type == base_agent {
            if let Some(model) = base_model {
                (Some(model), base_model_source)
            } else if let Some(model) = present_value(inherited_model) {
                (Some(model), RouteValueSource::HarnessDefault)
            } else {
                (None, RouteValueSource::Unresolved)
            }
        } else if let Some(model) = present_value(inherited_model) {
            (Some(model), RouteValueSource::HarnessDefault)
        } else {
            (None, RouteValueSource::Unresolved)
        };
        let effort = if let Some(level) = overrides.effort {
            ResolvedEffort::from_cli(level)
        } else if let Some(level) = policy.and_then(|route| route.effort) {
            ResolvedEffort {
                level,
                source: EffortSource::Policy,
            }
        } else {
            base_effort
        };
        let configuration_fingerprint = self.routing_configuration_fingerprint();
        let mut route = ResolvedRoleRoute {
            role,
            agent_type,
            model,
            effort,
            agent_type_source,
            model_source,
            configuration_fingerprint,
            route_fingerprint: String::new(),
        };
        route.route_fingerprint = digest_route_fields([
            route.role.as_str().to_string(),
            agent_type_name(route.agent_type).to_string(),
            model_hash_value(route.model.as_deref()),
            route.effort.level.to_string(),
            route.effort.source.to_string(),
            route_source_name(route.agent_type_source).to_string(),
            route_source_name(route.model_source).to_string(),
            route.configuration_fingerprint.clone(),
        ]);
        Ok(route)
    }

    fn configured_route(
        &self,
        role: CanonicalRole,
    ) -> (
        AgentType,
        RouteValueSource,
        Option<String>,
        RouteValueSource,
        ResolvedEffort,
    ) {
        match role {
            CanonicalRole::Tl => {
                let (model, source) = configured_model_for_harness(
                    self.model.clone(),
                    self.model_source,
                    self.opencode.tl_model.clone(),
                    self.opencode_tl_model_source,
                    self.root_agent_type,
                );
                (
                    self.root_agent_type,
                    self.root_agent_type_source,
                    model,
                    source,
                    self.tl_effort_level,
                )
            }
            CanonicalRole::Worker => {
                let (model, source) = configured_model_for_harness(
                    self.worker_model.clone(),
                    self.worker_model_source,
                    self.opencode.worker_model.clone(),
                    self.opencode_worker_model_source,
                    self.spawn_agent_type,
                );
                (
                    self.spawn_agent_type,
                    self.spawn_agent_type_source,
                    model,
                    source,
                    self.worker_effort_level,
                )
            }
            CanonicalRole::Reviewer => (
                self.reviewer.agent_type,
                self.reviewer_agent_type_source,
                self.reviewer.model.clone(),
                self.reviewer_model_source,
                self.reviewer_effort_level,
            ),
        }
    }
}

fn configured_model_for_harness(
    model: Option<String>,
    model_source: RouteValueSource,
    legacy_model: Option<String>,
    legacy_source: RouteValueSource,
    agent_type: AgentType,
) -> (Option<String>, RouteValueSource) {
    if let Some(model) = present_value(model) {
        return (Some(model), model_source);
    }
    if agent_type == AgentType::OpenCode {
        if let Some(model) = present_value(legacy_model) {
            return (Some(model), legacy_source);
        }
    }
    (None, RouteValueSource::Unresolved)
}

fn digest_route_fields(fields: impl IntoIterator<Item = String>) -> String {
    let mut digest = Sha256::new();
    digest.update(b"exomonad-role-route-v1");
    for field in fields {
        let bytes = field.as_bytes();
        digest.update((bytes.len() as u64).to_be_bytes());
        digest.update(bytes);
    }
    format!("sha256:v1:{:x}", digest.finalize())
}

/// Versioned identity of effective role values. `v1` route and policy
/// receipts keep their own versioned domain; this identity is deliberately
/// separate so an old hash can never be mistaken for a current acceptance.
fn digest_effective_role_routes(fields: impl IntoIterator<Item = String>) -> String {
    let mut digest = Sha256::new();
    digest.update(b"exomonad-role-route-identity-v2");
    for field in fields {
        let bytes = field.as_bytes();
        digest.update((bytes.len() as u64).to_be_bytes());
        digest.update(bytes);
    }
    format!("sha256:v2:{:x}", digest.finalize())
}

fn model_hash_value(model: Option<&str>) -> String {
    model.map_or_else(|| "absent".to_string(), |value| format!("present:{value}"))
}

fn validate_optional_model(source: &str, model: Option<&str>) -> Result<()> {
    if model.is_some_and(|value| value.trim().is_empty()) {
        anyhow::bail!("{source} must be omitted or contain a non-empty model identifier");
    }
    Ok(())
}

fn route_source_name(source: RouteValueSource) -> &'static str {
    match source {
        RouteValueSource::Cli => "cli",
        RouteValueSource::Policy => "policy",
        RouteValueSource::Environment => "environment",
        RouteValueSource::LocalConfig => "local_config",
        RouteValueSource::ProjectConfig => "project_config",
        RouteValueSource::Default => "default",
        RouteValueSource::HarnessDefault => "harness_default",
        RouteValueSource::Unresolved => "unresolved",
    }
}

fn agent_type_name(agent_type: AgentType) -> &'static str {
    match agent_type {
        AgentType::Claude => "claude",
        AgentType::Codex => "codex",
        AgentType::OpenCode => "opencode",
        AgentType::Shoal => "shoal",
        AgentType::Process => "process",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_core::services::agent_control::AGENT_TYPE_DEPRECATION_MESSAGE;

    #[test]
    fn test_raw_config_parse_local() {
        let content = r#"
            role = "dev"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.role, Some(Role::dev()));
    }

    #[test]
    fn test_parse_positive_u32_rejects_zero_and_invalid_values() {
        assert_eq!(parse_positive_u32("1"), Ok(1));
        assert_eq!(parse_positive_u32("42"), Ok(42));
        assert!(parse_positive_u32("0").is_err());
        assert!(parse_positive_u32("not-a-number").is_err());
        assert!(parse_positive_u32("4294967296").is_err());
    }

    #[test]
    fn test_require_positive_timeout_rejects_nan_and_nonpositive() {
        assert!(require_positive_timeout("t", 1.0).is_ok());
        assert!(require_positive_timeout("t", f64::INFINITY).is_ok());
        assert!(require_positive_timeout("t", f64::NAN).is_err());
        assert!(require_positive_timeout("t", 0.0).is_err());
        assert!(require_positive_timeout("t", -1.0).is_err());
    }

    #[test]
    fn test_raw_config_parse_global() {
        let content = r#"
            project_dir = "/my/project"
            default_role = "tl"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.project_dir, Some(PathBuf::from("/my/project")));
        assert_eq!(raw.default_role, Some(Role::tl()));
    }

    #[test]
    fn test_raw_config_parse_forgejo_reviewer_token() {
        let content = r#"
            forgejo_url = "http://localhost:3000"
            forgejo_token = "author-token"
            forgejo_reviewer_token = "reviewer-token"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.forgejo_token.as_deref(), Some("author-token"));
        assert_eq!(
            raw.forgejo_reviewer_token.as_deref(),
            Some("reviewer-token")
        );
    }

    #[test]
    fn test_raw_config_empty() {
        let raw: RawConfig = toml::from_str("").unwrap();
        assert!(raw.role.is_none());
        assert!(raw.default_role.is_none());
        assert!(raw.project_dir.is_none());
    }

    #[test]
    fn test_config_default() {
        let config = Config::default();
        assert_eq!(config.project_dir, PathBuf::from("."));
        assert_eq!(config.role, Role::tl());
        assert_eq!(config.root_agent_type, AgentType::Claude);
        assert_eq!(config.spawn_agent_type, AgentType::Codex);
        assert_eq!(config.port, 7433);
        assert_eq!(
            config.tl_transport_timeout_seconds,
            DEFAULT_TL_TRANSPORT_TIMEOUT_SECONDS
        );
        assert_eq!(
            config.tl_active_tail_timeout_seconds,
            DEFAULT_TL_ACTIVE_TAIL_TIMEOUT_SECONDS
        );
        assert_eq!(
            config.tl_task_timeout_seconds,
            DEFAULT_TL_TASK_TIMEOUT_SECONDS
        );
    }

    #[test]
    fn test_spawn_agent_type_defaults_to_codex_when_config_omits_field() {
        assert_eq!(
            resolve_agent_type_value(
                None,
                None,
                None,
                RouteValueSource::LocalConfig,
                None,
                RouteValueSource::ProjectConfig,
                AgentType::Codex,
                "EXOMONAD_SPAWN_AGENT_TYPE",
            )
            .unwrap(),
            (AgentType::Codex, RouteValueSource::Default)
        );
    }

    #[test]
    fn test_raw_config_parse_port() {
        let content = r#"
            port = 9001
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.port, Some(9001));
    }

    #[test]
    fn test_raw_config_parse_inbox_poke_interval() {
        let content = r#"
            inbox_poke_interval = 120
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.inbox_poke_interval, Some(120));
    }

    #[test]
    fn test_raw_config_parse_tl_timeouts() {
        let content = r#"
            tl_transport_timeout_seconds = 45.5
            tl_active_tail_timeout_seconds = 60.0
            tl_task_timeout_seconds = 90.0
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.tl_transport_timeout_seconds, Some(45.5));
        assert_eq!(raw.tl_active_tail_timeout_seconds, Some(60.0));
        assert_eq!(raw.tl_task_timeout_seconds, Some(90.0));
    }

    #[test]
    fn test_raw_config_parse_tl_dispatch_retry_budget() {
        let content = r#"
            tl_dispatch_retry_limit = 7
            tl_dispatch_retry_base_delay_seconds = 2.5
            tl_dispatch_retry_max_delay_seconds = 30.0
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.tl_dispatch_retry_limit, Some(7));
        assert_eq!(raw.tl_dispatch_retry_base_delay_seconds, Some(2.5));
        assert_eq!(raw.tl_dispatch_retry_max_delay_seconds, Some(30.0));
    }

    #[test]
    fn test_dispatch_retry_defaults_when_unset() {
        let config = Config::default();
        assert_eq!(config.tl_dispatch_retry_limit, 3);
        assert_eq!(config.tl_dispatch_retry_base_delay_seconds, 5.0);
        assert_eq!(config.tl_dispatch_retry_max_delay_seconds, 60.0);
    }

    #[test]
    fn test_dispatch_retry_limit_must_be_positive() {
        let error = require_dispatch_retry_limit("tl_dispatch_retry_limit", 0)
            .expect_err("a zero budget would park every retryable refusal immediately");
        assert!(error.to_string().contains("at least 1"), "{error}");
    }

    #[test]
    fn test_dispatch_retry_delays_must_be_positive_and_ordered() {
        for (base, max) in [(0.0, 60.0), (-1.0, 60.0), (5.0, 0.0), (5.0, -1.0)] {
            require_dispatch_retry_delays("base", base, "max", max)
                .expect_err("a non-positive delay must be refused");
        }
        require_dispatch_retry_delays("base", 5.0, "max", 60.0).expect("ordered pair");
        require_dispatch_retry_delays("base", 5.0, "max", 5.0).expect("equal pair");
        let error = require_dispatch_retry_delays("base", 30.0, "max", 5.0)
            .expect_err("a cap below the base would schedule a retry before the first one");
        assert!(
            error.to_string().contains("greater than or equal to"),
            "{error}"
        );
    }

    #[test]
    fn test_raw_config_parse_tl_preflight_runtime_paths() {
        let content = r#"
              tl_preflight_runtime_paths = [".cache/tool/", "runtime/state"]
          "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(
            raw.tl_preflight_runtime_paths,
            Some(vec![
                ".cache/tool/".to_string(),
                "runtime/state".to_string()
            ])
        );
        validate_preflight_runtime_paths(raw.tl_preflight_runtime_paths.as_ref().unwrap()).unwrap();
    }

    #[test]
    fn test_preflight_runtime_paths_reject_absolute_and_parent_paths() {
        assert!(validate_preflight_runtime_paths(&["/tmp/runtime".to_string()]).is_err());
        assert!(validate_preflight_runtime_paths(&["runtime/../source".to_string()]).is_err());
        assert!(validate_preflight_runtime_paths(&[" ".to_string()]).is_err());
    }

    #[test]
    fn test_raw_config_parse_root_agent_type() {
        let retired = ["ge", "mini"].concat();
        let content = format!("root_agent_type = \"{retired}\"");
        let error =
            toml::from_str::<RawConfig>(&content).expect_err("retired harness must fail closed");
        assert!(error.to_string().contains("agent_type '"));
    }

    #[test]
    fn test_session_name_at_the_limit_is_accepted_unchanged() {
        let name = "a".repeat(TMUX_SESSION_MAX_CHARS);
        let resolved = validate_session_name(
            name.clone(),
            SessionNameSource::ProjectConfig,
            PathBuf::from("/repo/.exo/config.toml"),
        )
        .expect("a name exactly at the limit is valid");
        assert_eq!(resolved.name, name);
        assert_eq!(resolved.configured, name);
    }

    #[test]
    fn test_session_name_over_the_limit_is_rejected_with_the_typed_error() {
        let name = "a".repeat(TMUX_SESSION_MAX_CHARS + 1);
        let origin = PathBuf::from("/repo/.exo/config.local.toml");
        let failure =
            validate_session_name(name.clone(), SessionNameSource::LocalConfig, origin.clone())
                .expect_err("a name over the limit must not be shortened");

        let error = failure
            .downcast_ref::<SessionNameTooLong>()
            .expect("config load must report the typed session-name error");
        assert_eq!(error.configured, name);
        assert_eq!(error.source, SessionNameSource::LocalConfig);
        assert_eq!(error.origin, origin);
        assert_eq!(error.length(), TMUX_SESSION_MAX_CHARS + 1);
        assert_eq!(error.limit(), TMUX_SESSION_MAX_CHARS);

        // The message names the value, its length, the limit, and the file.
        let message = error.to_string();
        assert!(
            message.contains(&name),
            "message names the value: {message}"
        );
        assert!(
            message.contains(&(TMUX_SESSION_MAX_CHARS + 1).to_string()),
            "message names the length: {message}"
        );
        assert!(
            message.contains(&TMUX_SESSION_MAX_CHARS.to_string()),
            "message names the limit: {message}"
        );
        assert!(
            message.contains("config.local.toml"),
            "message names the file: {message}"
        );
    }

    #[test]
    fn test_session_name_rewrite_dots_and_leaves_other_names_alone() {
        // tmux forbids dots in session names, so they are rewritten — and the
        // resolved name is reported, never a silent transformation.
        let resolved = validate_session_name(
            "my.project".to_string(),
            SessionNameSource::ProjectConfig,
            PathBuf::from("/repo/.exo/config.toml"),
        )
        .expect("dots are rewritten, not rejected");
        assert_eq!(resolved.name, "my_project");
        assert_eq!(resolved.configured, "my.project");

        let resolved = validate_session_name(
            "exomonad".to_string(),
            SessionNameSource::ProjectConfig,
            PathBuf::from("/repo/.exo/config.toml"),
        )
        .expect("a clean name is valid");
        assert_eq!(resolved.name, "exomonad");

        // The rewrite is length-preserving, so a dotted name is held to the
        // same limit as any other.
        let dotted = format!("{}.x", "a".repeat(TMUX_SESSION_MAX_CHARS - 2));
        assert!(validate_session_name(
            dotted,
            SessionNameSource::ProjectConfig,
            PathBuf::from("/repo/.exo/config.toml")
        )
        .is_ok());
    }

    #[test]
    fn test_resolve_tmux_session_prefers_local_then_project_then_directory() {
        let root = PathBuf::from("/repo");
        let local = resolve_tmux_session(
            Some("from-local".to_string()),
            Some("from-project".to_string()),
            &root,
        )
        .expect("local config wins");
        assert_eq!(local.name, "from-local");
        assert_eq!(local.source, SessionNameSource::LocalConfig);
        assert_eq!(local.origin, PathBuf::from("/repo/.exo/config.local.toml"));

        let project = resolve_tmux_session(None, Some("from-project".to_string()), &root)
            .expect("project config is the fallback");
        assert_eq!(project.name, "from-project");
        assert_eq!(project.source, SessionNameSource::ProjectConfig);
        assert_eq!(project.origin, PathBuf::from("/repo/.exo/config.toml"));

        let directory =
            resolve_tmux_session(None, None, &root).expect("directory name is the last resort");
        assert_eq!(directory.name, "repo");
        assert_eq!(directory.source, SessionNameSource::ProjectDirectory);
    }

    #[test]
    fn test_resolve_tmux_session_reports_the_file_that_set_an_over_long_name() {
        let root = PathBuf::from("/repo");
        let failure = resolve_tmux_session(None, Some("b".repeat(37)), &root)
            .expect_err("an over-long project-config name must fail closed");
        let error = failure
            .downcast_ref::<SessionNameTooLong>()
            .expect("typed error");
        assert_eq!(error.source, SessionNameSource::ProjectConfig);
        assert!(error.to_string().contains("config.toml"));
        assert!(!error.to_string().contains("config.local.toml"));
    }

    #[test]
    fn test_raw_config_parse_with_tmux_session() {
        let content = r#"
            default_role = "tl"
            tmux_session = "exomonad"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.tmux_session, Some("exomonad".to_string()));
    }

    #[test]
    fn test_extra_mcp_servers_http() {
        let content = r#"
            [extra_mcp_servers.metacog]
            type = "http"
            url = "http://localhost:8080"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert!(raw.extra_mcp_servers.contains_key("metacog"));
        match &raw.extra_mcp_servers["metacog"] {
            McpServerConfig::Http { url, headers } => {
                assert_eq!(url, "http://localhost:8080");
                assert!(headers.is_empty());
            }
            _ => panic!("Expected Http variant"),
        }
    }

    #[test]
    fn test_extra_mcp_servers_stdio() {
        let content = r#"
            [extra_mcp_servers.notebooklm]
            type = "stdio"
            command = "node"
            args = ["vendor/notebooklm-mcp/dist/index.js"]
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert!(raw.extra_mcp_servers.contains_key("notebooklm"));
        match &raw.extra_mcp_servers["notebooklm"] {
            McpServerConfig::Stdio { command, args } => {
                assert_eq!(command, "node");
                assert_eq!(args, &["vendor/notebooklm-mcp/dist/index.js"]);
            }
            _ => panic!("Expected Stdio variant"),
        }
    }

    #[test]
    fn test_raw_config_parse_companions_without_agent_type() {
        let content = r#"
            [[companions]]
            name = "sleeptime"
            command = "claude -c"
            task = "You are sleeptime"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.companions.len(), 1);
        assert_eq!(raw.companions[0].name, "sleeptime");
        assert_eq!(raw.companions[0].role, "worker");
        assert!(raw.companions[0].agent_type.is_none());
        assert_eq!(
            raw.companions[0].task,
            Some("You are sleeptime".to_string())
        );
    }

    #[test]
    fn test_raw_config_parse_companions_with_agent_type() {
        let retired = ["ge", "mini"].concat();
        let content = format!(
            r#"
            [[companions]]
            name = "sleeptime"
            agent_type = "claude"
            command = "claude -c"
            task = "You are sleeptime"

            [[companions]]
            name = "researcher"
            agent_type = "{retired}"
            command = "retired-cli"
            task = "Research task"
        "#
        );
        let error =
            toml::from_str::<RawConfig>(&content).expect_err("retired harness must fail closed");
        assert!(error.to_string().contains("agent_type '"));
    }

    #[test]
    fn test_raw_config_parse_model_field() {
        let content = r#"
            model = "sonnet"

            [[companions]]
            name = "test-runner"
            agent_type = "claude"
            command = "claude --dangerously-skip-permissions"
            model = "haiku"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.model, Some("sonnet".to_string()));
        assert_eq!(raw.companions[0].model, Some("haiku".to_string()));
    }

    #[test]
    fn test_raw_config_parse_companion_without_task() {
        let content = r#"
            [[companions]]
            name = "sleeptime"
            agent_type = "claude"
            command = "claude --dangerously-skip-permissions -c"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.companions.len(), 1);
        assert_eq!(raw.companions[0].name, "sleeptime");
        assert!(raw.companions[0].task.is_none());
    }

    #[test]
    fn test_raw_config_parse_opencode_section() {
        let content = r#"
            [opencode]
            tl_model = "anthropic/claude-sonnet-4-5"
            worker_model = "anthropic/claude-haiku-4-5"
            use_embedded_key = true
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        let oc = raw.opencode.expect("opencode section should parse");
        assert_eq!(oc.tl_model, Some("anthropic/claude-sonnet-4-5".to_string()));
        assert_eq!(
            oc.worker_model,
            Some("anthropic/claude-haiku-4-5".to_string())
        );
        assert!(oc.use_embedded_key);
    }

    #[test]
    fn test_raw_config_parse_opencode_as_tl() {
        let content = r#"
            opencode_as_tl = true
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.opencode_as_tl, Some(true));
    }

    #[test]
    fn test_raw_config_parse_root_agent_type_opencode() {
        let content = r#"
            root_agent_type = "opencode"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.root_agent_type, Some(AgentType::OpenCode));
    }

    #[test]
    fn test_raw_config_parse_spawn_agent_type_opencode() {
        let content = r#"
            spawn_agent_type = "opencode"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.spawn_agent_type, Some(AgentType::OpenCode));
    }

    #[test]
    fn raw_config_parses_harness_independent_worker_model() {
        let raw: RawConfig = toml::from_str(
            "spawn_agent_type = 'claude'\nworker_model = 'claude-sonnet'\nworker_effort_level = 'high'\n",
        )
        .unwrap();
        assert_eq!(raw.worker_model.as_deref(), Some("claude-sonnet"));
        assert_eq!(raw.worker_effort_level, Some(EffortLevel::High));
    }

    #[test]
    fn test_spawn_agent_type_retired_fails_during_config_load() {
        let retired = ["ge", "mini"].concat();
        let temp_dir = tempfile::tempdir().unwrap();
        let path = temp_dir.path().join("config.toml");
        std::fs::write(&path, format!("spawn_agent_type = \"{retired}\"\n")).unwrap();

        let error = Config::load_raw(&path).expect_err("retired spawn harness must fail closed");
        let error_text = format!("{error:#}");
        assert!(error_text.contains(AGENT_TYPE_DEPRECATION_MESSAGE));
    }

    // ── Reviewer config tests ──────────────────────────────────────────────

    #[test]
    fn test_reviewer_config_absent_means_none() {
        let raw: RawConfig = toml::from_str("default_role = \"tl\"").unwrap();
        assert!(raw.reviewer.is_none());
    }

    #[test]
    fn test_reviewer_config_empty_section_uses_defaults() {
        let raw: RawConfig = toml::from_str("[reviewer]").unwrap();
        let rc = raw.reviewer.unwrap();
        assert_eq!(rc.agent_type, AgentType::Claude);
        assert!(rc.model.is_none());
        assert!(rc.context.is_empty());
    }

    #[test]
    fn test_reviewer_config_claude_with_model() {
        let content = r#"
[reviewer]
agent_type = "claude"
model = "claude-haiku-4-5-20251001"
"#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        let rc = raw.reviewer.unwrap();
        assert_eq!(rc.agent_type, AgentType::Claude);
        assert_eq!(rc.model.as_deref(), Some("claude-haiku-4-5-20251001"));
    }

    #[test]
    fn test_reviewer_config_opencode_with_model_and_context() {
        let content = r#"
[reviewer]
agent_type = "opencode"
model = "anthropic/claude-haiku-4-5"
context = ["CLAUDE.md", ".exo/rules/reviewer.md"]
"#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        let rc = raw.reviewer.unwrap();
        assert_eq!(rc.agent_type, AgentType::OpenCode);
        assert_eq!(rc.model.as_deref(), Some("anthropic/claude-haiku-4-5"));
        assert_eq!(rc.context, vec!["CLAUDE.md", ".exo/rules/reviewer.md"]);
    }

    #[test]
    fn test_reviewer_config_accepts_luna_codex_with_xhigh() {
        let content = r#"
[reviewer]
agent_type = "codex"
model = "gpt-5.6-luna"
effort_level = "xhigh"
"#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        let reviewer = raw.reviewer.unwrap();
        assert_eq!(reviewer.agent_type, AgentType::Codex);
        assert_eq!(reviewer.model.as_deref(), Some("gpt-5.6-luna"));
        assert_eq!(reviewer.effort_level, Some(EffortLevel::XHigh));
    }

    #[test]
    fn test_reviewer_config_invalid_agent_type_rejected() {
        let content = "[reviewer]\nagent_type = \"invalid\"\n";
        let result: Result<RawConfig, _> = toml::from_str(content);
        assert!(
            result.is_err(),
            "Unknown agent_type should fail to deserialize"
        );
    }

    #[test]
    fn uses_codex_anywhere_true_by_default_via_spawn_agent_type() {
        // AgentType's #[default] is Codex, and Config::default() uses it for
        // spawn_agent_type — the common case (workers/leaves/companions).
        let config = Config::default();
        assert!(config.uses_codex_anywhere());
    }

    #[test]
    fn uses_codex_anywhere_false_when_no_role_selects_codex() {
        let mut config = Config::default();
        config.root_agent_type = AgentType::Claude;
        config.spawn_agent_type = AgentType::OpenCode;
        config.reviewer.agent_type = AgentType::Claude;
        assert!(!config.uses_codex_anywhere());
    }

    #[test]
    fn uses_codex_anywhere_true_when_only_reviewer_is_codex() {
        let mut config = Config::default();
        config.root_agent_type = AgentType::Claude;
        config.spawn_agent_type = AgentType::OpenCode;
        config.reviewer.agent_type = AgentType::Codex;
        assert!(config.uses_codex_anywhere());
    }

    #[test]
    fn uses_codex_anywhere_true_when_only_a_companion_is_codex() {
        let mut config = Config::default();
        config.root_agent_type = AgentType::Claude;
        config.spawn_agent_type = AgentType::OpenCode;
        config.reviewer.agent_type = AgentType::Claude;
        config.companions.push(CompanionConfig {
            name: "sleeptime".to_string(),
            role: "sleeptime".to_string(),
            agent_type: Some(AgentType::Codex),
            command: "claude".to_string(),
            task: None,
            model: None,
        });
        assert!(config.uses_codex_anywhere());
    }

    #[test]
    fn uses_codex_anywhere_ignores_companion_with_no_agent_type() {
        let mut config = Config::default();
        config.root_agent_type = AgentType::Claude;
        config.spawn_agent_type = AgentType::OpenCode;
        config.reviewer.agent_type = AgentType::Claude;
        config.companions.push(CompanionConfig {
            name: "sleeptime".to_string(),
            role: "sleeptime".to_string(),
            agent_type: None,
            command: "claude".to_string(),
            task: None,
            model: None,
        });
        assert!(!config.uses_codex_anywhere());
    }

    #[test]
    fn test_reviewer_config_default_is_claude() {
        let config = Config::default();
        assert_eq!(config.reviewer.agent_type, AgentType::Claude);
        assert!(config.reviewer.model.is_none());
        assert!(config.reviewer.context.is_empty());
    }
}

/// Public effort levels accepted by supported harnesses.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, ValueEnum, Deserialize, Serialize)]
pub enum EffortLevel {
    #[value(name = "low")]
    #[serde(rename = "low")]
    Low,
    #[value(name = "medium")]
    #[serde(rename = "medium")]
    #[default]
    Medium,
    #[value(name = "high")]
    #[serde(rename = "high")]
    High,
    #[value(name = "xhigh")]
    #[serde(rename = "xhigh")]
    XHigh,
    #[value(name = "max")]
    #[serde(rename = "max")]
    Max,
}

impl std::fmt::Display for EffortLevel {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Low => "low",
            Self::Medium => "medium",
            Self::High => "high",
            Self::XHigh => "xhigh",
            Self::Max => "max",
        })
    }
}

/// Identifies whether a resolved effort value was explicit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum EffortSource {
    Cli,
    Environment,
    Policy,
    Config,
    Default,
}

impl std::fmt::Display for EffortSource {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Cli => "cli",
            Self::Environment => "environment",
            Self::Policy => "policy",
            Self::Config => "config",
            Self::Default => "default",
        })
    }
}

/// A role effort value together with its provenance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct ResolvedEffort {
    pub level: EffortLevel,
    pub source: EffortSource,
}

impl ResolvedEffort {
    pub const MEDIUM_DEFAULT: Self = Self {
        level: EffortLevel::Medium,
        source: EffortSource::Default,
    };

    pub fn from_cli(level: EffortLevel) -> Self {
        Self {
            level,
            source: EffortSource::Cli,
        }
    }
}

/// Resolve CLI over local config, global config, and the medium default.
pub fn resolve_effort_setting(
    cli: Option<EffortLevel>,
    local: Option<EffortLevel>,
    global: Option<EffortLevel>,
) -> ResolvedEffort {
    match (cli, local, global) {
        (Some(level), _, _) => ResolvedEffort {
            level,
            source: EffortSource::Cli,
        },
        (None, Some(level), _) | (None, None, Some(level)) => ResolvedEffort {
            level,
            source: EffortSource::Config,
        },
        (None, None, None) => ResolvedEffort::MEDIUM_DEFAULT,
    }
}

#[cfg(test)]
mod effort_tests {
    use super::*;
    use clap::ValueEnum;

    #[test]
    fn effort_levels_parse_display_and_serialize() {
        #[derive(Serialize)]
        struct EffortConfig {
            effort_level: EffortLevel,
        }

        for (level, name) in [
            (EffortLevel::Low, "low"),
            (EffortLevel::Medium, "medium"),
            (EffortLevel::High, "high"),
            (EffortLevel::XHigh, "xhigh"),
            (EffortLevel::Max, "max"),
        ] {
            assert_eq!(EffortLevel::from_str(name, false), Ok(level));
            assert_eq!(level.to_string(), name);
            let config = EffortConfig {
                effort_level: level,
            };
            assert_eq!(
                toml::to_string(&config).unwrap().trim(),
                format!("effort_level = \"{name}\"")
            );
        }
    }

    #[test]
    fn effort_resolution_preserves_precedence_and_source() {
        let cli = resolve_effort_setting(
            Some(EffortLevel::High),
            Some(EffortLevel::Low),
            Some(EffortLevel::Max),
        );
        assert_eq!(cli.level, EffortLevel::High);
        assert_eq!(cli.source, EffortSource::Cli);

        let xhigh = resolve_effort_setting(Some(EffortLevel::XHigh), Some(EffortLevel::High), None);
        assert_eq!(xhigh.level, EffortLevel::XHigh);
        assert_eq!(xhigh.source, EffortSource::Cli);

        let local = resolve_effort_setting(None, Some(EffortLevel::Low), Some(EffortLevel::Max));
        assert_eq!(local.level, EffortLevel::Low);
        assert_eq!(local.source, EffortSource::Config);

        let default = resolve_effort_setting(None, None, None);
        assert_eq!(default, ResolvedEffort::MEDIUM_DEFAULT);
    }

    #[test]
    fn effort_fields_parse_from_toml() {
        let raw: RawConfig = toml::from_str(
            r#"
                tl_effort_level = "high"
                worker_effort_level = "low"
                [reviewer]
                effort_level = "xhigh"
            "#,
        )
        .unwrap();
        assert_eq!(raw.tl_effort_level, Some(EffortLevel::High));
        assert_eq!(raw.worker_effort_level, Some(EffortLevel::Low));
        assert_eq!(raw.reviewer.unwrap().effort_level, Some(EffortLevel::XHigh));
        assert!(EffortLevel::from_str("extra-high", false).is_err());
    }

    #[test]
    fn reviewer_effort_environment_values_are_strictly_parsed() {
        assert_eq!(
            super::parse_effort_override(REVIEWER_EFFORT_ENV, Some(" HIGH ".to_string())).unwrap(),
            Some(EffortLevel::High)
        );
        assert_eq!(
            super::parse_effort_override(REVIEWER_EFFORT_ENV, Some(" ".to_string())).unwrap(),
            None
        );
        let error = super::parse_effort_override(REVIEWER_EFFORT_ENV, Some("turbo".to_string()))
            .unwrap_err();
        assert!(error.to_string().contains(REVIEWER_EFFORT_ENV));
    }
}
