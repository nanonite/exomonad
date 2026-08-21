//! High-level agent control service.
//!
//! Provides semantic operations for agent lifecycle management:
//! - SpawnAgent: Create agent directory, open tmux window
//! - CleanupAgent: Close tab, remove per-agent config
//! - ListAgents: Discover from tmux windows (source of truth for running agents)

mod cleanup;
mod internal;
pub mod invocation;
mod spawn;

pub use invocation::{
    finish_invocation, finish_invocation_and_tombstone,
    finish_invocation_and_tombstone_with_context, finish_invocation_with_context, read_invocation,
    read_invocation_conservatively, start_invocation, start_invocation_with_provenance,
    start_invocation_with_provenance_and_context, InvocationExitContext, InvocationFinishResult,
    InvocationIdentityContext, InvocationMetadata, InvocationRecord, InvocationStatus,
    InvocationTrigger, INVOCATION_FILENAME,
};
pub use spawn::{
    CODEX_DEV_INSTRUCTIONS, CODEX_REVIEWER_INSTRUCTIONS, CODEX_TL_RUNTIME_NOTES,
    CODEX_WORKER_INSTRUCTIONS, OPENCODE_DEV_INSTRUCTIONS, OPENCODE_WORKER_INSTRUCTIONS,
};

pub(crate) use crate::common::TimeoutError;
pub(crate) use crate::domain::{
    AgentName, AgentPermissions, BirthBranch, BranchName, ClaudeSessionUuid, ItemState,
    RoutingInfo, Slug, TeamName,
};
pub(crate) use crate::effects::EffectError;
pub(crate) use crate::ffi::FFIBoundary;
pub(crate) use crate::{GithubOwner, GithubRepo, IssueNumber};
pub(crate) use anyhow::{anyhow, Context, Result};
pub(crate) use serde::{Deserialize, Serialize};
pub(crate) use std::collections::{HashMap, HashSet};
pub(crate) use std::path::{Path, PathBuf};
pub(crate) use tokio::fs;
pub(crate) use tokio::process::Command;
pub(crate) use tokio::time::{timeout, Duration};
pub(crate) use tracing::{debug, info, instrument, warn};

pub(crate) use super::agent_resolver::{AgentIdentityRecord, AgentResolver};
pub(crate) use super::git_worktree::GitWorktreeService;
pub(crate) use super::github::{GitHubClient, GitHubService, Repo};
pub(crate) use super::tmux_events;
pub(crate) use super::tmux_ipc;
pub(crate) use claude_teams_bridge::TeamRegistry;
pub(crate) use std::sync::Arc;

pub(crate) const SPAWN_TIMEOUT: Duration = Duration::from_secs(60);
pub(crate) const TMUX_TIMEOUT: Duration = Duration::from_secs(30);
pub(crate) const INVOCATION_STARTUP_TIMEOUT: Duration = Duration::from_secs(3);
pub(crate) const INVOCATION_MONITOR_INTERVAL: Duration = Duration::from_millis(200);
pub(crate) const LAST_ACTIVITY_FILE: &str = "last_activity_at";
const MAX_INVOCATION_STDERR_TAIL_BYTES: usize = 4096;

async fn read_invocation_stderr_tail(agent_dir: &Path) -> Option<String> {
    for filename in ["stderr_tail", "stderr.log", "stderr"] {
        let Ok(bytes) = fs::read(agent_dir.join(filename)).await else {
            continue;
        };
        if bytes.is_empty() {
            continue;
        }
        let start = bytes.len().saturating_sub(MAX_INVOCATION_STDERR_TAIL_BYTES);
        let tail = String::from_utf8_lossy(&bytes[start..]).trim().to_string();
        if !tail.is_empty() {
            return Some(tail);
        }
    }
    None
}

/// Push the parent branch to the remote so child PRs can reference it as
/// their base. Non-fatal: warns on failure (supports local/airgapped setups
/// where no remote or non-GitHub remote is configured).
pub(crate) async fn ensure_branch_pushed(
    git_wt: &Arc<GitWorktreeService>,
    branch: &BranchName,
    project_dir: &Path,
) {
    info!(branch = %branch, "Pushing parent branch to remote");
    let git_wt = git_wt.clone();
    let dir = project_dir.to_path_buf();
    let bookmark = branch.clone();
    match tokio::task::spawn_blocking(move || git_wt.push_bookmark(&dir, &bookmark)).await {
        Ok(Ok(())) => info!(branch = %branch, "Branch pushed successfully"),
        Ok(Err(e)) => {
            let anyhow_err = anyhow::Error::from(EffectError::from(e));
            warn!(
                branch = %branch,
                error = %anyhow_err,
                "Failed to push parent branch (non-fatal, PRs may not work)"
            )
        }
        Err(e) => warn!(branch = %branch, error = %e, "Push task panicked (non-fatal)"),
    }
}

/// Fetch a branch from the remote if it exists (but not locally).
///
/// This is needed when re-spawning a leaf whose worktree was deleted but
/// whose branch and PR still exist on the remote. `git worktree add` requires
/// the branch to exist locally, so we fetch it first.
pub(crate) async fn ensure_branch_fetched(project_dir: &Path, branch: &BranchName) {
    let branch_str = branch.as_str();
    let ls_output = match tokio::process::Command::new("git")
        .args(["ls-remote", "origin", branch_str])
        .current_dir(project_dir)
        .output()
        .await
    {
        Ok(o) => o,
        Err(e) => {
            warn!(branch = %branch_str, error = %e, "git ls-remote failed, skipping fetch");
            return;
        }
    };

    if !ls_output.status.success() || String::from_utf8_lossy(&ls_output.stdout).trim().is_empty() {
        return;
    }

    info!(branch = %branch_str, "Branch exists on remote, fetching for worktree recovery");
    match tokio::process::Command::new("git")
        .args(["fetch", "origin", &format!("{}:{}", branch_str, branch_str)])
        .current_dir(project_dir)
        .output()
        .await
    {
        Ok(o) if o.status.success() => {
            info!(branch = %branch_str, "Fetched remote branch for recovery");
        }
        Ok(o) => {
            warn!(
                branch = %branch_str,
                stderr = %String::from_utf8_lossy(&o.stderr).trim(),
                "git fetch failed for branch recovery"
            );
        }
        Err(e) => {
            warn!(branch = %branch_str, error = %e, "git fetch command failed for branch recovery");
        }
    }
}

/// If no git remote is configured, create a local bare repo and set it as origin.
/// This enables local-only workflows where agents need a remote for PR creation.
pub(crate) async fn ensure_remote_exists(project_dir: &Path) {
    let output = tokio::process::Command::new("git")
        .args(["remote"])
        .current_dir(project_dir)
        .output()
        .await;

    let has_remote = output
        .map(|o| !String::from_utf8_lossy(&o.stdout).trim().is_empty())
        .unwrap_or(false);

    if has_remote {
        return;
    }

    let dir_name = project_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("repo");
    let bare_path = project_dir
        .parent()
        .unwrap_or(project_dir)
        .join(format!("{}.git-remote", dir_name));

    info!(
        path = %bare_path.display(),
        "No git remote configured — creating local bare repo as origin"
    );

    let _ = tokio::process::Command::new("git")
        .args(["init", "--bare", bare_path.to_str().unwrap_or("")])
        .output()
        .await;

    let _ = tokio::process::Command::new("git")
        .args(["remote", "add", "origin", bare_path.to_str().unwrap_or("")])
        .current_dir(project_dir)
        .output()
        .await;

    info!(path = %bare_path.display(), "Local bare repo set as origin");
}

// ============================================================================
// Types
// ============================================================================

/// Pairs a bare slug with its agent type, providing named accessors for all
/// derived naming forms. Construct at the boundary (from proto fields, dir names,
/// MCP args), then thread through code as a typed value.
///
/// # Naming concepts
/// - **slug**: Bare human-readable identifier (`"feature-a"`)
/// - **internal_name**: Suffixed directory/identity name as `AgentName` (`"feature-a-claude"`)
/// - **display_name**: tmux window name (`"🤖 feature-a-claude"`)
///
/// `internal_name()` returns `AgentName` (a validated newtype), not `String`.
/// This makes it impossible to accidentally pass a bare slug where an internal
/// name is expected — the type system catches it.
#[derive(Debug, Clone)]
pub struct AgentIdentity {
    slug: String,
    agent_type: AgentType,
}

impl AgentIdentity {
    /// Construct from a bare slug and agent type.
    pub fn new(slug: String, agent_type: AgentType) -> Self {
        Self { slug, agent_type }
    }

    /// Parse from an internal name (e.g., `"feature-a-claude"` → slug=`"feature-a"`, type=Claude).
    /// Falls back to Codex if no known suffix is found.
    pub fn from_internal_name(name: &str) -> Self {
        let agent_type = AgentType::from_dir_name(name);
        let suffix = format!("-{}", agent_type.suffix());
        let slug = name.strip_suffix(&suffix).unwrap_or(name).to_string();
        Self { slug, agent_type }
    }

    /// Bare slug without type suffix.
    pub fn slug(&self) -> &str {
        &self.slug
    }

    /// Agent type.
    pub fn agent_type(&self) -> AgentType {
        self.agent_type
    }

    /// Suffixed directory/identity name as a validated `AgentName`.
    ///
    /// Used for: worktree dirs, agent config dirs, synthetic member names,
    /// MCP --name flag, EXOMONAD_AGENT_ID env var.
    pub fn internal_name(&self) -> AgentName {
        // Safe: slug is non-empty (validated at construction) and suffix is non-empty,
        // so the formatted string is always non-empty.
        AgentName::try_from_str(format!("{}-{}", self.slug, self.agent_type.suffix()).as_str())
            .expect("validated string input is non-empty")
    }

    /// tmux window display name (e.g., `"🤖 feature-a-claude"`).
    ///
    /// Includes the type suffix so `resolve_worktree_from_tab` (which extracts the
    /// segment after the emoji) yields the internal_name, matching the worktree directory.
    pub fn display_name(&self) -> String {
        format!("{} {}", self.agent_type.emoji(), self.internal_name())
    }
}

/// Agent type for spawned agents.
///
/// Determines which CLI tool to use when spawning an agent in a tmux window.
/// Each type has different command names and prompt flags.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
#[derive(Default)]
pub enum AgentType {
    /// Claude Code CLI (spawns with `claude --prompt '...'`).
    Claude,

    /// Custom binary agent (e.g., shoal-agent).
    Shoal,

    /// OpenCode CLI (spawns with `opencode run "..."`).
    OpenCode,

    /// OpenAI Codex CLI.
    #[default]
    Codex,

    /// Plain long-running process (no MCP, no agent identity, no worktree).
    /// Used for companion processes like mock servers, log tailers, etc.
    Process,
}

pub const AGENT_TYPE_DEPRECATION_MESSAGE: &str =
    "agent_type 'gemini' is retired; use 'codex' (model gpt-luna). See CLAUDE.md Configuration."; // deprecation

impl<'de> Deserialize<'de> for AgentType {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        let value = value.to_ascii_lowercase();
        if value == ["ge", "mini"].concat() {
            return Err(serde::de::Error::custom(AGENT_TYPE_DEPRECATION_MESSAGE));
        }
        match value.as_str() {
            "claude" | "claude-code" => Ok(Self::Claude),
            "shoal" => Ok(Self::Shoal),
            "opencode" | "opencode-cli" => Ok(Self::OpenCode),
            "codex" => Ok(Self::Codex),
            "process" => Ok(Self::Process),
            value => Err(serde::de::Error::custom(format!(
                "unknown agent type '{value}'; expected claude, shoal, opencode, codex, or process"
            ))),
        }
    }
}

/// Static metadata for each agent type, replacing per-method match dispatch.
pub(crate) struct AgentMetadata {
    pub(crate) command: &'static str,
    pub(crate) prompt_flag: &'static str,
    pub(crate) suffix: &'static str,
    pub(crate) emoji: &'static str,
}

pub(crate) const CLAUDE_META: AgentMetadata = AgentMetadata {
    command: "claude",
    prompt_flag: "",
    suffix: "claude",
    emoji: "\u{1F916}", // 🤖
};

pub(crate) const SHOAL_META: AgentMetadata = AgentMetadata {
    command: "shoal-agent",
    prompt_flag: "",
    suffix: "shoal",
    emoji: "\u{1F30A}", // 🌊
};

pub(crate) const OPENCODE_META: AgentMetadata = AgentMetadata {
    command: "opencode",
    prompt_flag: "run",
    suffix: "opencode",
    emoji: "\u{1F4BB}", // 💻
};

pub(crate) const CODEX_META: AgentMetadata = AgentMetadata {
    command: "codex",
    prompt_flag: "",
    suffix: "codex",
    emoji: "\u{1F916}", // 🤖
};

pub(crate) const PROCESS_META: AgentMetadata = AgentMetadata {
    command: "",
    prompt_flag: "",
    suffix: "process",
    emoji: "\u{2699}\u{FE0F}", // ⚙️
};

impl AgentType {
    pub(crate) fn meta(&self) -> &'static AgentMetadata {
        match self {
            AgentType::Claude => &CLAUDE_META,
            AgentType::Shoal => &SHOAL_META,
            AgentType::OpenCode => &OPENCODE_META,
            AgentType::Codex => &CODEX_META,
            AgentType::Process => &PROCESS_META,
        }
    }

    pub fn command(&self) -> &'static str {
        self.meta().command
    }
    pub(crate) fn prompt_flag(&self) -> &'static str {
        self.meta().prompt_flag
    }
    /// Agent type suffix for naming (e.g., "claude", "codex").
    pub fn suffix(&self) -> &'static str {
        self.meta().suffix
    }
    /// Emoji for display in tmux windows.
    pub fn emoji(&self) -> &'static str {
        self.meta().emoji
    }

    /// Generate a display name for tmux windows.
    ///
    /// Format: "{emoji} gh-{issue_id}-{short_slug}"
    /// The slug is truncated to 20 chars for readability.
    pub(crate) fn display_name(&self, issue_id: &str, slug: &str) -> String {
        format!("{} gh-{}-{}", self.emoji(), issue_id, slug)
    }

    /// tmux window display name for an agent with this type and slug.
    pub fn tab_display_name(&self, slug: &str) -> String {
        format!("{} {}", self.emoji(), slug)
    }

    /// Infer agent type from a worktree directory name (e.g., "feature-a-claude" → Claude).
    pub fn from_dir_name(dir_name: &str) -> Self {
        if dir_name.ends_with("-claude") {
            AgentType::Claude
        } else if dir_name.ends_with("-shoal") {
            AgentType::Shoal
        } else if dir_name.ends_with("-opencode") {
            AgentType::OpenCode
        } else if dir_name.ends_with("-codex") {
            AgentType::Codex
        } else if dir_name.ends_with("-process") {
            AgentType::Process
        } else {
            AgentType::Codex
        }
    }
}

/// Resolve the tmux window name of THIS agent from structural identity.
///
/// Root agent (no dots in birth_branch): "TL" tab (created by `exomonad init`).
/// Spawned subtree: "{emoji} {slug}" where slug = last segment of birth_branch.
/// Used for routing popup requests to the correct plugin instance.
/// Resolve the working directory for an agent from its birth branch.
///
/// Follows the dot-segment hierarchy: last segment is the agent name (suffixed).
/// Example: `"main.feature-a-claude"` → `".exo/worktrees/feature-a-claude/"`.
pub fn resolve_working_dir(birth_branch: &str) -> PathBuf {
    if let Some((_, slug)) = birth_branch.rsplit_once('.') {
        PathBuf::from(format!(".exo/worktrees/{}/", slug))
    } else {
        PathBuf::from(".")
    }
}

/// Resolve the working directory for an agent from its tmux tab name.
///
/// Tab names are formatted as `"{emoji} {agent_name}"` or `"TL"`.
/// Example: `"🤖 feature-a-codex"` → `".exo/worktrees/feature-a-codex/"`.
pub fn resolve_worktree_from_tab(tab: &str) -> PathBuf {
    if tab == "TL" {
        PathBuf::from(".")
    } else {
        // Tab name is "{emoji} {agent_name}" (e.g. "🤖 feature-a-codex")
        if let Some((_, agent_name)) = tab.split_once(' ') {
            PathBuf::from(format!(".exo/worktrees/{}/", agent_name))
        } else {
            PathBuf::from(".")
        }
    }
}

pub fn resolve_own_tab_name(ctx: &crate::effects::EffectContext) -> String {
    let birth_branch_str = ctx.birth_branch.as_str();

    if birth_branch_str.contains('.') {
        // Last dot-segment is the agent_name (suffixed), matching the tmux tab format.
        let agent_name = birth_branch_str
            .rsplit_once('.')
            .map(|(_, s)| s)
            .unwrap_or(birth_branch_str);
        let agent_type = AgentType::from_dir_name(agent_name);
        agent_type.tab_display_name(agent_name)
    } else {
        "TL".to_string()
    }
}

/// Resolve the tmux window name of the parent agent from structural identity.
///
/// Workers: parent derived from birth_branch (inherited).
/// Subtree agents: parent is one dot-level up in branch hierarchy.
/// Root agents (no dots): parent is the TL tab.
/// Parent tabs are always Claude (TL role), so always use the Claude emoji.
/// Options for spawning an agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpawnOptions {
    /// GitHub repository owner
    pub owner: GithubOwner,
    /// GitHub repository name
    pub repo: GithubRepo,
    /// Agent type.
    #[serde(default)]
    pub agent_type: AgentType,
    /// Sub-repository path relative to project_dir (e.g., "urchin/").
    /// When set, the agent's project context targets this directory instead of project_dir.
    pub subrepo: Option<PathBuf>,
    /// Base branch to branch off of (default: "main").
    pub base_branch: Option<BirthBranch>,
}

/// Claude-specific spawn flags for permission control.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ClaudeSpawnFlags {
    /// Permission mode. None = --dangerously-skip-permissions.
    pub permission_mode: Option<crate::domain::PermissionMode>,
    /// Tool patterns to allow (e.g., "Read", "Grep").
    pub allowed_tools: Vec<String>,
    /// Tool patterns to disallow (e.g., "Bash").
    pub disallowed_tools: Vec<String>,
}

/// Options for spawning a worker agent in the current worktree (no branch/worktree).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpawnWorkerOptions {
    /// Human-readable name for the worker
    pub name: AgentName,
    /// Implementation instructions
    pub prompt: String,
    /// Agent type (default: configured spawn harness).
    #[serde(default)]
    pub agent_type: AgentType,
    /// Optional model override, separate from the agent type.
    #[serde(default)]
    pub model: Option<String>,
    /// Claude-specific permission flags (ignored for non-Claude agents).
    #[serde(default)]
    pub claude_flags: ClaudeSpawnFlags,
}

/// Options for spawning a subtree agent (isolated worktree).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpawnSubtreeOptions {
    /// Full task/prompt for the agent.
    pub task: String,
    /// Branch name suffix.
    pub branch_name: String,
    /// Parent Claude session ID for --resume --fork-session context inheritance.
    pub parent_session_id: Option<ClaudeSessionUuid>,
    /// Optional role override.
    pub role: Option<crate::domain::Role>,
    /// Agent type. Required — no default.
    pub agent_type: AgentType,
    /// Claude-specific permission flags (ignored for other harnesses).
    #[serde(default)]
    pub claude_flags: ClaudeSpawnFlags,
    /// Optional working directory. If Some, worktree creation is skipped.
    pub working_dir: Option<PathBuf>,
    /// Optional agent permissions.
    pub permissions: Option<AgentPermissions>,
    /// When true, creates a standalone git repo instead of a worktree.
    pub standalone_repo: bool,
    /// Directories from the parent project to be copied into the agent's worktree.
    pub allowed_dirs: Vec<String>,
    /// Model override for this spawn. None = use service default (spawn_agent_model).
    #[serde(default)]
    pub model: Option<String>,
    /// Effort override for this spawn. None = use the role service default.
    #[serde(default)]
    pub effort: Option<String>,
    /// PR context for reviewer invocation metadata.
    #[serde(default)]
    pub invocation_pr_number: Option<u64>,
    /// Exact reviewed head SHA for invocation metadata.
    #[serde(default)]
    pub invocation_head_sha: Option<String>,
}

/// Options for spawning a leaf subtree agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpawnLeafOptions {
    /// Full task/prompt for the agent.
    pub task: String,
    /// Branch name suffix.
    pub branch_name: String,
    /// Optional role override.
    pub role: Option<crate::domain::Role>,
    /// Agent type. Required — no default.
    pub agent_type: AgentType,
    /// Optional model override, separate from the agent type.
    #[serde(default)]
    pub model: Option<String>,
    /// Claude-specific permission flags (ignored for other harnesses).
    #[serde(default)]
    pub claude_flags: ClaudeSpawnFlags,
    /// When true, creates a standalone git repo instead of a worktree.
    pub standalone_repo: bool,
    /// Directories from the parent project to be copied into the agent's worktree.
    pub allowed_dirs: Vec<String>,
    /// Optional exact revision from which to create the leaf branch.
    #[serde(default)]
    pub start_point: Option<String>,
    /// Optional base branch used for the leaf birth branch and future PR target.
    #[serde(default)]
    pub base_branch: Option<String>,
    /// Canonical identity resolved by the host for an existing-PR resume.
    /// When set, no directory scan or alternate runtime may replace it.
    #[serde(default)]
    pub expected_agent_name: Option<AgentName>,
    /// PR number when this starts a resume_pr invocation.
    #[serde(default)]
    pub invocation_pr_number: Option<u64>,
}

/// Result of spawning an agent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SpawnResult {
    /// Path to the agent directory (.exo/agents/{agent_id}/)
    pub agent_dir: PathBuf,
    /// Actual git branch created or resumed for the agent. Empty for shared-dir workers.
    pub branch_name: String,
    /// Agent's internal name (suffixed, e.g., "feature-a-claude").
    /// Typed as `AgentName` to prevent confusion with bare slugs.
    pub agent_name: AgentName,
    /// Issue title
    pub issue_title: String,
    /// Agent type
    pub agent_type: AgentType,
    /// Stable tmux pane id for ephemeral worker panes.
    pub pane_id: Option<String>,
}

impl FFIBoundary for SpawnResult {}

/// Simplified PR info for agent listing.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AgentPrInfo {
    /// PR number.
    pub number: u64,
    /// PR title.
    pub title: String,
    /// Web URL to the PR.
    pub url: String,
    /// PR state.
    pub state: ItemState,
}

impl FFIBoundary for AgentPrInfo {}

/// Workspace topology for an agent — how it relates to the project directory.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum Topology {
    /// Unknown or legacy agent.
    #[default]
    Unspecified,
    /// Agent works in an isolated git worktree.
    WorktreePerAgent,
    /// Agent shares the project directory.
    SharedDir,
}

impl Topology {
    /// Convert to proto i32 representation.
    pub fn to_proto(self) -> i32 {
        match self {
            Topology::Unspecified => 0,
            Topology::WorktreePerAgent => 1,
            Topology::SharedDir => 2,
        }
    }
}

/// Information about an active agent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AgentInfo {
    /// Internal name for the agent (e.g., "feature-a-claude").
    pub internal_name: AgentName,
    /// Whether a tmux window/pane exists for this agent.
    pub has_tab: bool,
    /// Workspace topology.
    #[serde(default)]
    pub topology: Topology,
    /// Path to agent directory (.exo/agents/{agent_id}/)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent_dir: Option<PathBuf>,
    /// Slug from agent name (e.g., "fix-bug-in-parser")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slug: Option<AgentName>,
    /// Agent type.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<AgentType>,
    /// Associated PR if one exists
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pr: Option<AgentPrInfo>,
    /// Unix timestamp for the last lifecycle activity or resume refresh.
    /// This is distinct from the inbox check timestamp.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_activity_at: Option<u64>,
}

impl FFIBoundary for AgentInfo {}

/// Result of batch spawn operation.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BatchSpawnResult {
    pub spawned: Vec<SpawnResult>,
    pub failed: Vec<(String, String)>, // (issue_id, error)
}

impl FFIBoundary for BatchSpawnResult {}

/// Result of batch cleanup operation.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BatchCleanupResult {
    pub cleaned: Vec<String>,
    pub failed: Vec<(String, String)>, // (issue_id, error)
}

impl FFIBoundary for BatchCleanupResult {}

#[derive(Clone, Debug, Default)]
pub struct ForgejoSpawnEnv {
    forgejo_url: Option<String>,
    forgejo_token: Option<String>,
    forgejo_reviewer_token: Option<String>,
    repo_owner: Option<String>,
    repo_name: Option<String>,
}

impl ForgejoSpawnEnv {
    pub fn new(
        forgejo_url: Option<String>,
        forgejo_token: Option<String>,
        forgejo_reviewer_token: Option<String>,
    ) -> Self {
        Self {
            forgejo_url,
            forgejo_token,
            forgejo_reviewer_token,
            repo_owner: None,
            repo_name: None,
        }
    }

    pub fn with_repo(mut self, owner: impl Into<String>, repo: impl Into<String>) -> Self {
        self.repo_owner = Some(owner.into());
        self.repo_name = Some(repo.into());
        self
    }

    fn insert_non_empty(env_vars: &mut HashMap<String, String>, key: &str, value: &str) {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            env_vars.insert(key.to_string(), trimmed.to_string());
        }
    }

    fn apply_to(&self, env_vars: &mut HashMap<String, String>) {
        if let Some(url) = self.forgejo_url.as_deref() {
            Self::insert_non_empty(env_vars, "FORGEJO_URL", url);
            if let Some(host) = forgejo_host_from_url(url) {
                env_vars.insert("FORGEJO_HOST".to_string(), host.clone());
                env_vars.insert("GH_HOST".to_string(), host);
            }
        }
        if let Some(token) = self.forgejo_token.as_deref() {
            Self::insert_non_empty(env_vars, "FORGEJO_TOKEN", token);
            Self::insert_non_empty(env_vars, "GH_TOKEN", token);
        }
        if let Some(token) = self.forgejo_reviewer_token.as_deref() {
            Self::insert_non_empty(env_vars, "FORGEJO_REVIEWER_TOKEN", token);
        }
        if let Some(owner) = self.repo_owner.as_deref() {
            Self::insert_non_empty(env_vars, "FORGEJO_OWNER", owner);
        }
        if let Some(repo) = self.repo_name.as_deref() {
            Self::insert_non_empty(env_vars, "FORGEJO_REPO", repo);
            Self::insert_non_empty(env_vars, "REPO", repo);
        }
    }
}

fn forgejo_host_from_url(input: &str) -> Option<String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return None;
    }
    let no_scheme = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
        .unwrap_or(trimmed);
    let host = no_scheme.split('/').next().unwrap_or_default().trim();
    if host.is_empty() {
        None
    } else {
        Some(host.to_string())
    }
}

// ============================================================================
// Service
// ============================================================================

/// Agent control service for high-level agent lifecycle management.
///
/// Generic over `C` (capability context) which provides shared registries
/// via Has* trait bounds. The same `C` is typically `Services`.
#[derive(Clone)]
pub struct AgentControlService<C> {
    /// Capability context providing shared registries.
    pub(crate) ctx: Arc<C>,
    /// Base directory for worktrees (default: .exo/worktrees)
    pub(crate) worktree_base: PathBuf,
    /// tmux session name for event emission
    pub(crate) tmux_session: Option<String>,
    /// Direct tmux IPC client.
    pub(crate) tmux_ipc: Option<super::tmux_ipc::TmuxIpc>,
    /// This agent's birth-branch (git identity). Root TL = "main".
    pub(crate) birth_branch: BirthBranch,
    /// Legacy compatibility flag; no longer changes harness behavior.
    pub(crate) yolo: bool,
    /// Agent type for spawned workers/teammates.
    pub(crate) spawn_agent_type: AgentType,
    /// Model for spawned OpenCode workers (passed to `opencode serve --model` / `opencode run --model`).
    /// `None` means let opencode pick.
    pub(crate) spawn_agent_model: Option<String>,
    /// Effort inherited by spawned TLs, leaves, workers, and companions.
    pub(crate) spawn_agent_effort: Option<String>,
    /// WASM name for role context resolution (default: "devswarm").
    pub(crate) wasm_name: String,
    /// Pre-serialized extra MCP servers to include in spawned agent configs.
    pub(crate) extra_mcp_servers: HashMap<String, serde_json::Value>,
    /// OpenRouter API key. When Some, all LLM calls route through OpenRouter.
    pub(crate) openrouter_api_key: Option<String>,
    /// Agent type for the reviewer. Default: Claude.
    pub(crate) reviewer_agent_type: AgentType,
    /// Model for the reviewer agent. None = agent picks its default.
    pub(crate) reviewer_model: Option<String>,
    /// Effort used by automatically spawned reviewers.
    pub(crate) reviewer_effort: Option<String>,
    /// Context file paths injected into the reviewer's task (e.g. CLAUDE.md, reviewer rules).
    pub(crate) reviewer_context: Vec<String>,
    /// Forgejo environment sourced from loaded config, overriding stale parent process env.
    pub(crate) forgejo_spawn_env: Option<ForgejoSpawnEnv>,
}

impl<
        C: super::HasGitHubClient
            + super::HasTeamRegistry
            + super::HasAgentResolver
            + super::HasProjectDir
            + super::HasGitWorktreeService
            + 'static,
    > AgentControlService<C>
{
    /// Create a new agent control service.
    pub fn new(ctx: Arc<C>) -> Self {
        let worktree_base = ctx.project_dir().join(".exo/worktrees");
        Self {
            ctx,
            worktree_base,
            tmux_session: None,
            tmux_ipc: None,
            birth_branch: BirthBranch::try_from_str("unset")
                .expect("literal validated string is non-empty"),
            yolo: false,
            spawn_agent_type: AgentType::default(),
            spawn_agent_model: None,
            spawn_agent_effort: None,
            wasm_name: "devswarm".to_string(),
            extra_mcp_servers: HashMap::new(),
            openrouter_api_key: None,
            reviewer_agent_type: AgentType::Claude,
            reviewer_model: None,
            reviewer_effort: None,
            reviewer_context: vec![],
            forgejo_spawn_env: None,
        }
    }

    /// Set the worktree base directory.
    pub fn with_worktree_base(mut self, base: PathBuf) -> Self {
        self.worktree_base = base;
        self
    }

    /// Set the WASM name for role context resolution.
    pub fn with_wasm_name(mut self, wasm_name: String) -> Self {
        self.wasm_name = wasm_name;
        self
    }

    /// Set the tmux session name for event emission + direct IPC.
    pub fn with_tmux_session(mut self, session: String) -> Self {
        self.tmux_ipc = Some(super::tmux_ipc::TmuxIpc::new(&session));
        self.tmux_session = Some(session);
        self
    }

    /// Set the birth-branch (git identity) for this agent.
    pub fn with_birth_branch(mut self, branch: BirthBranch) -> Self {
        self.birth_branch = branch;
        self
    }

    /// Retained for configuration compatibility; no longer changes harness behavior.
    pub fn with_yolo(mut self, yolo: bool) -> Self {
        self.yolo = yolo;
        self
    }

    /// Set the agent type for spawned workers/teammates.
    pub fn with_spawn_agent_type(mut self, agent_type: AgentType) -> Self {
        self.spawn_agent_type = agent_type;
        self
    }

    /// Get the configured default agent type for spawned workers/teammates.
    pub fn default_spawn_agent_type(&self) -> AgentType {
        self.spawn_agent_type
    }

    /// Set the model for spawned OpenCode workers.
    pub fn with_spawn_agent_model(mut self, model: Option<String>) -> Self {
        self.spawn_agent_model = model;
        self
    }

    /// Get the configured model for spawned OpenCode workers, if any.
    pub fn spawn_agent_model(&self) -> Option<&str> {
        self.spawn_agent_model.as_deref()
    }

    /// Set the effort inherited by spawned agents and companions.
    pub fn with_spawn_agent_effort(mut self, effort: Option<String>) -> Self {
        self.spawn_agent_effort = effort;
        self
    }

    /// Get the inherited effort for spawned agents, if configured.
    pub fn spawn_agent_effort(&self) -> Option<&str> {
        self.spawn_agent_effort.as_deref()
    }

    /// Set the effort used by automatically spawned reviewers.
    pub fn with_reviewer_effort(mut self, effort: Option<String>) -> Self {
        self.reviewer_effort = effort;
        self
    }

    /// Get the reviewer effort, if configured.
    pub fn reviewer_effort(&self) -> Option<&str> {
        self.reviewer_effort.as_deref()
    }

    /// Resolve the model that the requested harness will actually receive.
    pub(crate) fn effective_model_for(
        &self,
        agent_type: AgentType,
        role: &str,
        override_model: Option<&str>,
    ) -> Option<String> {
        override_model
            .filter(|model| !model.trim().is_empty())
            .map(str::to_string)
            .or_else(|| {
                if role == "reviewer" {
                    self.reviewer_model.clone()
                } else if matches!(agent_type, AgentType::OpenCode | AgentType::Codex) {
                    self.spawn_agent_model.clone()
                } else {
                    None
                }
            })
    }

    /// Resolve the effort that the requested harness will actually receive.
    pub(crate) fn effective_effort_for(
        &self,
        role: &str,
        override_effort: Option<&str>,
    ) -> Option<String> {
        override_effort
            .filter(|effort| !effort.trim().is_empty())
            .map(str::to_string)
            .or_else(|| self.effort_for_role(role).map(str::to_string))
    }

    /// Set the agent type for the reviewer.
    pub fn with_reviewer_agent_type(mut self, agent_type: AgentType) -> Self {
        self.reviewer_agent_type = agent_type;
        self
    }

    /// Set the model for the reviewer agent.
    pub fn with_reviewer_model(mut self, model: Option<String>) -> Self {
        self.reviewer_model = model;
        self
    }

    /// Set context file paths injected into the reviewer's task.
    pub fn with_reviewer_context(mut self, context: Vec<String>) -> Self {
        self.reviewer_context = context;
        self
    }

    /// Set Forgejo env values from the loaded config for spawned agents.
    pub fn with_forgejo_spawn_env(mut self, env: ForgejoSpawnEnv) -> Self {
        self.forgejo_spawn_env = Some(env);
        self
    }

    /// Set extra MCP servers to include in spawned agent configs.
    pub fn with_extra_mcp_servers(mut self, servers: HashMap<String, serde_json::Value>) -> Self {
        self.extra_mcp_servers = servers;
        self
    }

    /// Enable OpenRouter routing: all Claude CLI agents get ANTHROPIC_BASE_URL injected.
    pub fn with_openrouter(mut self, api_key: String) -> Self {
        self.openrouter_api_key = Some(api_key);
        self
    }

    /// Project root directory (from capability context).
    pub(crate) fn project_dir(&self) -> &std::path::Path {
        self.ctx.project_dir()
    }

    /// GitHub client (from capability context).
    pub(crate) fn github(&self) -> Option<&Arc<GitHubClient>> {
        self.ctx.github_client()
    }

    /// Git worktree service Arc (from capability context).
    pub(crate) fn git_wt(&self) -> &Arc<GitWorktreeService> {
        self.ctx.git_worktree_service()
    }

    /// Team registry (from capability context).
    pub(crate) fn team_registry(&self) -> &TeamRegistry {
        self.ctx.team_registry()
    }

    /// Agent resolver (from capability context).
    pub(crate) fn agent_resolver(&self) -> &AgentResolver {
        self.ctx.agent_resolver()
    }

    /// Resolve the effective birth branch for spawn operations.
    ///
    /// Callers pass the birth branch from EffectContext. Falls back to `self.birth_branch`
    /// if no override is provided.
    pub(crate) fn effective_birth_branch(&self, override_bb: Option<&BirthBranch>) -> BirthBranch {
        override_bb.cloned().unwrap_or_else(|| {
            debug_assert!(
                self.birth_branch.as_str() != "unset",
                "birth_branch was never initialized via with_birth_branch()"
            );
            self.birth_branch.clone()
        })
    }

    /// Common post-spawn bookkeeping.
    ///
    /// Creates the agent's config directory, writes routing info, and registers
    /// identity with the resolver if available.
    pub(crate) async fn finalize_spawn(
        &self,
        agent_name: &AgentName,
        routing: RoutingInfo,
        identity: Option<AgentIdentityRecord>,
    ) -> Result<PathBuf> {
        let runtime = identity
            .as_ref()
            .map(|record| record.agent_type)
            .unwrap_or_else(|| AgentType::from_dir_name(agent_name.as_str()));
        let model = identity.as_ref().and_then(|record| record.model.clone());
        let effort = identity.as_ref().and_then(|record| record.effort.clone());
        let identity_context = InvocationIdentityContext {
            runtime_agent_id: Some(agent_name.to_string()),
            slice_id: identity.as_ref().and_then(|record| record.slice_id.clone()),
            branch: identity
                .as_ref()
                .map(|record| record.birth_branch.to_string()),
            worktree: identity
                .as_ref()
                .map(|record| record.working_dir.to_string_lossy().into_owned()),
        };
        self.finalize_spawn_with_invocation(
            agent_name,
            routing,
            identity,
            InvocationMetadata {
                runtime,
                trigger: InvocationTrigger::Spawn,
                pr_number: None,
                head_sha: None,
                model,
                effort,
                identity: Some(identity_context),
            },
        )
        .await
    }

    pub(crate) async fn finalize_spawn_with_invocation(
        &self,
        agent_name: &AgentName,
        routing: RoutingInfo,
        identity: Option<AgentIdentityRecord>,
        metadata: InvocationMetadata,
    ) -> Result<PathBuf> {
        let agent_config_dir = self
            .project_dir()
            .join(".exo/agents")
            .join(agent_name.as_str());
        fs::create_dir_all(&agent_config_dir).await?;
        if !routing.has_delivery_target() {
            return Err(anyhow!(
                "refusing to finalize spawn for {} without routing target",
                agent_name
            ));
        }
        routing.write_to_dir(&agent_config_dir).await?;
        let identity_context = metadata.identity.or_else(|| {
            Some(InvocationIdentityContext {
                runtime_agent_id: Some(agent_name.to_string()),
                slice_id: identity.as_ref().and_then(|record| record.slice_id.clone()),
                branch: identity
                    .as_ref()
                    .map(|record| record.birth_branch.to_string()),
                worktree: identity
                    .as_ref()
                    .map(|record| record.working_dir.to_string_lossy().into_owned()),
            })
        });
        let invocation = invocation::start_invocation_with_provenance_and_context(
            &agent_config_dir,
            metadata.runtime,
            metadata.trigger,
            routing,
            metadata.pr_number,
            metadata.head_sha,
            metadata.model,
            metadata.effort,
            identity_context,
        )
        .await?;
        let effective_routing = RoutingInfo::read_from_dir(&agent_config_dir).await?;
        if effective_routing.window_id.is_some() || effective_routing.pane_id.is_some() {
            match self.tmux() {
                Ok(tmux) => {
                    if let Err(error) = tmux
                        .set_routing_owner(
                            &effective_routing,
                            agent_name.as_str(),
                            &invocation.invocation_id,
                            invocation.generation,
                        )
                        .await
                    {
                        warn!(
                            agent = %agent_name,
                            %error,
                            "Could not persist tmux ownership; cleanup will remain conservative"
                        );
                    }
                }
                Err(error) => warn!(
                    agent = %agent_name,
                    %error,
                    "Could not create tmux client for ownership metadata; cleanup will remain conservative"
                ),
            }
        }
        if let Err(error) = fs::remove_file(agent_config_dir.join("exited_at")).await {
            if error.kind() != std::io::ErrorKind::NotFound {
                warn!(
                    agent = %agent_name,
                    %error,
                    "Failed to clear previous invocation tombstone"
                );
            }
        }
        if let Err(error) = fs::remove_file(agent_config_dir.join("exit_code")).await {
            if error.kind() != std::io::ErrorKind::NotFound {
                warn!(
                    agent = %agent_name,
                    %error,
                    "Failed to clear previous invocation exit code"
                );
            }
        }

        let now_secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let _ = fs::write(agent_config_dir.join("spawned_at"), now_secs.to_string()).await;
        let _ = fs::write(
            agent_config_dir.join(LAST_ACTIVITY_FILE),
            now_secs.to_string(),
        )
        .await;

        if let Some(issue_id) = issue_id_from_agent_name(agent_name.as_str()) {
            let _ = fs::write(agent_config_dir.join("active_issue"), issue_id.to_string()).await;
        }

        if let Some(record) = identity {
            if let Err(e) = self.agent_resolver().register(record).await {
                warn!(agent = %agent_name, error = %e, "Failed to register agent identity (non-fatal)");
            }
        }

        self.monitor_invocation(agent_config_dir.clone(), invocation);

        Ok(agent_config_dir)
    }

    /// Reconcile a tmux-owned invocation as soon as its exact target exits.
    ///
    /// The invocation ID and routing target are captured from the same record
    /// that was persisted at startup.  `finish_invocation_and_tombstone`
    /// rejects a later generation, so a delayed monitor cannot retire a fresh
    /// `resume_pr` invocation.
    fn monitor_invocation(&self, agent_dir: PathBuf, invocation: InvocationRecord) {
        if invocation.routing.window_id.is_none() && invocation.routing.pane_id.is_none() {
            return;
        }
        let Ok(tmux) = self.tmux() else {
            warn!(
                path = %agent_dir.display(),
                "Skipping invocation monitor because tmux is not configured"
            );
            return;
        };

        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(INVOCATION_MONITOR_INTERVAL);
            let mut consecutive_errors = 0u8;
            loop {
                ticker.tick().await;
                match crate::services::tmux_ipc::routing_target_alive(&invocation.routing, &tmux)
                    .await
                {
                    Ok(true) => {
                        consecutive_errors = 0;
                    }
                    Ok(false) => {
                        let exit_marker = tokio::fs::read_to_string(agent_dir.join("exit_code"))
                            .await
                            .ok();
                        let exit_code = exit_marker
                            .as_deref()
                            .and_then(|value| value.trim().parse::<i32>().ok());
                        let (status, classification, reason) = match exit_code {
                            Some(0) => {
                                (InvocationStatus::Exited, "clean_exit", "tmux_target_exited")
                            }
                            Some(_) => (
                                InvocationStatus::Failed,
                                "nonzero_exit",
                                "tmux_target_exited_with_nonzero_code",
                            ),
                            None => (
                                InvocationStatus::Killed,
                                "missing_exit_marker",
                                "tmux_target_exited_without_exit_marker",
                            ),
                        };
                        let exit_context = InvocationExitContext {
                            reason: Some(reason.to_string()),
                            classification: Some(classification.to_string()),
                            stderr_tail: read_invocation_stderr_tail(&agent_dir).await,
                        };
                        match invocation::finish_invocation_and_tombstone_with_context(
                            &agent_dir,
                            &invocation.routing,
                            status,
                            exit_code,
                            exit_context,
                        )
                        .await
                        {
                            Ok(InvocationFinishResult::IgnoredStale) => {
                                info!(
                                    path = %agent_dir.display(),
                                    invocation_id = %invocation.invocation_id,
                                    "Ignored exit from stale invocation generation"
                                );
                            }
                            Ok(InvocationFinishResult::Finished(_))
                            | Ok(InvocationFinishResult::Missing) => {
                                info!(
                                    path = %agent_dir.display(),
                                    invocation_id = %invocation.invocation_id,
                                    "Reconciled exited tmux invocation"
                                );
                            }
                            Err(error) => {
                                warn!(
                                    path = %agent_dir.display(),
                                    invocation_id = %invocation.invocation_id,
                                    %error,
                                    "Failed to reconcile exited tmux invocation"
                                );
                            }
                        }
                        break;
                    }
                    Err(error) => {
                        consecutive_errors = consecutive_errors.saturating_add(1);
                        warn!(
                            path = %agent_dir.display(),
                            invocation_id = %invocation.invocation_id,
                            consecutive_errors,
                            %error,
                            "Could not verify tmux invocation liveness"
                        );
                        if consecutive_errors >= 5 {
                            warn!(
                                path = %agent_dir.display(),
                                invocation_id = %invocation.invocation_id,
                                "Stopping invocation monitor after repeated tmux errors"
                            );
                            break;
                        }
                    }
                }
            }
        });
    }

    /// Refresh lifecycle activity for an already-live agent.
    ///
    /// This updates only the activity marker. Routing and identity files are
    /// intentionally left untouched so a resume cannot change message
    /// delivery or ownership.
    pub async fn refresh_agent_activity(&self, agent_name: &AgentName) -> Result<u64> {
        let agent_config_dir = self
            .project_dir()
            .join(".exo/agents")
            .join(agent_name.as_str());
        if !agent_config_dir.is_dir() {
            return Err(anyhow!(
                "cannot refresh activity for {}: agent registry directory is missing",
                agent_name
            ));
        }
        let now_secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        fs::write(
            agent_config_dir.join(LAST_ACTIVITY_FILE),
            now_secs.to_string(),
        )
        .await?;
        Ok(now_secs)
    }

    /// Initialize a standalone git repo at the given path.
    /// Creates the directory and runs `git init`, providing a .git boundary
    /// that prevents Claude's project discovery from traversing into the parent.
    pub(crate) async fn init_standalone_repo(&self, path: &Path) -> Result<()> {
        tokio::fs::create_dir_all(path).await?;
        let output = tokio::process::Command::new("git")
            .args(["init"])
            .current_dir(path)
            .output()
            .await?;
        if !output.status.success() {
            return Err(anyhow!(
                "git init failed at {}: {}",
                path.display(),
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        tracing::info!("Initialized standalone repo at {}", path.display());
        Ok(())
    }

    /// Resolve effective project dir for git operations.
    /// When subrepo is set, git operations target project_dir/subrepo instead.
    /// Validates that subrepo is relative and does not escape project_dir.
    pub(crate) fn effective_project_dir(&self, subrepo: Option<&Path>) -> Result<PathBuf> {
        match subrepo {
            Some(sub_path) => {
                if sub_path.is_absolute() {
                    return Err(anyhow!(
                        "subrepo path must be relative: {}",
                        sub_path.display()
                    ));
                }
                for component in sub_path.components() {
                    if matches!(component, std::path::Component::ParentDir) {
                        return Err(anyhow!(
                            "subrepo path cannot contain '..': {}",
                            sub_path.display()
                        ));
                    }
                }
                let dir = self.project_dir().join(sub_path);
                info!(subrepo = %sub_path.display(), effective_dir = %dir.display(), "Using subrepo for git operations");
                Ok(dir)
            }
            None => Ok(self.project_dir().to_path_buf()),
        }
    }

    /// Copy allowed directories into the agent's context.
    pub(crate) async fn copy_allowed_dirs(
        &self,
        target_dir: &Path,
        allowed_dirs: &[String],
    ) -> Result<()> {
        if allowed_dirs.is_empty() {
            return Ok(());
        }

        let context_dir = target_dir.join(".exo/context");
        fs::create_dir_all(&context_dir).await?;

        for dir_str in allowed_dirs {
            let dir_path = Path::new(dir_str);

            // Validation: Reject absolute paths and path traversal
            if dir_path.is_absolute() {
                tracing::error!("allowed_dir '{}' rejected: must be relative", dir_str);
                continue;
            }
            if dir_str.contains("..") {
                tracing::error!("allowed_dir '{}' rejected: cannot contain '..'", dir_str);
                continue;
            }

            let source_dir = self.project_dir().join(dir_path);

            // Canonicalize and verify the resolved path is within project_dir
            match source_dir.canonicalize() {
                Ok(canonical_source) => {
                    let canonical_project = self.project_dir().canonicalize()?;
                    if !canonical_source.starts_with(&canonical_project) {
                        tracing::error!("allowed_dir '{}' rejected: outside project_dir", dir_str);
                        continue;
                    }
                    if !canonical_source.is_dir() {
                        tracing::error!("allowed_dir '{}' rejected: not a directory", dir_str);
                        continue;
                    }

                    tracing::info!("Copying allowed_dir '{}' to agent context", dir_str);

                    // Recursive copy
                    let target_subdir = context_dir.join(dir_path);
                    self.copy_dir_recursive(&canonical_source, &target_subdir)
                        .await?;
                }
                Err(e) => {
                    tracing::error!("allowed_dir '{}' rejected: {}", dir_str, e);
                    continue;
                }
            }
        }

        Ok(())
    }

    pub(crate) async fn copy_dir_recursive(&self, src: &Path, dst: &Path) -> Result<()> {
        let mut stack = vec![(src.to_path_buf(), dst.to_path_buf())];

        while let Some((curr_src, curr_dst)) = stack.pop() {
            fs::create_dir_all(&curr_dst).await?;
            let mut entries = fs::read_dir(&curr_src).await?;
            while let Some(entry) = entries.next_entry().await? {
                let ty = entry.file_type().await?;
                let entry_path = entry.path();
                let dest_path = curr_dst.join(entry.file_name());
                if ty.is_dir() {
                    stack.push((entry_path, dest_path));
                } else {
                    fs::copy(&entry_path, &dest_path).await?;
                }
            }
        }
        Ok(())
    }
}

/// Resolve role context file with two-tier fallback: project-local > global.
///
/// Checks `.exo/roles/{wasm_name}/context/{role}.md` in the project directory first,
/// then falls back to `~/.exo/roles/{wasm_name}/context/{role}.md`.
pub fn resolve_role_context_path(
    project_dir: &Path,
    wasm_name: &str,
    role: &str,
) -> Option<PathBuf> {
    let local = project_dir.join(format!(".exo/roles/{}/context/{}.md", wasm_name, role));
    if local.exists() {
        return Some(local);
    }
    if let Ok(home) = std::env::var("HOME") {
        let global =
            PathBuf::from(home).join(format!(".exo/roles/{}/context/{}.md", wasm_name, role));
        if global.exists() {
            return Some(global);
        }
    }
    None
}

/// Load a role context file from the same project-local/global resolution used
/// when copying context into agent worktrees.
pub fn load_role_context(project_dir: &Path, wasm_name: &str, role: &str) -> Option<String> {
    let path = resolve_role_context_path(project_dir, wasm_name, role)?;
    let content = std::fs::read_to_string(path).ok()?;
    let content = strip_role_context_frontmatter(&content);
    (!content.is_empty()).then_some(content)
}

fn strip_role_context_frontmatter(content: &str) -> String {
    if !content.starts_with("---") {
        return content.trim().to_string();
    }
    content[3..]
        .find("---")
        .map(|end| content[3 + end + 3..].trim().to_string())
        .unwrap_or_else(|| content.trim().to_string())
}

/// Create a URL-safe slug from a title.
pub fn slugify(title: &str) -> String {
    title
        .to_lowercase()
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { '-' })
        .collect::<String>()
        .split('-')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("-")
        .chars()
        .take(50)
        .collect()
}

/// Remove accumulated harness markers from a human-facing slug.
///
/// Agent directories and branches retain one final runtime suffix, but a
/// resumed or respawned agent must never turn a multi-harness slug into a new
/// semantic slug.
pub fn normalize_agent_slug(value: &str) -> String {
    const HARNESS_SUFFIXES: [&str; 5] = ["claude", "shoal", "opencode", "codex", "process"];
    let mut normalized = slugify(value);
    while let Some((prefix, suffix)) = normalized.rsplit_once('-') {
        if !prefix.is_empty() && HARNESS_SUFFIXES.contains(&suffix) {
            normalized = prefix.to_string();
        } else {
            break;
        }
    }
    normalized
}

/// Extract issue ID from an agent name slug, e.g. "issue-26-loader-alias-fallback-codex" → Some(26).
pub(crate) fn issue_id_from_agent_name(slug: &str) -> Option<u64> {
    let rest = slug.strip_prefix("issue-")?;
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        None
    } else {
        digits.parse().ok()
    }
}

#[cfg(test)]
mod tests {
    // Legacy helpers used only by tests for issue-driven agent dir name parsing.
    #[derive(Debug, PartialEq)]
    pub(crate) struct ParsedAgentDirName<'a> {
        pub(crate) issue_id: &'a str,
        pub(crate) slug: &'a str,
        pub(crate) agent_type: Option<super::AgentType>,
    }

    pub(crate) fn parse_agent_dir_name(name: &str) -> Option<ParsedAgentDirName<'_>> {
        let rest = name.strip_prefix("gh-")?;
        let (issue_id, rest) = rest.split_once('-')?;
        let (slug, agent_suffix) = rest.rsplit_once('-')?;
        let agent_type = match agent_suffix {
            "claude" => Some(super::AgentType::Claude),
            "shoal" => Some(super::AgentType::Shoal),
            "opencode" => Some(super::AgentType::OpenCode),
            "codex" => Some(super::AgentType::Codex),
            "process" => Some(super::AgentType::Process),
            _ => None,
        };
        Some(ParsedAgentDirName {
            issue_id,
            slug,
            agent_type,
        })
    }
    use super::*;

    #[test]
    fn test_slugify() {
        assert_eq!(slugify("Fix the Bug"), "fix-the-bug");
        assert_eq!(slugify("Add new feature!"), "add-new-feature");
        assert_eq!(slugify("CamelCase"), "camelcase");
    }

    #[test]
    fn test_normalize_agent_slug_removes_accumulated_harnesses() {
        assert_eq!(
            normalize_agent_slug("Fix the Bug-codex-opencode"),
            "fix-the-bug"
        );
        assert_eq!(normalize_agent_slug("feature-codex"), "feature");
        assert_eq!(normalize_agent_slug("codex"), "codex");
        assert_eq!(normalize_agent_slug("feature"), "feature");
    }

    #[test]
    fn test_agent_type_command() {
        assert_eq!(AgentType::Claude.command(), "claude");
        assert_eq!(AgentType::Codex.command(), "codex");
    }

    #[test]
    fn test_agent_type_prompt_flag() {
        assert_eq!(AgentType::Claude.prompt_flag(), "");
    }

    #[test]
    fn test_agent_type_suffix() {
        assert_eq!(AgentType::Claude.suffix(), "claude");
        assert_eq!(AgentType::Codex.suffix(), "codex");
    }

    #[test]
    fn test_agent_type_default() {
        assert_eq!(AgentType::default(), AgentType::Codex);
    }

    #[test]
    fn test_retired_agent_type_fails_with_actionable_message() {
        let retired = ["ge", "mini"].concat();
        let error = serde_json::from_value::<AgentType>(serde_json::Value::String(retired))
            .expect_err("retired harness must fail closed");
        assert!(error.to_string().contains(AGENT_TYPE_DEPRECATION_MESSAGE));
    }

    #[test]
    fn test_agent_type_emoji() {
        assert_eq!(AgentType::Claude.emoji(), "🤖");
    }

    #[test]
    fn test_agent_type_display_name() {
        assert_eq!(
            AgentType::Claude.display_name("473", "refactor-polish"),
            "🤖 gh-473-refactor-polish"
        );
    }

    #[test]
    fn test_agent_type_display_name_no_truncation() {
        let long_slug = "this-is-a-very-long-slug-that-should-be-truncated";
        let display = AgentType::Claude.display_name("123", long_slug);
        assert_eq!(
            display,
            "🤖 gh-123-this-is-a-very-long-slug-that-should-be-truncated"
        );
    }

    #[test]
    fn test_agent_type_deserialization() {
        use serde_json;

        let claude: AgentType = serde_json::from_str("\"claude\"").unwrap();
        assert_eq!(claude, AgentType::Claude);

        let codex: AgentType = serde_json::from_str("\"codex\"").unwrap();
        assert_eq!(codex, AgentType::Codex);

        // Invalid agent type should fail at parse boundary
        let invalid = serde_json::from_str::<AgentType>("\"invalid\"");
        assert!(invalid.is_err());
    }

    #[test]
    fn test_parse_agent_dir_name_claude() {
        let parsed = parse_agent_dir_name("gh-123-fix-bug-claude").unwrap();
        assert_eq!(parsed.issue_id, "123");
        assert_eq!(parsed.slug, "fix-bug");
        assert_eq!(parsed.agent_type, Some(AgentType::Claude));
    }

    #[test]
    fn test_parse_agent_dir_name_codex() {
        let parsed = parse_agent_dir_name("gh-456-add-feature-codex").unwrap();
        assert_eq!(parsed.issue_id, "456");
        assert_eq!(parsed.slug, "add-feature");
        assert_eq!(parsed.agent_type, Some(AgentType::Codex));
    }

    #[test]
    fn test_parse_agent_dir_name_slug_with_hyphens() {
        let parsed = parse_agent_dir_name("gh-789-fix-the-big-bug-claude").unwrap();
        assert_eq!(parsed.issue_id, "789");
        assert_eq!(parsed.slug, "fix-the-big-bug");
        assert_eq!(parsed.agent_type, Some(AgentType::Claude));
    }

    #[test]
    fn test_parse_agent_dir_name_unknown_suffix() {
        let parsed = parse_agent_dir_name("gh-123-test-unknown").unwrap();
        assert_eq!(parsed.issue_id, "123");
        assert_eq!(parsed.slug, "test");
        assert_eq!(parsed.agent_type, None);
    }

    #[test]
    fn test_parse_agent_dir_name_invalid_format() {
        assert!(parse_agent_dir_name("123-test-claude").is_none());
        assert!(parse_agent_dir_name("gh-nohyphens").is_none());
        assert!(parse_agent_dir_name("gh-123").is_none());
    }

    #[tokio::test]
    async fn test_finalize_spawn_writes_codex_shared_dir_routing() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let git_wt = Arc::new(crate::services::git_worktree::GitWorktreeService::new(
            project_dir.clone(),
        ));
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.clone();
        services.git_wt = git_wt;
        services.agent_resolver = Arc::new(AgentResolver::load(project_dir.clone()).await);
        let service = AgentControlService::new(Arc::new(services));

        let agent_name = AgentName::try_from_str("issue-65-divergence-investigation-codex")
            .expect("literal validated string is non-empty");
        let parent_branch = BirthBranch::try_from_str("opencode-e2e")
            .expect("literal validated string is non-empty");
        let identity = AgentIdentityRecord {
            agent_name: agent_name.clone(),
            slug: Slug::try_from_str("issue-65-divergence-investigation")
                .expect("literal validated string is non-empty"),
            agent_type: AgentType::Codex,
            birth_branch: parent_branch.clone(),
            parent_branch,
            working_dir: PathBuf::from("."),
            display_name: AgentType::Codex.tab_display_name("issue-65-divergence-investigation"),
            topology: Topology::SharedDir,
            model: None,
            effort: None,
            ledger_owned: false,
            slice_id: None,
        };
        let pane_id = tmux_ipc::PaneId::parse("%42").unwrap();
        let agent_dir = service
            .finalize_spawn(
                &agent_name,
                RoutingInfo::pane(pane_id, "TL"),
                Some(identity),
            )
            .await
            .unwrap();

        let routing: serde_json::Value = serde_json::from_str(
            &tokio::fs::read_to_string(agent_dir.join("routing.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(routing["pane_id"], "%42");
        assert_eq!(routing["parent_tab"], "TL");
        assert!(agent_dir.join("identity.json").exists());
        assert_eq!(
            tokio::fs::read_to_string(agent_dir.join("active_issue"))
                .await
                .unwrap(),
            "65"
        );
        assert!(agent_dir.join(LAST_ACTIVITY_FILE).exists());
    }

    #[test]
    fn test_effective_model_for_override_wins_over_static_config() {
        let services = crate::services::Services::test();
        let service = AgentControlService::new(Arc::new(services))
            .with_spawn_agent_model(Some("config-default-model".to_string()))
            .with_reviewer_model(Some("config-reviewer-model".to_string()));

        // An explicit per-spawn override must win over the static per-role config.
        assert_eq!(
            service
                .effective_model_for(AgentType::OpenCode, "dev", Some("override-model"))
                .as_deref(),
            Some("override-model")
        );

        // Without an override, a dev leaf falls back to the static config default.
        assert_eq!(
            service
                .effective_model_for(AgentType::OpenCode, "dev", None)
                .as_deref(),
            Some("config-default-model")
        );

        // The reviewer role uses its own static config default.
        assert_eq!(
            service
                .effective_model_for(AgentType::OpenCode, "reviewer", None)
                .as_deref(),
            Some("config-reviewer-model")
        );

        // A whitespace-only override is treated as absent.
        assert_eq!(
            service
                .effective_model_for(AgentType::OpenCode, "dev", Some("   "))
                .as_deref(),
            Some("config-default-model")
        );
    }

    #[tokio::test]
    async fn refresh_agent_activity_preserves_routing_and_identity() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let git_wt = Arc::new(crate::services::git_worktree::GitWorktreeService::new(
            project_dir.clone(),
        ));
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.clone();
        services.git_wt = git_wt;
        services.agent_resolver = Arc::new(AgentResolver::load(project_dir.clone()).await);
        let service = AgentControlService::new(Arc::new(services));

        let agent_name = AgentName::try_from_str("resume-owner-codex").unwrap();
        let agent_dir = project_dir.join(".exo/agents").join(agent_name.as_str());
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        RoutingInfo::window(tmux_ipc::WindowId::parse("@42").unwrap())
            .write_to_dir(&agent_dir)
            .await
            .unwrap();
        tokio::fs::write(agent_dir.join("identity.json"), "valid identity")
            .await
            .unwrap();
        tokio::fs::write(agent_dir.join(LAST_ACTIVITY_FILE), "1")
            .await
            .unwrap();
        let routing_before = tokio::fs::read_to_string(agent_dir.join("routing.json"))
            .await
            .unwrap();
        let identity_before = tokio::fs::read_to_string(agent_dir.join("identity.json"))
            .await
            .unwrap();

        let refreshed_at = service.refresh_agent_activity(&agent_name).await.unwrap();
        let marker = tokio::fs::read_to_string(agent_dir.join(LAST_ACTIVITY_FILE))
            .await
            .unwrap()
            .trim()
            .parse::<u64>()
            .unwrap();

        assert_eq!(marker, refreshed_at);
        assert!(marker > 1);
        assert_eq!(
            tokio::fs::read_to_string(agent_dir.join("routing.json"))
                .await
                .unwrap(),
            routing_before
        );
        assert_eq!(
            tokio::fs::read_to_string(agent_dir.join("identity.json"))
                .await
                .unwrap(),
            identity_before
        );
    }

    // =========================================================================
    // resolve_working_dir tests
    // =========================================================================

    #[test]
    fn test_resolve_working_dir_root_branch() {
        // Root agents (no dots) resolve to project root
        assert_eq!(resolve_working_dir("main"), PathBuf::from("."));
        assert_eq!(resolve_working_dir("develop"), PathBuf::from("."));
        assert_eq!(resolve_working_dir("my-feature"), PathBuf::from("."));
    }

    #[test]
    fn test_resolve_working_dir_one_level() {
        // Single dot: last segment is the agent name (suffixed)
        assert_eq!(
            resolve_working_dir("main.feature-a-claude"),
            PathBuf::from(".exo/worktrees/feature-a-claude/")
        );
        assert_eq!(
            resolve_working_dir("main.remove-option-mcp-codex"),
            PathBuf::from(".exo/worktrees/remove-option-mcp-codex/")
        );
    }

    #[test]
    fn test_resolve_working_dir_nested() {
        // Multiple dots: agent name is always the LAST segment
        assert_eq!(
            resolve_working_dir("main.tui-port-2-claude.pdv-snapshot-enums-codex"),
            PathBuf::from(".exo/worktrees/pdv-snapshot-enums-codex/")
        );
        assert_eq!(
            resolve_working_dir("main.auth-claude.oauth-provider-codex"),
            PathBuf::from(".exo/worktrees/oauth-provider-codex/")
        );
        assert_eq!(
            resolve_working_dir("main.a.b.c.d"),
            PathBuf::from(".exo/worktrees/d/")
        );
    }

    #[test]
    fn test_resolve_working_dir_agent_name_uniqueness() {
        // Two different birth branches with the same agent name resolve to the same dir.
        // This is correct: same agent name = same directory (by design, collision).
        let dir_a = resolve_working_dir("main.tl-a-claude.my-feature-codex");
        let dir_b = resolve_working_dir("main.tl-b-claude.my-feature-codex");
        assert_eq!(
            dir_a, dir_b,
            "Same agent name = same worktree dir (by design)"
        );
    }

    // =========================================================================
    // resolve_worktree_from_tab tests
    // =========================================================================

    #[test]
    fn test_resolve_worktree_from_tab_tl() {
        assert_eq!(resolve_worktree_from_tab("TL"), PathBuf::from("."));
    }

    #[test]
    fn test_resolve_worktree_from_tab_emoji_agent_name() {
        assert_eq!(
            resolve_worktree_from_tab("🤖 feature-a-codex"),
            PathBuf::from(".exo/worktrees/feature-a-codex/")
        );
        assert_eq!(
            resolve_worktree_from_tab("🤖 auth-service-claude"),
            PathBuf::from(".exo/worktrees/auth-service-claude/")
        );
    }

    #[test]
    fn test_resolve_worktree_from_tab_no_space() {
        // No space separator: falls back to project root
        assert_eq!(resolve_worktree_from_tab("unknown"), PathBuf::from("."));
    }
}
