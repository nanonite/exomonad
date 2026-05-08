//! Configuration discovery from .exo/config.toml and config.local.toml

use anyhow::{Context, Result};
use exomonad_core::services::AgentType;
use exomonad_core::Role;

fn parse_agent_type_env(s: &str) -> Option<AgentType> {
    match s.to_lowercase().as_str() {
        "claude" | "claude-code" => Some(AgentType::Claude),
        "gemini" => Some(AgentType::Gemini),
        "opencode" | "opencode-cli" => Some(AgentType::OpenCode),
        "shoal" => Some(AgentType::Shoal),
        _ => None,
    }
}
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tracing::debug;

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
    /// Model override (e.g., "haiku", "sonnet"). Passed as --model flag to Claude/Gemini.
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
    /// Model string passed to the reviewer agent
    /// (e.g. "claude-haiku-4-5-20251001", "anthropic/claude-haiku-4-5").
    /// `None` means the agent picks its own default.
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
    pub tmux_session: Option<String>,

    /// Base directory for worktrees (default: .exo/worktrees).
    pub worktree_base: Option<PathBuf>,

    /// Shell command to wrap environment (e.g. "nix develop"). TL tab runs this as shell.
    pub shell_command: Option<String>,

    /// WASM directory override (default: .exo/wasm/).
    pub wasm_dir: Option<PathBuf>,

    /// Agent type for the root (TL) tab.
    pub root_agent_type: Option<AgentType>,

    /// Agent type for spawned workers/teammates.
    pub spawn_agent_type: Option<AgentType>,

    /// Optional flake reference to use when building WASM plugin via nix.
    pub flake_ref: Option<String>,

    /// Name of the WASM module (default: "devswarm"). Used to find wasm-guest-{name}.wasm.
    pub wasm_name: Option<String>,

    /// Extra MCP servers to include in agent settings (e.g. metacog).
    #[serde(default)]
    pub extra_mcp_servers: std::collections::HashMap<String, McpServerConfig>,

    /// Initial prompt for the root agent (used with `gemini --prompt-interactive`).
    pub initial_prompt: Option<String>,

    /// Custom command for the root TL window (overrides agent_type-based default).
    /// Use for development (e.g., `cargo run -p shoal-agent -- --exo root`).
    pub root_command: Option<String>,

    /// When true, spawned Gemini agents receive `--yolo` flag.
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

    /// WebSocket URL of the local Tangled knot (e.g. "ws://localhost:7000").
    /// When set, exomonad subscribes to the knot's /events for pipeline→branch mappings.
    pub tangled_knot_url: Option<String>,

    /// WebSocket URL of the local Tangled spindle (e.g. "ws://localhost:8080").
    /// When set, exomonad subscribes to the spindle's /events for CI status updates.
    pub tangled_spindle_url: Option<String>,

    /// DID of the repo owner on the Tangled knot (e.g. "did:plc:abc123").
    /// Required alongside tangled_knot_url for XRPC repo registration during init.
    pub tangled_owner_did: Option<String>,

    /// Docker container name for the local Tangled knot (e.g. "tangled-knot-knot-1").
    /// When set alongside tangled_owner_did, exomonad init registers the repo via docker exec.
    pub tangled_knot_container: Option<String>,

    /// Absolute path to the spindle SQLite database (e.g. "/home/user/project/spindle.db").
    /// Used during init to INSERT the repo into the spindle's repos table.
    pub tangled_spindle_db: Option<String>,

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
    pub tmux_session: String,
    /// Base directory for worktrees.
    pub worktree_base: PathBuf,
    /// Shell command to wrap environment (e.g. "nix develop").
    pub shell_command: Option<String>,
    /// Resolved WASM directory.
    pub wasm_dir: PathBuf,
    /// Agent type for the root (TL) tab.
    pub root_agent_type: AgentType,
    /// Agent type for spawned workers/teammates.
    pub spawn_agent_type: AgentType,
    /// Flake reference to use when building WASM plugin via nix.
    pub flake_ref: Option<String>,
    /// Name of the WASM module (default: "devswarm").
    pub wasm_name: String,
    /// Extra MCP servers to include in agent settings.
    pub extra_mcp_servers: std::collections::HashMap<String, McpServerConfig>,
    /// Initial prompt for the root agent.
    pub initial_prompt: Option<String>,
    /// When true, spawned Gemini agents receive `--yolo` flag.
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

    /// GitHub poller interval in seconds (default: 60).
    pub poll_interval: Option<u64>,

    /// OpenRouter routing configuration.
    pub openrouter: OpenRouterConfig,

    /// Opencode agent configuration.
    pub opencode: OpencodeConfig,

    /// Use OpenCode as the root TL agent instead of Claude.
    pub opencode_as_tl: bool,

    /// WebSocket URL of the local Tangled knot (e.g. "ws://localhost:7000").
    pub tangled_knot_url: Option<String>,

    /// WebSocket URL of the local Tangled spindle (e.g. "ws://localhost:8080").
    pub tangled_spindle_url: Option<String>,

    /// DID of the repo owner on the Tangled knot (e.g. "did:plc:abc123").
    pub tangled_owner_did: Option<String>,

    /// Docker container name for the local Tangled knot (e.g. "tangled-knot-knot-1").
    pub tangled_knot_container: Option<String>,

    /// Absolute path to the spindle SQLite database.
    pub tangled_spindle_db: Option<String>,

    /// PR reviewer agent configuration.
    pub reviewer: ReviewerConfig,
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
        let project_root = find_project_root()?;

        let local_path = project_root.join(".exo/config.local.toml");
        let global_path = project_root.join(".exo/config.toml");

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

        // Resolve tmux_session: config > directory name
        let tmux_session = local_raw
            .tmux_session
            .or(global_raw.tmux_session)
            .unwrap_or_else(|| {
                project_root
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("exomonad")
                    .to_string()
            });
        let tmux_session = sanitize_session_name(tmux_session);

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

        // Resolve root_agent_type: env > global > local > default (Claude)
        let root_agent_type = std::env::var("EXOMONAD_ROOT_AGENT_TYPE")
            .ok()
            .and_then(|s| parse_agent_type_env(&s))
            .or(global_raw.root_agent_type)
            .or(local_raw.root_agent_type)
            .unwrap_or(AgentType::Claude);

        // Resolve spawn_agent_type: env > local > global > default (Gemini)
        let spawn_agent_type = std::env::var("EXOMONAD_SPAWN_AGENT_TYPE")
            .ok()
            .and_then(|s| parse_agent_type_env(&s))
            .or(local_raw.spawn_agent_type)
            .or(global_raw.spawn_agent_type)
            .unwrap_or(AgentType::Gemini);

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

        // Resolve model: local > global
        let model = local_raw.model.or(global_raw.model);

        // Resolve poll_interval: local > global
        let poll_interval = local_raw.poll_interval.or(global_raw.poll_interval);

        // Resolve openrouter: local > global > default
        let openrouter = local_raw
            .openrouter
            .or(global_raw.openrouter)
            .unwrap_or_default();

        // Resolve opencode: env > local > global > default
        let mut opencode = local_raw
            .opencode
            .or(global_raw.opencode)
            .unwrap_or_default();
        if let Ok(m) = std::env::var("EXOMONAD_TL_MODEL") {
            if !m.is_empty() {
                opencode.tl_model = Some(m);
            }
        }
        if let Ok(m) = std::env::var("EXOMONAD_WORKER_MODEL") {
            if !m.is_empty() {
                opencode.worker_model = Some(m);
            }
        }

        // Resolve opencode_as_tl: local > global > false
        let opencode_as_tl = local_raw.opencode_as_tl.or(global_raw.opencode_as_tl).unwrap_or(false);

        // Resolve tangled config: local > global
        let tangled_knot_url = local_raw.tangled_knot_url.or(global_raw.tangled_knot_url);
        let tangled_spindle_url = local_raw.tangled_spindle_url.or(global_raw.tangled_spindle_url);
        let tangled_owner_did = local_raw.tangled_owner_did.or(global_raw.tangled_owner_did);
        let tangled_knot_container = local_raw.tangled_knot_container.or(global_raw.tangled_knot_container);
        let tangled_spindle_db = local_raw.tangled_spindle_db.or(global_raw.tangled_spindle_db);

        // Resolve reviewer: local > global > default
        let reviewer = local_raw.reviewer.or(global_raw.reviewer).unwrap_or_default();

        Ok(Self {
            project_dir,
            role,
            tmux_session,
            worktree_base,
            shell_command,
            wasm_dir,
            root_agent_type,
            spawn_agent_type,
            flake_ref,
            wasm_name,
            extra_mcp_servers,
            initial_prompt,
            yolo,
            companions,
            root_command,
            otlp_endpoint,
            model,
            poll_interval,
            openrouter,
            opencode,
            opencode_as_tl,
            tangled_knot_url,
            tangled_spindle_url,
            tangled_owner_did,
            tangled_knot_container,
            tangled_spindle_db,
            reviewer,
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

impl Default for Config {
    fn default() -> Self {
        Self {
            project_dir: PathBuf::from("."),
            role: Role::tl(),
            tmux_session: "default".to_string(),
            worktree_base: PathBuf::from(".exo/worktrees"),
            shell_command: None,
            wasm_dir: PathBuf::from(".exo/wasm"),
            root_agent_type: AgentType::Claude,
            spawn_agent_type: AgentType::Gemini,
            flake_ref: None,
            wasm_name: "devswarm".to_string(),
            extra_mcp_servers: std::collections::HashMap::new(),
            initial_prompt: None,
            yolo: false,
            companions: Vec::new(),
            root_command: None,
            otlp_endpoint: None,
            model: None,
            poll_interval: None,
            openrouter: OpenRouterConfig::default(),
            opencode: OpencodeConfig::default(),
            opencode_as_tl: false,
            tangled_knot_url: None,
            tangled_spindle_url: None,
            tangled_owner_did: None,
            tangled_knot_container: None,
            tangled_spindle_db: None,
            reviewer: ReviewerConfig::default(),
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

/// Sanitize session name.
/// - Max 36 characters
/// - Replace . with _ (dots cause issues)
fn sanitize_session_name(name: String) -> String {
    name.replace('.', "_").chars().take(36).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_raw_config_parse_local() {
        let content = r#"
            role = "dev"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.role, Some(Role::dev()));
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
    }

    #[test]
    fn test_raw_config_parse_root_agent_type() {
        let content = r#"
            root_agent_type = "gemini"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.root_agent_type, Some(AgentType::Gemini));
    }

    #[test]
    fn test_sanitize_session_name() {
        // Dots replaced with underscores
        assert_eq!(
            sanitize_session_name("my.project".to_string()),
            "my_project"
        );

        // Max 36 characters
        let long_name = "a".repeat(50);
        assert_eq!(sanitize_session_name(long_name).len(), 36);

        // Clean name unchanged
        assert_eq!(sanitize_session_name("exomonad".to_string()), "exomonad");
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
        let content = r#"
            [[companions]]
            name = "sleeptime"
            agent_type = "claude"
            command = "claude -c"
            task = "You are sleeptime"

            [[companions]]
            name = "researcher"
            agent_type = "gemini"
            command = "gemini"
            task = "Research task"
        "#;
        let raw: RawConfig = toml::from_str(content).unwrap();
        assert_eq!(raw.companions.len(), 2);
        assert_eq!(raw.companions[0].agent_type, Some(AgentType::Claude));
        assert_eq!(raw.companions[1].agent_type, Some(AgentType::Gemini));
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
    fn test_reviewer_config_invalid_agent_type_rejected() {
        let content = "[reviewer]\nagent_type = \"invalid\"\n";
        let result: Result<RawConfig, _> = toml::from_str(content);
        assert!(result.is_err(), "Unknown agent_type should fail to deserialize");
    }

    #[test]
    fn test_reviewer_config_default_is_claude() {
        let config = Config::default();
        assert_eq!(config.reviewer.agent_type, AgentType::Claude);
        assert!(config.reviewer.model.is_none());
        assert!(config.reviewer.context.is_empty());
    }
}
