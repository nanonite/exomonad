// File PR service - creates/updates Forgejo PRs using the Forgejo REST API.

use crate::domain::{BirthBranch, BranchName, GithubOwner, GithubRepo, PRNumber};
use crate::services::forgejo::{ForgejoClient, ForgejoPullRequest};
use crate::services::git;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::repo;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tracing::{info, warn};

use super::tmux_events;

// ============================================================================
// Types
// ============================================================================

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct FilePRInput {
    pub title: String,
    pub body: String,
    pub base_branch: Option<BranchName>,
    pub working_dir: Option<String>,
    pub author_agent: Option<String>,
    pub author_role: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FilePROutput {
    pub pr_url: String,
    pub pr_number: PRNumber,
    pub head_branch: BranchName,
    pub base_branch: BranchName,
    pub created: bool,
}

/// Structured errors for file_pr operations.
#[derive(Debug, thiserror::Error)]
enum FilePrError {
    #[error("push failed: {0}")]
    Push(String),

    #[error("Forgejo PR create failed: {0}")]
    Create(String),

    #[error("Forgejo PR edit failed: {0}")]
    Update(String),

    #[error("Forgejo PR list failed: {0}")]
    List(String),
}

// ============================================================================
// Git Helpers
// ============================================================================

/// Resolve the base branch for a PR using the `BirthBranch` domain type.
///
/// Priority: explicit override > `BirthBranch::parent()` (dot hierarchy) > "main".
pub(crate) fn resolve_base_branch(head: &BranchName, explicit: Option<&BranchName>) -> BranchName {
    if let Some(base) = explicit {
        return base.clone();
    }
    BirthBranch::try_from_str(head.as_str())
        .expect("validated string input is non-empty")
        .parent()
        .map(|p| BranchName::try_from_str(p.as_str()).expect("validated string input is non-empty"))
        .unwrap_or_else(|| {
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        })
}

fn append_pr_body_metadata(
    body: &str,
    author_agent: Option<&str>,
    author_role: Option<&str>,
    head_branch: &BranchName,
) -> String {
    let extracted_agent = git::extract_agent_id(head_branch.as_str());
    let author_agent = author_agent
        .filter(|value| !value.trim().is_empty())
        .map(str::trim)
        .or(extracted_agent.as_deref())
        .unwrap_or_else(|| head_branch.as_str());
    let author_role = author_role
        .filter(|value| !value.trim().is_empty())
        .map(str::trim)
        .unwrap_or("dev");
    format!(
        "{}

---
Authoring-Agent: {}
Authoring-Role:  {}
Birth-Branch:    {}",
        body.trim_end(),
        author_agent,
        author_role,
        head_branch.as_str()
    )
}

// ============================================================================
// Forgejo operations
// ============================================================================

async fn find_existing_pr(
    forgejo: &ForgejoClient,
    owner: &GithubOwner,
    repo: &GithubRepo,
    head_branch: &BranchName,
) -> Result<Option<ForgejoPullRequest>, FilePrError> {
    forgejo
        .find_open_pull_request(owner, repo, head_branch)
        .await
        .map_err(|e| FilePrError::List(e.to_string()))
}

async fn update_pr(
    forgejo: &ForgejoClient,
    owner: &GithubOwner,
    repo: &GithubRepo,
    number: PRNumber,
    title: &str,
    body: &str,
    base: &BranchName,
) -> Result<(), FilePrError> {
    forgejo
        .update_pull_request(owner, repo, number, title, body, base)
        .await
        .map_err(|e| FilePrError::Update(e.to_string()))
}

async fn create_pr(
    forgejo: &ForgejoClient,
    owner: &GithubOwner,
    repo: &GithubRepo,
    title: &str,
    body: &str,
    base: &BranchName,
    head: &BranchName,
) -> Result<ForgejoPullRequest, FilePrError> {
    forgejo
        .create_pull_request(owner, repo, title, body, base, head)
        .await
        .map_err(|e| FilePrError::Create(e.to_string()))
}

// ============================================================================
// Main implementation
// ============================================================================

/// File a PR using the Forgejo REST API. Pushes the branch, creates or updates the PR.
pub async fn file_pr_async(
    input: &FilePRInput,
    git_wt: Arc<GitWorktreeService>,
    forgejo: &ForgejoClient,
) -> Result<FilePROutput> {
    let dir = input.working_dir.as_deref().unwrap_or(".");

    // Get branch from the agent's working directory, not server CWD
    let dir_path = std::path::PathBuf::from(dir);
    let git_wt_clone = git_wt.clone();
    let head_str =
        tokio::task::spawn_blocking(move || git_wt_clone.get_workspace_bookmark(&dir_path))
            .await
            .context("spawn_blocking failed")?
            .context("Failed to get workspace bookmark")?
            .ok_or_else(|| anyhow::anyhow!("No bookmark found for workspace at {}", dir))?;
    let head =
        BranchName::try_from_str(head_str.as_str()).expect("validated string input is non-empty");

    let base = resolve_base_branch(&head, input.base_branch.as_ref());
    let pr_body = append_pr_body_metadata(
        &input.body,
        input.author_agent.as_deref(),
        input.author_role.as_deref(),
        &head,
    );

    info!("[FilePR] head={} base={} dir={}", head, base, dir);

    // Push first
    {
        let dir_path = std::path::PathBuf::from(dir);
        let bookmark = head.clone();
        let git_wt_clone = git_wt.clone();
        let forgejo_token = forgejo.git_auth_token().map(str::to_owned);
        tokio::task::spawn_blocking(move || {
            git_wt_clone.push_bookmark_with_token(&dir_path, &bookmark, forgejo_token.as_deref())
        })
        .await
        .context("spawn_blocking failed")?
        .map_err(|e| FilePrError::Push(e.to_string()))?;
        info!("[FilePR] Pushed bookmark: {}", head);
    }

    let repo_info = repo::get_repo_info(dir).await?;

    // Check for existing PR
    let existing = find_existing_pr(forgejo, &repo_info.owner, &repo_info.repo, &head).await?;
    if let Some(pr) = existing {
        let pr_number = pr.number;
        info!("[FilePR] Updating existing PR #{}", pr_number);
        update_pr(
            forgejo,
            &repo_info.owner,
            &repo_info.repo,
            pr_number,
            &input.title,
            &pr_body,
            &base,
        )
        .await?;
        info!("[FilePR] Updated PR #{}: {}", pr_number, pr.url);
        return Ok(FilePROutput {
            pr_url: pr.url,
            pr_number,
            head_branch: pr.head_ref,
            base_branch: base,
            created: false,
        });
    }

    // Create new PR
    info!("[FilePR] Creating PR: {}", input.title);
    let pr = create_pr(
        forgejo,
        &repo_info.owner,
        &repo_info.repo,
        &input.title,
        &pr_body,
        &base,
        &head,
    )
    .await?;

    // Emit pr:filed event (only if in tmux session)
    if let Ok(session) = std::env::var("EXOMONAD_TMUX_SESSION") {
        if let Some(agent_id_str) = git::extract_agent_id(head.as_str()) {
            match crate::ui_protocol::AgentId::try_from(agent_id_str) {
                Ok(agent_id) => {
                    let event = crate::ui_protocol::AgentEvent::PrFiled {
                        agent_id,
                        pr_number: pr.number,
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
        pr_url: pr.url,
        pr_number: pr.number,
        head_branch: pr.head_ref,
        base_branch: base,
        created: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // =========================================================================
    // resolve_base_branch tests
    // =========================================================================

    #[test]
    fn test_resolve_base_branch_explicit_override() {
        let head =
            BranchName::try_from_str("main.feat").expect("literal validated string is non-empty");
        let explicit =
            BranchName::try_from_str("develop").expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, Some(&explicit)),
            BranchName::try_from_str("develop").expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_root_no_dots() {
        let head =
            BranchName::try_from_str("my-branch").expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_single_dot() {
        let head = BranchName::try_from_str("main.my-feature")
            .expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_double_dot() {
        let head = BranchName::try_from_str("main.auth-service.middleware")
            .expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main.auth-service")
                .expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_deep_nesting() {
        let head = BranchName::try_from_str("main.a.b.c.d.e")
            .expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main.a.b.c.d")
                .expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_agent_suffixed() {
        let head = BranchName::try_from_str("main.fix-auth-gemini")
            .expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_agent_suffixed_nested() {
        let head = BranchName::try_from_str("main.tl-auth-claude.fix-oauth-gemini")
            .expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main.tl-auth-claude")
                .expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_no_slash_convention() {
        // Slash convention is dead — no dots means fallback to "main"
        let head = BranchName::try_from_str("feature/my-work")
            .expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_none_explicit() {
        let head =
            BranchName::try_from_str("main.feat").expect("literal validated string is non-empty");
        assert_eq!(
            resolve_base_branch(&head, None),
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        );
    }

    #[test]
    fn test_resolve_base_branch_consistency_with_birth_branch() {
        // Verify resolve_base_branch matches BirthBranch::parent() for all cases
        let cases = [
            "main",
            "main.feat",
            "main.auth.middleware",
            "main.a.b.c.d.e",
            "main.fix-auth-gemini",
            "main.tl-auth-claude.fix-oauth-gemini",
        ];
        for case in &cases {
            let head =
                BranchName::try_from_str(*case).expect("validated string input is non-empty");
            let expected = BirthBranch::try_from_str(*case)
                .expect("validated string input is non-empty")
                .parent()
                .map(|p| {
                    BranchName::try_from_str(p.as_str())
                        .expect("validated string input is non-empty")
                })
                .unwrap_or_else(|| {
                    BranchName::try_from_str("main").expect("literal validated string is non-empty")
                });
            assert_eq!(
                resolve_base_branch(&head, None),
                expected,
                "Mismatch for branch '{}'",
                case
            );
        }
    }

    #[test]
    fn test_resolve_base_branch_depth_4() {
        assert_eq!(
            resolve_base_branch(
                &BranchName::try_from_str("main.core-eval.optimize.inline.inline-coalg")
                    .expect("literal validated string is non-empty"),
                None
            ),
            BranchName::try_from_str("main.core-eval.optimize.inline")
                .expect("literal validated string is non-empty")
        );
    }

    // =========================================================================
    // file_pr_async integration tests
    // =========================================================================

    #[tokio::test]
    async fn test_file_pr_async_no_git_repo() -> Result<()> {
        let temp_dir = tempfile::tempdir()?;
        let git_wt = Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf()));
        let input = FilePRInput {
            title: "Test PR".to_string(),
            body: "Test Body".to_string(),
            base_branch: None,
            working_dir: Some(temp_dir.path().to_string_lossy().to_string()),
            author_agent: Some("test-agent".to_string()),
            author_role: Some("dev".to_string()),
        };

        let forgejo = ForgejoClient::new("http://forgejo.local", "token").unwrap();
        let result = file_pr_async(&input, git_wt, forgejo.as_ref()).await;
        assert!(result.is_err());

        Ok(())
    }

    #[tokio::test]
    async fn test_file_pr_async_head_detection() -> Result<()> {
        let temp_dir = tempfile::tempdir()?;
        let dir = temp_dir.path();

        use std::process::Command;
        assert!(Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(dir)
            .status()?
            .success());
        assert!(Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(dir)
            .status()?
            .success());
        assert!(Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(dir)
            .status()?
            .success());
        std::fs::write(dir.join("README.md"), "test")?;
        assert!(Command::new("git")
            .args(["add", "README.md"])
            .current_dir(dir)
            .status()?
            .success());
        assert!(Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(dir)
            .status()?
            .success());

        assert!(Command::new("git")
            .args(["checkout", "-b", "feature-branch"])
            .current_dir(dir)
            .status()?
            .success());
        let git_wt = Arc::new(GitWorktreeService::new(dir.to_path_buf()));

        let input = FilePRInput {
            title: "Test PR".to_string(),
            body: "Test Body".to_string(),
            base_branch: None,
            working_dir: Some(dir.to_string_lossy().to_string()),
            author_agent: Some("test-agent".to_string()),
            author_role: Some("dev".to_string()),
        };

        let forgejo = ForgejoClient::new("http://forgejo.local", "token").unwrap();
        let result = file_pr_async(&input, git_wt, forgejo.as_ref()).await;

        if let Err(ref e) = result {
            let err_msg = e.to_string();
            assert!(
                err_msg.contains("push failed") || err_msg.contains("Failed to get remote URL"),
                "Expected push or remote error, got: {}",
                err_msg
            );
        } else {
            panic!("Expected file_pr_async to fail, but it succeeded?!");
        }

        Ok(())
    }

    #[tokio::test]
    async fn test_file_pr_async_base_branch_auto_detected() -> Result<()> {
        let temp_dir = tempfile::tempdir()?;
        let dir = temp_dir.path();

        use std::process::Command;
        assert!(Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(dir)
            .status()?
            .success());
        assert!(Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(dir)
            .status()?
            .success());
        assert!(Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(dir)
            .status()?
            .success());
        std::fs::write(dir.join("README.md"), "test")?;
        assert!(Command::new("git")
            .args(["add", "README.md"])
            .current_dir(dir)
            .status()?
            .success());
        assert!(Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(dir)
            .status()?
            .success());

        // Create a dot-separated branch (ExoMonad convention)
        assert!(Command::new("git")
            .args(["checkout", "-b", "main.feat-a-gemini"])
            .current_dir(dir)
            .status()?
            .success());

        let git_wt = Arc::new(GitWorktreeService::new(dir.to_path_buf()));
        let bookmark = git_wt.get_workspace_bookmark(dir)?;
        assert_eq!(bookmark, Some("main.feat-a-gemini".to_string()));

        // Verify base detection via resolve_base_branch
        let head = BranchName::try_from_str("main.feat-a-gemini")
            .expect("literal validated string is non-empty");
        let base = resolve_base_branch(&head, None);
        assert_eq!(
            base,
            BranchName::try_from_str("main").expect("literal validated string is non-empty")
        );

        Ok(())
    }
}
