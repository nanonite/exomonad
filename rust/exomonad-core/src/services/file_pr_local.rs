// Local PR registry service — replaces GitHub API calls with .exo/prs.json
//
// Mirrors file_pr_async() but reads/writes a local JSON registry instead
// of calling octocrab GitHub API. Push bookmark remains local git.

use crate::domain::{AgentName, BranchName, PRNumber, Role};
use crate::services::git;
use crate::services::git_worktree::GitWorktreeService;
use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tracing::{info, warn};

use super::file_pr::{FilePRInput, FilePROutput};
use super::tmux_events;

// ============================================================================
// Push Remote Resolution
// ============================================================================

/// Resolve which git remote to push to.
///
/// Checks whether a `tangled` remote is configured in the workspace.
/// If present, returns `"tangled"` so CI runs on the local knot.
/// Falls back to `"origin"` when no tangled remote is configured.
pub(crate) fn resolve_push_remote(workspace_path: &std::path::Path) -> &'static str {
    let ok = std::process::Command::new("git")
        .args(["remote", "get-url", "tangled"])
        .current_dir(workspace_path)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    if ok { "tangled" } else { "origin" }
}

// ============================================================================
// Registry Types
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PrState {
    Open,
    Merged,
    Closed,
    Stuck,
}

impl Default for PrState {
    fn default() -> Self {
        Self::Open
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LocalReviewState {
    PendingReview,
    ChangesRequested,
    Approved,
}

impl Default for LocalReviewState {
    fn default() -> Self {
        Self::PendingReview
    }
}

/// A single PR entry in the local registry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrEntry {
    pub number: u64,
    pub head_branch: String,
    pub base_branch: String,
    pub title: String,
    pub body: String,
    pub author_agent: String,
    pub author_role: String,
    pub created_at: DateTime<Utc>,
    #[serde(default)]
    pub state: PrState,
    #[serde(default)]
    pub review_state: LocalReviewState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_review_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_head_sha: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reviewer_agent: Option<String>,
    #[serde(default)]
    pub rounds: u32,
    #[serde(default)]
    pub stuck: bool,
    #[serde(default)]
    pub needs_human_review: bool,
}

/// Local PR registry stored at `.exo/prs.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrRegistry {
    pub prs: HashMap<u64, PrEntry>,
    #[serde(default = "default_next_number")]
    pub next_number: u64,
}

fn default_next_number() -> u64 {
    1
}

impl Default for PrRegistry {
    fn default() -> Self {
        Self {
            prs: HashMap::new(),
            next_number: 1,
        }
    }
}

impl PrRegistry {
    /// Find a PR by its head_branch name.
    pub fn find_by_branch(&self, head_branch: &BranchName) -> Option<&PrEntry> {
        let branch_str = head_branch.as_str();
        self.prs.values().find(|pr| pr.head_branch == branch_str)
    }
}

// ============================================================================
// Registry I/O
// ============================================================================

pub async fn read_pr_registry(prs_path: &std::path::Path) -> Result<PrRegistry> {
    if !prs_path.exists() {
        return Ok(PrRegistry::default());
    }
    let data = tokio::fs::read_to_string(prs_path)
        .await
        .context("Failed to read prs.json")?;
    let registry: PrRegistry =
        serde_json::from_str(&data).context("Failed to parse prs.json")?;
    Ok(registry)
}

pub(crate) async fn write_pr_registry(prs_path: &std::path::Path, registry: &PrRegistry) -> Result<()> {
    if let Some(parent) = prs_path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp_path = prs_path.with_extension("tmp");
    let json = serde_json::to_vec_pretty(registry)?;
    tokio::fs::write(&tmp_path, &json).await?;
    tokio::fs::rename(&tmp_path, prs_path).await?;
    Ok(())
}

// ============================================================================
// Authoring Footer
// ============================================================================

fn append_authoring_footer(
    body: &str,
    agent_id: &AgentName,
    role: &Role,
    head_branch: &BranchName,
) -> String {
    format!(
        "{}\n\n---\nAuthoring-Agent: {}\nAuthoring-Role:  {}\nBirth-Branch:    {}",
        body.trim_end(),
        agent_id.as_str(),
        role.as_str(),
        head_branch.as_str(),
    )
}

// ============================================================================
// Git Helpers (shared with file_pr.rs)
// ============================================================================

use super::file_pr::resolve_base_branch;

// ============================================================================
// Main Implementation
// ============================================================================

/// File a PR locally using `.exo/prs.json`. Pushes the branch, creates or
/// updates the local PR registry entry.
///
/// `project_dir` is the root of the exomonad project (where `.exo/` lives).
pub async fn file_pr_local(
    input: &FilePRInput,
    git_wt: Arc<GitWorktreeService>,
    project_dir: &std::path::Path,
    agent_role: &Role,
    agent_name: &AgentName,
) -> Result<FilePROutput> {
    let dir = input.working_dir.as_deref().unwrap_or(".");
    let dir_path = std::path::PathBuf::from(dir);

    // Get branch from the agent's working directory
    let git_wt_clone = git_wt.clone();
    let head_str = tokio::task::spawn_blocking(move || git_wt_clone.get_workspace_bookmark(&dir_path))
        .await
        .context("spawn_blocking failed")?
        .context("Failed to get workspace bookmark")?
        .ok_or_else(|| anyhow::anyhow!("No bookmark found for workspace at {}", dir))?;
    let head = BranchName::from(head_str.as_str());
    let base = resolve_base_branch(&head, input.base_branch.as_ref());

    info!("[FilePRLocal] head={} base={} dir={}", head, base, dir);

    // Push to tangled remote if configured, otherwise origin.
    {
        let dir_path = std::path::PathBuf::from(dir);
        let remote = resolve_push_remote(&dir_path);
        info!("[FilePRLocal] Push remote: {}", remote);
        let bookmark = head.clone();
        let git_wt_clone = git_wt.clone();
        let remote_str = remote.to_string();
        tokio::task::spawn_blocking(move || {
            git_wt_clone.push_to_remote(&dir_path, &bookmark, &remote_str)
        })
        .await
        .context("spawn_blocking failed")?
        .context("push failed")?;
        info!("[FilePRLocal] Pushed bookmark {} to {}", head, remote);
    }

    let prs_path = project_dir.join(".exo/prs.json");
    let mut registry = read_pr_registry(&prs_path).await?;

    // Check for existing PR on this head_branch
    let existing_number = registry
        .find_by_branch(&head)
        .map(|pr| pr.number);
    if let Some(number) = existing_number {
        let pr_number = PRNumber::new(number);
        info!("[FilePRLocal] Updating existing PR #{}", pr_number);

        // Update the PR entry
        if let Some(entry) = registry.prs.get_mut(&number) {
            entry.title = input.title.clone();
            entry.body = append_authoring_footer(&input.body, agent_name, agent_role, &head);
            entry.base_branch = base.to_string();
        }

        write_pr_registry(&prs_path, &registry).await?;
        info!("[FilePRLocal] Updated PR #{}", pr_number);

        return Ok(FilePROutput {
            pr_url: String::new(),
            pr_number,
            head_branch: head,
            base_branch: base,
            created: false,
        });
    }

    // Create new PR
    let number = registry.next_number;
    registry.next_number += 1;

    let entry = PrEntry {
        number,
        head_branch: head.to_string(),
        base_branch: base.to_string(),
        title: input.title.clone(),
        body: append_authoring_footer(&input.body, agent_name, agent_role, &head),
        author_agent: agent_name.to_string(),
        author_role: agent_role.to_string(),
        created_at: Utc::now(),
        state: PrState::Open,
        review_state: LocalReviewState::PendingReview,
        last_review_at: None,
        last_head_sha: None,
        reviewer_agent: None,
        rounds: 0,
        stuck: false,
        needs_human_review: false,
    };

    let pr_number = PRNumber::new(number);
    registry.prs.insert(number, entry);
    write_pr_registry(&prs_path, &registry).await?;

    // Emit pr:filed event
    if let Ok(session) = std::env::var("EXOMONAD_TMUX_SESSION") {
        if let Some(agent_id_str) = git::extract_agent_id(head.as_str()) {
            match crate::ui_protocol::AgentId::try_from(agent_id_str) {
                Ok(agent_id) => {
                    let event = crate::ui_protocol::AgentEvent::PrFiled {
                        agent_id,
                        pr_number,
                        timestamp: tmux_events::now_iso8601(),
                    };
                    if let Err(e) = tmux_events::emit_event(&session, &event) {
                        warn!("Failed to emit pr:filed event: {}", e);
                    }
                }
                Err(e) => {
                    warn!(
                        "Invalid agent_id in branch '{}', skipping event: {}",
                        head, e
                    );
                }
            }
        }
    }

    Ok(FilePROutput {
        pr_url: String::new(),
        pr_number,
        head_branch: head,
        base_branch: base,
        created: true,
    })
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::BranchName;
    use std::process::Command;
    use tempfile::TempDir;

    fn test_agent() -> AgentName {
        AgentName::from("test-agent-gemini")
    }

    fn test_role() -> Role {
        Role::dev()
    }

    fn init_git_repo() -> TempDir {
        let tmp = TempDir::new().unwrap();
        let run = |args: &[&str]| {
            let status = Command::new("git")
                .args(args)
                .current_dir(tmp.path())
                .status()
                .expect("git failed");
            assert!(status.success(), "git {:?} failed", args);
        };
        run(&["init"]);
        run(&["config", "user.email", "test@example.com"]);
        run(&["config", "user.name", "Test User"]);
        run(&["commit", "--allow-empty", "-m", "Initial commit"]);
        tmp
    }

    #[test]
    fn test_resolve_push_remote_no_tangled_returns_origin() {
        let tmp = init_git_repo();
        assert_eq!(resolve_push_remote(tmp.path()), "origin");
    }

    #[test]
    fn test_resolve_push_remote_tangled_configured_returns_tangled() {
        let tmp = init_git_repo();
        Command::new("git")
            .args(["remote", "add", "tangled", "git@local-tangled:repositories/owner/test.git"])
            .current_dir(tmp.path())
            .status()
            .unwrap();
        assert_eq!(resolve_push_remote(tmp.path()), "tangled");
    }

    #[test]
    fn test_resolve_push_remote_non_git_dir_returns_origin() {
        let tmp = TempDir::new().unwrap();
        assert_eq!(resolve_push_remote(tmp.path()), "origin");
    }

    #[test]
    fn test_registry_default() {
        let reg = PrRegistry::default();
        assert_eq!(reg.next_number, 1);
        assert!(reg.prs.is_empty());
    }

    #[test]
    fn test_find_by_branch() {
        let mut reg = PrRegistry::default();
        reg.prs.insert(
            1,
            PrEntry {
                number: 1,
                head_branch: "main.feat-gemini".into(),
                base_branch: "main".into(),
                title: "Test PR".into(),
                body: "Test body".into(),
                author_agent: "feat-gemini".into(),
                author_role: "dev".into(),
                created_at: Utc::now(),
                state: PrState::Open,
                review_state: LocalReviewState::PendingReview,
                last_review_at: None,
                last_head_sha: None,
                reviewer_agent: None,
                rounds: 0,
                stuck: false,
                needs_human_review: false,
            },
        );

        let found = reg.find_by_branch(&BranchName::from("main.feat-gemini"));
        assert!(found.is_some());
        assert_eq!(found.unwrap().number, 1);

        let not_found = reg.find_by_branch(&BranchName::from("nonexistent"));
        assert!(not_found.is_none());
    }

    #[test]
    fn test_append_authoring_footer() {
        let body = "This is a test PR body.";
        let result = append_authoring_footer(
            body,
            &AgentName::from("feat-gemini"),
            &Role::dev(),
            &BranchName::from("main.feat-gemini"),
        );
        assert!(result.contains("Authoring-Agent: feat-gemini"));
        assert!(result.contains("Authoring-Role:  dev"));
        assert!(result.contains("Birth-Branch:    main.feat-gemini"));
        assert!(result.starts_with("This is a test PR body."));
    }

    #[test]
    fn test_registry_serialization_roundtrip() {
        let mut reg = PrRegistry::default();
        reg.prs.insert(
            1,
            PrEntry {
                number: 1,
                head_branch: "main.feat-gemini".into(),
                base_branch: "main".into(),
                title: "Test".into(),
                body: "Body".into(),
                author_agent: "feat-gemini".into(),
                author_role: "dev".into(),
                created_at: Utc::now(),
                state: PrState::Open,
                review_state: LocalReviewState::PendingReview,
                last_review_at: None,
                last_head_sha: None,
                reviewer_agent: None,
                rounds: 0,
                stuck: false,
                needs_human_review: false,
            },
        );
        reg.next_number = 2;

        let json = serde_json::to_string_pretty(&reg).unwrap();
        let deserialized: PrRegistry = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.next_number, 2);
        assert_eq!(deserialized.prs.len(), 1);
        assert_eq!(deserialized.prs[&1].title, "Test");
    }

    #[tokio::test]
    async fn test_read_write_registry_roundtrip() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let prs_path = tmp.path().join("prs.json");

        let mut reg = PrRegistry::default();
        reg.prs.insert(
            1,
            PrEntry {
                number: 1,
                head_branch: "main.feat-gemini".into(),
                base_branch: "main".into(),
                title: "Test".into(),
                body: "Body".into(),
                author_agent: "feat-gemini".into(),
                author_role: "dev".into(),
                created_at: Utc::now(),
                state: PrState::Open,
                review_state: LocalReviewState::PendingReview,
                last_review_at: None,
                last_head_sha: None,
                reviewer_agent: None,
                rounds: 0,
                stuck: false,
                needs_human_review: false,
            },
        );

        write_pr_registry(&prs_path, &reg).await?;

        let read_back = read_pr_registry(&prs_path).await?;
        assert_eq!(read_back.prs.len(), 1);
        assert_eq!(read_back.prs[&1].title, "Test");
        assert_eq!(read_back.next_number, 1);

        Ok(())
    }

    #[tokio::test]
    async fn test_read_nonexistent_registry_returns_default() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let prs_path = tmp.path().join("nonexistent.json");
        let reg = read_pr_registry(&prs_path).await?;
        assert_eq!(reg.next_number, 1);
        assert!(reg.prs.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn test_file_pr_local_no_git_repo() -> Result<()> {
        let temp_dir = tempfile::tempdir()?;
        let git_wt = Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf()));
        let input = FilePRInput {
            title: "Test PR".to_string(),
            body: "Test Body".to_string(),
            base_branch: None,
            working_dir: Some(temp_dir.path().to_string_lossy().to_string()),
        };

        let result = file_pr_local(&input, git_wt, temp_dir.path(), &test_role(), &test_agent()).await;
        assert!(result.is_err());
        Ok(())
    }
}
