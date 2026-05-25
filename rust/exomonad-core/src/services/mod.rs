pub mod acp_client;
pub mod acp_registry;
pub mod agent_control;
pub mod agent_inbox;
pub mod agent_resolver;
pub mod agent_resources;
pub mod claude_session_registry;
pub mod command;
pub mod complexity_classifier;
pub mod delivery;
pub mod event_log;
pub mod event_queue;
pub mod external;
pub mod file_pr;
pub mod filesystem;
pub mod forgejo;
pub mod forgejo_ci;
pub mod git;
pub mod git_worktree;
pub mod github;
pub mod inbox_watcher;
pub mod local;
pub mod log;
pub mod merge_pr;
pub mod mutex_registry;
pub mod orphan_reconciler;
pub mod pr_registry;
pub mod repo;
pub mod resilience;
pub mod review_policy;
pub mod secrets;
pub mod supervisor_registry;
pub mod synthetic_members;
pub mod tmux_events;
pub mod tmux_ipc;
pub mod tui_consumption;
pub mod worktree_event_watcher;

pub use self::acp_registry::AcpRegistry;
pub use self::agent_control::{
    resolve_role_context_path, resolve_working_dir, resolve_worktree_from_tab, AgentControlService,
    AgentInfo, AgentType, BatchCleanupResult, BatchSpawnResult, SpawnOptions, SpawnResult,
};
pub use self::agent_resolver::{AgentIdentityRecord, AgentResolver};
pub use self::claude_session_registry::ClaudeSessionRegistry;
pub use self::event_log::EventLog;
pub use self::event_queue::EventQueue;
pub use self::filesystem::FileSystemService;
pub use self::forgejo::ForgejoClient;
pub use self::git_worktree::GitWorktreeService;
pub use self::github::GitHubClient;
pub use self::mutex_registry::MutexRegistry;
pub use self::secrets::Secrets;
pub use self::supervisor_registry::SupervisorRegistry;
use claude_teams_bridge::TeamRegistry;
use std::path::PathBuf;
use std::sync::Arc;
use thiserror::Error;

// ============================================================================
// Capability Traits — each consumer declares only what it needs
// ============================================================================

pub trait HasTeamRegistry: Send + Sync {
    fn team_registry(&self) -> &TeamRegistry;
}
pub trait HasAcpRegistry: Send + Sync {
    fn acp_registry(&self) -> &AcpRegistry;
}
pub trait HasAgentResolver: Send + Sync {
    fn agent_resolver(&self) -> &AgentResolver;
}
pub trait HasEventQueue: Send + Sync {
    fn event_queue(&self) -> &EventQueue;
}
pub trait HasEventLog: Send + Sync {
    fn event_log(&self) -> Option<&EventLog>;
}
pub trait HasProjectDir: Send + Sync {
    fn project_dir(&self) -> &std::path::Path;
}
pub trait HasSupervisorRegistry: Send + Sync {
    fn supervisor_registry(&self) -> &SupervisorRegistry;
}
pub trait HasClaudeSessionRegistry: Send + Sync {
    fn claude_session_registry(&self) -> &ClaudeSessionRegistry;
}
pub trait HasMutexRegistry: Send + Sync {
    fn mutex_registry(&self) -> &MutexRegistry;
}
pub trait HasGitHubClient: Send + Sync {
    fn github_client(&self) -> Option<&Arc<GitHubClient>>;
}
pub trait HasForgejoClient: Send + Sync {
    fn forgejo_client(&self) -> Option<&Arc<ForgejoClient>>;
}

pub trait HasForgejoReviewerClient: Send + Sync {
    fn forgejo_reviewer_client(&self) -> Option<&Arc<ForgejoClient>>;
}
pub type CiStatusKey = (crate::domain::BranchName, String);
pub type CiStatusMap = std::collections::HashMap<CiStatusKey, crate::domain::CIStatus>;

pub trait HasGitWorktreeService: Send + Sync {
    fn git_worktree_service(&self) -> &Arc<GitWorktreeService>;
}
pub trait HasCiStatusMap: Send + Sync {
    fn ci_status_map(&self) -> &Arc<tokio::sync::RwLock<CiStatusMap>>;
}
/// Spawns a reviewer agent for a Forgejo PR. Implemented by `AgentControlService`.
///
/// Reviewer creation is owned by the Forgejo PR watcher for automatic review
/// rounds. TL/root roles may request explicit reviewer-only recovery when a PR
/// outlives its original author worktree.
///
/// Decouples `WorktreeEventWatcher` from the concrete `AgentControlService` type.
#[async_trait::async_trait]
pub trait ReviewerSpawner: Send + Sync {
    async fn spawn_reviewer_for_pr(&self, pr: &pr_registry::PrEntry) -> anyhow::Result<()>;
}

// ============================================================================
// Services — the concrete type that implements all traits
// ============================================================================

/// Shared services context, constructed once and threaded through the app.
///
/// Contains all shared registries and infrastructure services. Handlers and
/// services access what they need via Has* capability trait bounds.
#[derive(Clone)]
pub struct Services {
    pub project_dir: PathBuf,
    pub github_client: Option<Arc<GitHubClient>>,
    pub forgejo_client: Option<Arc<ForgejoClient>>,
    pub forgejo_reviewer_client: Option<Arc<ForgejoClient>>,
    pub event_log: Option<Arc<EventLog>>,
    pub team_registry: Arc<TeamRegistry>,
    pub acp_registry: Arc<AcpRegistry>,
    pub supervisor_registry: Arc<SupervisorRegistry>,
    pub claude_session_registry: Arc<ClaudeSessionRegistry>,
    pub agent_resolver: Arc<AgentResolver>,
    pub event_queue: Arc<EventQueue>,
    pub mutex_registry: Arc<MutexRegistry>,
    pub git_wt: Arc<GitWorktreeService>,
    /// Model for spawned OpenCode workers (passed to `opencode run --model`).
    /// `None` means let opencode pick.
    pub opencode_worker_model: Option<String>,
    /// Shared CI status map updated by CI integrations.
    /// Keyed by `(branch, sha)` so stale CI from an older push cannot satisfy a newer PR head.
    /// Shared with `WorktreeEventWatcher` so both the watcher and merge gate read from the same map.
    pub ci_status_map: Arc<tokio::sync::RwLock<CiStatusMap>>,
}

impl Services {
    /// Read the configured model for spawned OpenCode workers.
    pub fn opencode_worker_model(&self) -> Option<&str> {
        self.opencode_worker_model.as_deref()
    }
}

impl HasTeamRegistry for Services {
    fn team_registry(&self) -> &TeamRegistry {
        &self.team_registry
    }
}
impl HasAcpRegistry for Services {
    fn acp_registry(&self) -> &AcpRegistry {
        &self.acp_registry
    }
}
impl HasAgentResolver for Services {
    fn agent_resolver(&self) -> &AgentResolver {
        &self.agent_resolver
    }
}
impl HasEventQueue for Services {
    fn event_queue(&self) -> &EventQueue {
        &self.event_queue
    }
}
impl HasEventLog for Services {
    fn event_log(&self) -> Option<&EventLog> {
        self.event_log.as_deref()
    }
}
impl HasProjectDir for Services {
    fn project_dir(&self) -> &std::path::Path {
        &self.project_dir
    }
}
impl HasSupervisorRegistry for Services {
    fn supervisor_registry(&self) -> &SupervisorRegistry {
        &self.supervisor_registry
    }
}
impl HasClaudeSessionRegistry for Services {
    fn claude_session_registry(&self) -> &ClaudeSessionRegistry {
        &self.claude_session_registry
    }
}
impl HasMutexRegistry for Services {
    fn mutex_registry(&self) -> &MutexRegistry {
        &self.mutex_registry
    }
}
impl HasGitHubClient for Services {
    fn github_client(&self) -> Option<&Arc<GitHubClient>> {
        self.github_client.as_ref()
    }
}
impl HasForgejoClient for Services {
    fn forgejo_client(&self) -> Option<&Arc<ForgejoClient>> {
        self.forgejo_client.as_ref()
    }
}
impl HasForgejoReviewerClient for Services {
    fn forgejo_reviewer_client(&self) -> Option<&Arc<ForgejoClient>> {
        self.forgejo_reviewer_client.as_ref()
    }
}
impl HasGitWorktreeService for Services {
    fn git_worktree_service(&self) -> &Arc<GitWorktreeService> {
        &self.git_wt
    }
}
impl HasCiStatusMap for Services {
    fn ci_status_map(&self) -> &Arc<tokio::sync::RwLock<CiStatusMap>> {
        &self.ci_status_map
    }
}

#[cfg(test)]
impl Services {
    /// Construct a Services with empty/default registries for testing.
    pub fn test() -> Self {
        Self {
            project_dir: PathBuf::from("."),
            github_client: None,
            forgejo_client: None,
            forgejo_reviewer_client: None,
            event_log: None,
            team_registry: Arc::new(TeamRegistry::new()),
            acp_registry: Arc::new(AcpRegistry::new()),
            supervisor_registry: Arc::new(SupervisorRegistry::new()),
            claude_session_registry: Arc::new(ClaudeSessionRegistry::new()),
            agent_resolver: Arc::new(AgentResolver::empty()),
            event_queue: Arc::new(EventQueue::new()),
            mutex_registry: Arc::new(MutexRegistry::new()),
            git_wt: Arc::new(GitWorktreeService::new(PathBuf::from("."))),
            opencode_worker_model: None,
            ci_status_map: Arc::new(tokio::sync::RwLock::new(std::collections::HashMap::new())),
        }
    }
}

#[cfg(test)]
mod opencode_worker_model_tests {
    use super::*;

    #[test]
    fn accessor_returns_configured_model() {
        let mut s = Services::test();
        s.opencode_worker_model = Some("opencode-go/qwen3.6-plus".to_string());
        assert_eq!(s.opencode_worker_model(), Some("opencode-go/qwen3.6-plus"));
    }

    #[test]
    fn accessor_returns_none_when_unset() {
        let s = Services::test();
        assert_eq!(s.opencode_worker_model(), None);
    }
}

/// Errors that can occur during services validation.
///
/// These errors are returned by [`validate_git()`] and [`validate_gh_cli()`] when validating
/// service prerequisites (executables, paths, etc.).
#[derive(Debug, Error)]
pub enum ServicesError {
    /// Git executable not found on PATH (required for git operations).
    #[error("git executable not found on PATH")]
    GitNotFound,

    /// GitHub CLI (gh) not found on PATH (required when GitHub service is enabled).
    #[error("gh CLI not found on PATH (required when GitHub service is enabled)")]
    GhCliNotFound,

    /// Command execution failed during validation.
    #[error("command execution failed: {0}")]
    CommandFailed(String),
}

/// Validate that git is available on PATH.
pub fn validate_git() -> Result<(), ServicesError> {
    let git_check = std::process::Command::new("git").arg("--version").output();
    match git_check {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(ServicesError::GitNotFound),
        Err(e) => Err(ServicesError::CommandFailed(format!(
            "git --version: {}",
            e
        ))),
        Ok(output) if !output.status.success() => Err(ServicesError::CommandFailed(format!(
            "git --version exited with: {}",
            output.status
        ))),
        Ok(_) => Ok(()),
    }
}

/// Validate that gh CLI is available on PATH.
pub fn validate_gh_cli() -> Result<(), ServicesError> {
    let gh_check = std::process::Command::new("gh").arg("--version").output();
    match gh_check {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(ServicesError::GhCliNotFound),
        Err(e) => Err(ServicesError::CommandFailed(format!("gh --version: {}", e))),
        Ok(output) if !output.status.success() => Err(ServicesError::CommandFailed(format!(
            "gh --version exited with: {}",
            output.status
        ))),
        Ok(_) => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_services_error_display() {
        // Test error message formatting
        let err = ServicesError::GitNotFound;
        assert_eq!(err.to_string(), "git executable not found on PATH");

        let err = ServicesError::GhCliNotFound;
        assert_eq!(
            err.to_string(),
            "gh CLI not found on PATH (required when GitHub service is enabled)"
        );

        let err = ServicesError::CommandFailed("git --version failed".to_string());
        assert_eq!(
            err.to_string(),
            "command execution failed: git --version failed"
        );
    }
}
