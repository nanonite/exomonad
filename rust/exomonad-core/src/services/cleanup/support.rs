pub(super) use super::decision::{
    candidate_decision, classify_routing_target, refuse_duplicate_branches, DecisionContext,
};
pub(super) use super::receipt_support::{
    dry_run_receipt, in_progress_receipt, receipt_entry, unix_timestamp,
};
use crate::services::agent_resolver::AgentIdentityRecord;
use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};
use tokio::fs;
use tokio::process::Command;

pub(super) const DEREGISTER_PENDING: &str = "deregister_identity_pending";
pub(super) const PROGRESS_PERSISTENCE_FAILURE: &str = "persist cleanup progress";

pub(super) fn requested_target_matches(
    target: Option<&str>,
    entry_name: &str,
    identity: Option<&AgentIdentityRecord>,
) -> bool {
    target.is_none_or(|target| {
        target == entry_name
            || identity.is_some_and(|identity| {
                target == identity.agent_name.as_str() || target == identity.slug.as_str()
            })
    })
}

pub(super) fn resolve_path(project_dir: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        project_dir.join(path)
    }
}

pub(super) fn path_within(parent: &Path, child: &Path) -> bool {
    child == parent || child.strip_prefix(parent).is_ok()
}

pub(super) async fn current_branch(project_dir: &Path) -> Option<String> {
    let output = git_command(project_dir)
        .args(["branch", "--show-current"])
        .output()
        .await
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).trim().to_string())
}

pub(super) async fn read_active_issue(agent_dir: &Path) -> Option<String> {
    let value = fs::read_to_string(agent_dir.join("active_issue"))
        .await
        .ok()?;
    let value = value.trim();
    (!value.is_empty() && value.chars().all(|character| character.is_ascii_digit()))
        .then(|| value.to_string())
}

pub(super) async fn workspace_git_root(worktree: &Path) -> Result<Option<PathBuf>> {
    let output = git_command(worktree)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .await
        .context("read worktree git root")?;
    if !output.status.success() {
        return Ok(None);
    }
    Ok(Some(
        fs::canonicalize(String::from_utf8_lossy(&output.stdout).trim()).await?,
    ))
}

pub(super) async fn workspace_branch(worktree: &Path) -> Result<Option<String>> {
    let output = git_command(worktree)
        .args(["branch", "--show-current"])
        .output()
        .await
        .context("read worktree branch")?;
    if !output.status.success() {
        bail!("read worktree branch failed");
    }
    let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok((!branch.is_empty()).then_some(branch))
}

pub(super) async fn workspace_dirty(worktree: &Path) -> Result<bool> {
    let output = git_command(worktree)
        .args(["status", "--porcelain", "--untracked-files=all"])
        .output()
        .await
        .context("read worktree status")?;
    if !output.status.success() {
        bail!("read worktree status failed");
    }
    Ok(!String::from_utf8_lossy(&output.stdout).trim().is_empty())
}

pub(super) async fn local_branch_state(project_dir: &Path, branch: &str) -> Result<Option<String>> {
    let ref_name = format!("refs/heads/{branch}");
    let output = git_command(project_dir)
        .args(["rev-parse", "--verify", &ref_name])
        .output()
        .await
        .context("read local branch")?;
    if !output.status.success() {
        return Ok(None);
    }
    Ok(Some(
        String::from_utf8_lossy(&output.stdout).trim().to_string(),
    ))
}

pub(super) async fn remote_branch_state(
    project_dir: &Path,
    remote: &str,
    branch: &str,
) -> Result<Option<String>> {
    let ref_name = format!("refs/heads/{branch}");
    let output = git_command(project_dir)
        .args(["ls-remote", "--heads", remote, &ref_name])
        .output()
        .await
        .with_context(|| format!("read remote branch {remote}/{branch}"))?;
    if !output.status.success() {
        bail!(
            "read remote branch {remote}/{branch}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let line = String::from_utf8_lossy(&output.stdout);
    Ok(line
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().next())
        .filter(|sha| !sha.is_empty())
        .map(ToOwned::to_owned))
}

pub(super) fn git_command(directory: &Path) -> Command {
    let mut command = Command::new("git");
    command
        .current_dir(directory)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GCM_INTERACTIVE", "never")
        .env("GIT_ASKPASS", "")
        .env("SSH_ASKPASS_REQUIRE", "never");
    command
}
