use crate::domain::{BranchName, MergeStrategy, PRNumber};
use crate::services::forgejo::ForgejoClient;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::repo;
use anyhow::Result;
use std::sync::Arc;
use tokio::time::Duration;
use tracing::{error, info};

const MERGE_TIMEOUT: Duration = Duration::from_secs(120);

pub struct MergePROutput {
    pub success: bool,
    pub message: String,
    pub git_fetched: bool,
    pub branch_name: BranchName,
    pub head_sha: Option<String>,
}

/// Merge a PR using the Forgejo API.
pub async fn merge_pr_async(
    pr_number: PRNumber,
    strategy: &MergeStrategy,
    working_dir: &str,
    expected_base_sha: Option<&str>,
    expected_head_sha: Option<&str>,
    _expected_patch_digest: Option<&str>,
    _expected_merge_tree_sha: Option<&str>,
    git_wt: Arc<GitWorktreeService>,
    forgejo: Option<&ForgejoClient>,
) -> Result<MergePROutput> {
    let dir = if working_dir.is_empty() {
        "."
    } else {
        working_dir
    };

    info!(
        pr_number = pr_number.as_u64(),
        strategy = strategy.as_str(),
        working_dir = dir,
        "Merging Forgejo PR"
    );

    let Some(forgejo) = forgejo else {
        anyhow::bail!("Forgejo client is required for merge_pr");
    };
    let repo_info = repo::get_repo_info(dir).await?;
    let pr = forgejo
        .get_pull_request(&repo_info.owner, &repo_info.repo, pr_number)
        .await?;
    let branch_name = pr.head_ref.clone();
    let head_sha = pr.head_sha.clone();
    if let Some(expected) = expected_head_sha.filter(|value| !value.is_empty()) {
        if pr.head_sha.as_deref() != Some(expected) {
            return Ok(MergePROutput {
                success: false,
                message: format!(
                    "compare_and_swap: expected head {expected}, found {:?}",
                    pr.head_sha
                ),
                git_fetched: false,
                branch_name,
                head_sha,
            });
        }
    }
    if let Some(expected) = expected_base_sha.filter(|value| !value.is_empty()) {
        let output = tokio::process::Command::new("git")
            .args(["rev-parse", pr.base_ref.as_str()])
            .current_dir(dir)
            .output()
            .await?;
        let actual = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if !output.status.success() || actual != expected {
            return Ok(MergePROutput {
                success: false,
                message: format!("compare_and_swap: expected base {expected}, found {actual:?}"),
                git_fetched: false,
                branch_name,
                head_sha,
            });
        }
    }

    let method = match strategy {
        MergeStrategy::Squash => "squash",
        MergeStrategy::Merge => "merge",
        MergeStrategy::Rebase => "rebase",
    };
    let merge_result = tokio::time::timeout(
        MERGE_TIMEOUT,
        forgejo.merge_pull_request(&repo_info.owner, &repo_info.repo, pr_number, method),
    )
    .await;

    if let Err(error) = match merge_result {
        Ok(result) => result.map_err(|error| anyhow::anyhow!("Forgejo merge failed: {error}")),
        Err(_) => Err(anyhow::anyhow!(
            "Forgejo merge timed out after {}s",
            MERGE_TIMEOUT.as_secs()
        )),
    } {
        error!(error = %error, "Forgejo merge failed");
        return Ok(MergePROutput {
            success: false,
            message: error.to_string(),
            git_fetched: false,
            branch_name,
            head_sha,
        });
    }

    info!(pr_number = pr_number.as_u64(), "PR merged successfully");

    let dir_path = std::path::PathBuf::from(dir);
    let git_wt_clone = git_wt.clone();
    let git_result = tokio::task::spawn_blocking(move || git_wt_clone.fetch(&dir_path)).await;

    let git_fetched = match git_result {
        Ok(Ok(())) => {
            info!("git fetch succeeded");
            true
        }
        Ok(Err(e)) => {
            info!(error = %e, "git fetch failed");
            false
        }
        Err(e) => {
            info!(error = %e, "git fetch spawn_blocking failed");
            false
        }
    };

    Ok(MergePROutput {
        success: true,
        message: format!("PR #{} merged via {}", pr_number, strategy),
        git_fetched,
        branch_name,
        head_sha,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_strategy_strings_are_stable() {
        assert_eq!(MergeStrategy::Squash.as_str(), "squash");
        assert_eq!(MergeStrategy::Merge.as_str(), "merge");
        assert_eq!(MergeStrategy::Rebase.as_str(), "rebase");
    }
}
