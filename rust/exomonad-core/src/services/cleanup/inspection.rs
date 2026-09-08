use super::discovery::DiscoveredResource;
use super::inspection_observation::WorktreeObservation;
use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::services::repo::RepositoryIdentity;

impl VerifiedCleanupService {
    pub(super) async fn inspect_candidate(
        &self,
        resource: DiscoveredResource,
        repository: Option<&RepositoryIdentity>,
        repository_error: Option<&str>,
        current_branch: Option<&str>,
    ) -> CleanupCandidate {
        let DiscoveredResource {
            id,
            agent_dir,
            worktree_path: discovered_worktree,
            identity,
            mut identity_error,
            resolver_only,
            recovery_receipt,
        } = resource;
        let WorktreeObservation {
            worktree_path,
            branch_name,
            identity_drift: worktree_drift,
            dirty,
        } = self
            .observe_worktree(
                &id,
                discovered_worktree,
                identity.as_ref(),
                identity_error.as_deref(),
            )
            .await;
        let mut local_branch = None;
        let mut local_head_sha = None;
        if !resolver_only {
            if let Some(branch) = branch_name.as_deref() {
                match local_branch_state(&self.project_dir, branch).await {
                    Ok(Some(sha)) => {
                        local_branch = Some(branch.to_string());
                        local_head_sha = Some(sha);
                    }
                    Ok(None) => {
                        identity_error = Some("managed local branch is unavailable".to_string());
                    }
                    Err(error) => {
                        identity_error = Some(format!("read local branch: {error}"));
                    }
                }
            }
        }
        let (remote_branch, remote_head_sha, remote_error) = if !resolver_only {
            self.inspect_remote_branch(repository, branch_name.as_deref())
                .await
        } else {
            (None, None, None)
        };
        let (pull_request, pr_error) = self
            .pull_request_state(
                branch_name.as_deref(),
                worktree_path.is_some() && !resolver_only,
                repository,
                repository_error,
            )
            .await;
        let head_matches_pull_request = match (&local_head_sha, &pull_request) {
            (Some(local), Some(pr)) => Some(pr.head_sha.as_ref() == Some(local)),
            _ => None,
        };
        let remote_head_matches_pull_request = match (&remote_head_sha, &pull_request) {
            (Some(remote), Some(pr)) => Some(pr.head_sha.as_ref() == Some(remote)),
            _ => None,
        };
        let protected = branch_name.as_deref().is_some_and(|branch| {
            branch == current_branch.unwrap_or("")
                || repository.is_some_and(|repo| branch == repo.base_branch)
                || matches!(branch, "main" | "master" | "trunk" | "develop")
        });
        let liveness = if resolver_only
            && recovery_receipt
            && !agent_dir.exists()
            && worktree_path.as_ref().is_none_or(|path| !path.exists())
        {
            CleanupLiveness::Dead
        } else {
            self.liveness(&agent_dir).await
        };
        let decision = candidate_decision(DecisionContext {
            identity: identity.as_ref(),
            identity_error: identity_error.as_deref(),
            liveness: &liveness,
            dirty,
            protected,
            identity_drift: worktree_drift,
            repository,
            repository_error,
            remote_error: remote_error.as_deref(),
            pull_request: pull_request.as_ref(),
            pr_error: pr_error.as_deref(),
            head_matches_pull_request,
            remote_head_matches_pull_request,
            resolver_only,
            recovery_receipt,
        });
        let agent_name = identity
            .as_ref()
            .map(|identity| identity.agent_name.to_string())
            .unwrap_or_else(|| id.clone());
        CleanupCandidate {
            id,
            managed: true,
            resolver_only,
            recovery_receipt,
            agent_name,
            issue: read_active_issue(&agent_dir).await,
            agent_dir,
            worktree_path,
            local_branch,
            local_head_sha,
            remote_branch,
            remote_head_sha,
            pull_request,
            liveness,
            dirty,
            protected,
            identity_drift: worktree_drift,
            identity_error,
            head_matches_pull_request,
            remote_head_matches_pull_request,
            identity,
            decision,
        }
    }

    async fn inspect_remote_branch(
        &self,
        repository: Option<&RepositoryIdentity>,
        branch: Option<&str>,
    ) -> (Option<String>, Option<String>, Option<String>) {
        let (Some(repository), Some(branch)) = (repository, branch) else {
            return (None, None, None);
        };
        match remote_branch_state(&self.project_dir, &repository.remote_name, branch).await {
            Ok(Some(sha)) => (Some(branch.to_string()), Some(sha), None),
            Ok(None) => (None, None, None),
            Err(error) => (None, None, Some(error.to_string())),
        }
    }
}
