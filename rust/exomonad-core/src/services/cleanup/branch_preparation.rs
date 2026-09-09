use super::branch_validation::RevalidatedBranch;
use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use anyhow::Result;

pub(super) enum RemoteBranchDeletion {
    AlreadyAbsent,
    Present(RevalidatedBranch),
}

pub(super) enum LocalBranchDeletion {
    AlreadyAbsent,
    Present(RevalidatedBranch),
}

impl VerifiedCleanupService {
    pub(super) async fn prepare_remote_branch_deletion(
        &self,
        candidate: &CleanupCandidate,
        expected: &str,
    ) -> Result<RemoteBranchDeletion> {
        let branch = self.revalidate_branch(candidate).await?;
        verify_planned_heads(candidate, &branch, expected)?;
        let remote_head = remote_branch_state(
            &self.project_dir,
            &branch.repository.remote_name,
            &branch.branch,
        )
        .await?;
        match remote_head {
            None => Ok(RemoteBranchDeletion::AlreadyAbsent),
            Some(observed) if observed == expected => Ok(RemoteBranchDeletion::Present(branch)),
            Some(observed) => anyhow::bail!(
                "remote branch head conflict: expected {expected}, observed {observed}"
            ),
        }
    }

    pub(super) async fn prepare_local_branch_deletion(
        &self,
        candidate: &CleanupCandidate,
        expected: &str,
    ) -> Result<LocalBranchDeletion> {
        let branch = self.revalidate_branch(candidate).await?;
        if worktree_exists(candidate) {
            anyhow::bail!("managed worktree reappeared before local branch deletion");
        }
        match branch.local_head.as_deref() {
            None => Ok(LocalBranchDeletion::AlreadyAbsent),
            Some(observed) if observed == expected => Ok(LocalBranchDeletion::Present(branch)),
            Some(observed) => anyhow::bail!(
                "local branch head conflict: expected {expected}, observed {observed}"
            ),
        }
    }
}

fn verify_planned_heads(
    candidate: &CleanupCandidate,
    branch: &RevalidatedBranch,
    expected_remote: &str,
) -> Result<()> {
    if candidate.local_head_sha.as_deref() != branch.local_head.as_deref() {
        anyhow::bail!("managed local branch head differs from the planned head");
    }
    if candidate
        .remote_head_sha
        .as_deref()
        .is_some_and(|planned| planned != expected_remote)
    {
        anyhow::bail!("managed remote branch head differs from the planned head");
    }
    Ok(())
}

fn worktree_exists(candidate: &CleanupCandidate) -> bool {
    candidate
        .worktree_path
        .as_ref()
        .is_some_and(|path| path_exists_sync(path))
}
