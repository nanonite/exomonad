use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::services::repo::{get_repository_identity, RepositoryIdentity};
use anyhow::{bail, Context, Result};

pub(super) struct RevalidatedBranch {
    pub(super) repository: RepositoryIdentity,
    pub(super) branch: String,
    pub(super) local_head: Option<String>,
}

impl VerifiedCleanupService {
    pub(super) async fn revalidate_branch(
        &self,
        candidate: &CleanupCandidate,
    ) -> Result<RevalidatedBranch> {
        let evidence = branch_evidence(candidate)?;
        let branch = branch_name(candidate, evidence)?;
        let identity = managed_identity(candidate)?;
        self.validate_resolver_identity(identity).await?;
        let repository = self.validate_repository(evidence).await?;
        let target = fetch_target_branch(
            &self.project_dir,
            &repository.remote_name,
            &repository.base_branch,
        )
        .await?;
        let pull_request = self
            .validate_pull_request(candidate, branch, &repository)
            .await?;
        if let Some(pull_request) = &pull_request {
            validate_merge_reachability(&self.project_dir, pull_request, &target).await?;
        }
        let local_head = self.validate_current_branch(branch, &repository).await?;
        Ok(RevalidatedBranch {
            repository,
            branch: branch.to_string(),
            local_head,
        })
    }

    async fn validate_resolver_identity(
        &self,
        expected: &crate::services::agent_resolver::AgentIdentityRecord,
    ) -> Result<()> {
        if self.resolver.get(&expected.agent_name).await.as_ref() != Some(expected) {
            bail!("managed resolver identity changed since planning");
        }
        Ok(())
    }

    async fn validate_repository(
        &self,
        evidence: &CleanupBranchEvidence,
    ) -> Result<RepositoryIdentity> {
        let repository = get_repository_identity(&self.project_dir)
            .await
            .context("re-read configured repository identity")?;
        if evidence.remote_name.as_deref() != Some(repository.remote_name.as_str()) {
            bail!("configured remote changed since planning");
        }
        if evidence.target_branch.as_deref() != Some(repository.base_branch.as_str()) {
            bail!("configured target branch changed since planning");
        }
        if let Some(remote_branch) = evidence.remote_branch.as_deref() {
            if evidence.branch.as_deref() != Some(remote_branch) {
                bail!("managed remote branch differs from the planned branch");
            }
        }
        Ok(repository)
    }

    async fn validate_pull_request(
        &self,
        candidate: &CleanupCandidate,
        branch: &str,
        repository: &RepositoryIdentity,
    ) -> Result<Option<CleanupPullRequest>> {
        let (pull_request, error) = self
            .pull_request_state(Some(branch), true, Some(repository), None)
            .await;
        match (pull_request, error) {
            (Some(pull_request), None) => {
                let expected = candidate
                    .pull_request
                    .as_ref()
                    .context("pull request appeared after the verified no-PR observation")?;
                if pull_request != *expected {
                    bail!("authoritative pull-request evidence changed since planning");
                }
                Ok(Some(pull_request))
            }
            (None, None) if candidate.allow_no_pr && candidate.pull_request.is_none() => Ok(None),
            (None, Some(error)) => bail!("authoritative pull request lookup failed: {error}"),
            _ => bail!("authoritative pull-request evidence disappeared"),
        }
    }

    async fn validate_current_branch(
        &self,
        branch: &str,
        repository: &RepositoryIdentity,
    ) -> Result<Option<String>> {
        let current = checked_out_branch(&self.project_dir).await?;
        if current.is_none()
            || current.as_deref() == Some(branch)
            || is_protected_branch(branch, &repository.base_branch)
        {
            bail!("managed branch is current or protected");
        }
        local_branch_state(&self.project_dir, branch).await
    }
}

fn branch_evidence(candidate: &CleanupCandidate) -> Result<&CleanupBranchEvidence> {
    candidate
        .branch
        .as_ref()
        .context("branch evidence is unavailable")
}

fn branch_name<'a>(
    candidate: &'a CleanupCandidate,
    evidence: &'a CleanupBranchEvidence,
) -> Result<&'a str> {
    evidence
        .branch
        .as_deref()
        .or(candidate.local_branch.as_deref())
        .context("managed branch name is unavailable")
}

fn managed_identity(
    candidate: &CleanupCandidate,
) -> Result<&crate::services::agent_resolver::AgentIdentityRecord> {
    candidate
        .identity
        .as_ref()
        .context("managed identity is unavailable")
}

async fn validate_merge_reachability(
    project_dir: &std::path::Path,
    pull_request: &CleanupPullRequest,
    target: &CleanupTargetBranch,
) -> Result<()> {
    let merge_commit = pull_request
        .merge_commit_sha
        .as_deref()
        .filter(|sha| !sha.is_empty())
        .context("authoritative pull request has no merge commit")?;
    if !merge_commit_reachable(project_dir, merge_commit, target).await? {
        bail!("pull request merge commit is not reachable from fetched target branch");
    }
    Ok(())
}

pub(super) fn is_protected_branch(branch: &str, base_branch: &str) -> bool {
    branch == base_branch || matches!(branch, "main" | "master" | "trunk" | "develop")
}
