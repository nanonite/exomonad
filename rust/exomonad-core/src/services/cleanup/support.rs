use super::types::*;
use crate::services::agent_control::Topology;
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::repo::RepositoryIdentity;
use anyhow::{bail, Context, Result};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::fs;
use tokio::process::Command;
use uuid::Uuid;

pub(super) fn requested_target_matches(
    target: Option<&str>,
    entry_name: &str,
    identity: &AgentIdentityRecord,
) -> bool {
    target.is_none_or(|target| {
        target == entry_name
            || target == identity.agent_name.as_str()
            || target == identity.slug.as_str()
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

pub(super) struct DecisionContext<'a> {
    pub(super) identity: &'a AgentIdentityRecord,
    pub(super) liveness: &'a CleanupLiveness,
    pub(super) dirty: Option<bool>,
    pub(super) protected: bool,
    pub(super) identity_drift: bool,
    pub(super) repository: Option<&'a RepositoryIdentity>,
    pub(super) repository_error: Option<&'a str>,
    pub(super) remote_error: Option<&'a str>,
    pub(super) pull_request: Option<&'a CleanupPullRequest>,
    pub(super) pr_error: Option<&'a str>,
    pub(super) head_matches_pull_request: Option<bool>,
}

pub(super) fn candidate_decision(context: DecisionContext<'_>) -> CleanupDecision {
    if context.identity.ledger_owned {
        return CleanupDecision::refusal("ledger-owned agent requires controller reconciliation");
    }
    if context.identity.topology == Topology::Unspecified {
        return CleanupDecision::refusal("agent topology is unspecified");
    }
    if context.identity_drift {
        return CleanupDecision::refusal("agent identity or worktree path drifted");
    }
    match context.liveness {
        CleanupLiveness::Live => return CleanupDecision::refusal("agent is live"),
        CleanupLiveness::Unknown => return CleanupDecision::refusal("agent liveness is unknown"),
        CleanupLiveness::Dead => {}
    }
    if context.dirty != Some(false) {
        return CleanupDecision::refusal("worktree is dirty or unavailable");
    }
    if context.protected {
        return CleanupDecision::refusal("branch is protected or is the current/base branch");
    }
    if context.identity.topology != Topology::WorktreePerAgent {
        return CleanupDecision::Cleanable;
    }
    if context.repository.is_none() {
        return CleanupDecision::refusal(
            context
                .repository_error
                .unwrap_or("repository identity is unavailable"),
        );
    }
    if let Some(error) = context.remote_error {
        return CleanupDecision::refusal(format!("configured remote is unavailable: {error}"));
    }
    if let Some(error) = context.pr_error {
        return CleanupDecision::refusal(format!(
            "pull-request ownership is not verified: {error}"
        ));
    }
    let Some(pr) = context.pull_request else {
        return CleanupDecision::refusal("pull-request ownership is not verified");
    };
    if !pr.merged {
        return CleanupDecision::refusal(format!(
            "pull request #{} is {} and not merged",
            pr.number,
            if pr.state.is_empty() {
                "closed"
            } else {
                pr.state.as_str()
            }
        ));
    }
    let Some(repository) = context.repository else {
        return CleanupDecision::refusal("repository identity is unavailable");
    };
    if pr.base_ref != repository.base_branch {
        return CleanupDecision::refusal("pull request targets a different base branch");
    }
    if pr.head_ref != context.identity.birth_branch.as_str() {
        return CleanupDecision::refusal("pull request head does not match the managed branch");
    }
    if context.head_matches_pull_request == Some(false) {
        return CleanupDecision::refusal("managed branch head differs from the pull-request head");
    }
    CleanupDecision::Cleanable
}

pub(super) fn refuse_duplicate_branches(candidates: &mut [CleanupCandidate]) {
    let mut counts = HashMap::new();
    for candidate in candidates
        .iter()
        .filter_map(|candidate| candidate.local_branch.as_ref())
    {
        *counts.entry(candidate.clone()).or_insert(0usize) += 1;
    }
    for candidate in candidates.iter_mut() {
        if candidate
            .local_branch
            .as_ref()
            .is_some_and(|branch| counts.get(branch).copied().unwrap_or_default() > 1)
        {
            candidate.decision =
                CleanupDecision::refusal("managed branch has ambiguous local ownership");
        }
    }
}

pub(super) fn receipt_entry(
    candidate: &CleanupCandidate,
    status: CleanupReceiptStatus,
    actions: Vec<String>,
    reason: Option<String>,
) -> CleanupReceiptEntry {
    CleanupReceiptEntry {
        candidate_id: candidate.id.clone(),
        status,
        actions,
        reason,
    }
}

pub(super) fn dry_run_receipt(plan: &CleanupPlan) -> CleanupReceipt {
    let entries = plan
        .candidates
        .iter()
        .map(|candidate| {
            let actions = if candidate.decision.is_cleanable() {
                let mut actions = Vec::new();
                if candidate.worktree_path.is_some() {
                    actions.push("remove_worktree".to_string());
                }
                actions.extend([
                    "remove_agent_directory".to_string(),
                    "deregister_identity".to_string(),
                ]);
                actions
            } else {
                Vec::new()
            };
            let status = if candidate.decision.is_cleanable() {
                CleanupReceiptStatus::WouldClean
            } else {
                CleanupReceiptStatus::Refused
            };
            receipt_entry(
                candidate,
                status,
                actions,
                candidate.decision.reason().map(ToOwned::to_owned),
            )
        })
        .collect();
    CleanupReceipt {
        schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
        operation_id: Uuid::new_v4().to_string(),
        plan_id: plan.plan_id.clone(),
        started_at: plan.generated_at,
        finished_at: unix_timestamp(),
        dry_run: true,
        entries,
    }
}

pub(super) fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
