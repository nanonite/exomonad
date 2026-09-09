pub(super) use super::decision::{
    candidate_decision, classify_routing_target, refuse_duplicate_branches, DecisionContext,
};
pub(super) use super::receipt_support::{
    dry_run_receipt, in_progress_receipt, receipt_entry, unix_timestamp,
};
use super::types::*;
use crate::domain::BranchName;
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
    checked_out_branch(project_dir).await.ok().flatten()
}

pub(super) async fn checked_out_branch(project_dir: &Path) -> Result<Option<String>> {
    let output = git_command(project_dir)
        .args(["branch", "--show-current"])
        .output()
        .await
        .context("read checked-out branch")?;
    if !output.status.success() {
        bail!(
            "read checked-out branch: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok((!branch.is_empty()).then_some(branch))
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
    validate_branch_arg(branch, "local branch")?;
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
    validate_branch_arg(branch, "remote branch")?;
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
    let output_text = String::from_utf8_lossy(&output.stdout);
    let mut matches = Vec::new();
    for line in output_text.lines().filter(|line| !line.trim().is_empty()) {
        let Some((sha, reference)) = parse_remote_branch_line(line) else {
            bail!("remote branch {remote}/{branch} returned malformed evidence");
        };
        if reference != ref_name {
            bail!("remote branch {remote}/{branch} returned conflicting evidence");
        }
        matches.push(sha);
    }
    match matches.as_slice() {
        [] => Ok(None),
        [sha] => Ok(Some((*sha).to_string())),
        _ => bail!("remote branch {remote}/{branch} has ambiguous heads"),
    }
}

pub(super) async fn fetch_target_branch(
    project_dir: &Path,
    remote: &str,
    branch: &str,
) -> Result<CleanupTargetBranch> {
    validate_branch_arg(branch, "configured target branch")?;
    let remote_ref = format!("refs/remotes/{remote}/{branch}");
    let refspec = format!("+refs/heads/{branch}:{remote_ref}");
    let output = git_command(project_dir)
        .args(["fetch", "--no-tags", remote])
        .arg(&refspec)
        .output()
        .await
        .with_context(|| format!("fetch configured target {remote}/{branch}"))?;
    if !output.status.success() {
        bail!(
            "fetch configured target {remote}/{branch}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let Some(head_sha) = local_ref_state(project_dir, &remote_ref).await? else {
        bail!("fetched configured target {remote}/{branch} has no head");
    };
    Ok(CleanupTargetBranch {
        remote_name: remote.to_string(),
        branch: branch.to_string(),
        head_sha,
    })
}

pub(super) async fn merge_commit_reachable(
    project_dir: &Path,
    merge_commit: &str,
    target: &CleanupTargetBranch,
) -> Result<bool> {
    validate_commit_arg(merge_commit)?;
    let target_ref = format!("refs/remotes/{}/{}", target.remote_name, target.branch);
    let output = git_command(project_dir)
        .args(["merge-base", "--is-ancestor", merge_commit, &target_ref])
        .output()
        .await
        .context("verify pull request merge reachability")?;
    if output.status.success() {
        Ok(true)
    } else if output.status.code() == Some(1) {
        Ok(false)
    } else {
        bail!(
            "verify pull request merge reachability: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
}

pub(super) async fn delete_local_branch(
    project_dir: &Path,
    branch: &str,
    expected_sha: &str,
) -> Result<()> {
    validate_branch_arg(branch, "managed local branch")?;
    validate_commit_arg(expected_sha).context("validate expected local branch head")?;
    let ref_name = format!("refs/heads/{branch}");
    let output = git_command(project_dir)
        .args(["update-ref", "-d", &ref_name, expected_sha])
        .output()
        .await
        .context("delete managed local branch")?;
    if !output.status.success() {
        bail!(
            "delete managed local branch {branch}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

pub(super) async fn delete_remote_branch_with_lease(
    project_dir: &Path,
    remote: &str,
    branch: &str,
    expected_sha: &str,
) -> Result<()> {
    validate_branch_arg(branch, "managed remote branch")?;
    validate_commit_arg(expected_sha).context("validate expected remote branch head")?;
    let lease = format!("--force-with-lease=refs/heads/{branch}:{expected_sha}");
    let refspec = format!(":refs/heads/{branch}");
    let output = git_command(project_dir)
        .args(["push", remote, &lease, &refspec])
        .output()
        .await
        .with_context(|| format!("delete remote branch {remote}/{branch}"))?;
    if !output.status.success() {
        bail!(
            "delete remote branch {remote}/{branch} with expected head {expected_sha}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

pub(super) fn path_exists_sync(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

fn validate_branch_arg(branch: &str, label: &str) -> Result<()> {
    BranchName::try_from_str(branch).with_context(|| format!("validate {label}"))?;
    if branch.starts_with('-')
        || branch.ends_with('/')
        || branch.ends_with('.')
        || branch.contains("..")
        || branch.contains("@{")
        || branch.contains("//")
        || branch
            .chars()
            .any(|character| matches!(character, ' ' | '~' | '^' | ':' | '?' | '*' | '[' | '\\'))
        || branch
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        || branch.split('/').any(|part| part.ends_with(".lock"))
    {
        bail!("{label} is not a valid Git branch name");
    }
    Ok(())
}

fn validate_commit_arg(commit: &str) -> Result<()> {
    if !matches!(commit.len(), 40 | 64)
        || !commit
            .chars()
            .all(|character| character.is_ascii_hexdigit())
    {
        bail!("commit SHA is invalid");
    }
    Ok(())
}

async fn local_ref_state(project_dir: &Path, ref_name: &str) -> Result<Option<String>> {
    let output = git_command(project_dir)
        .args(["rev-parse", "--verify", ref_name])
        .output()
        .await
        .context("read git ref")?;
    if !output.status.success() {
        return Ok(None);
    }
    Ok(Some(
        String::from_utf8_lossy(&output.stdout).trim().to_string(),
    ))
}

fn parse_remote_branch_line(line: &str) -> Option<(&str, &str)> {
    let mut fields = line.split_whitespace();
    let sha = fields.next()?;
    let reference = fields.next()?;
    (fields.next().is_none() && !sha.is_empty()).then_some((sha, reference))
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
