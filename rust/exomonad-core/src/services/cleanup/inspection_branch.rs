use super::service::VerifiedCleanupService;
use super::support::*;
use crate::services::repo::RepositoryIdentity;

pub(super) struct LocalBranchObservation {
    pub(super) identity_error: Option<String>,
    pub(super) local_branch: Option<String>,
    pub(super) local_head_sha: Option<String>,
}

#[derive(Default)]
pub(super) struct RemoteBranchObservation {
    pub(super) branch: Option<String>,
    pub(super) head_sha: Option<String>,
    pub(super) error: Option<String>,
}

impl VerifiedCleanupService {
    pub(super) async fn inspect_local_branch(
        &self,
        resolver_only: bool,
        branch: Option<&str>,
        identity_error: Option<String>,
    ) -> LocalBranchObservation {
        if resolver_only {
            return LocalBranchObservation::without_branch(identity_error);
        }
        let Some(branch) = branch else {
            return LocalBranchObservation::without_branch(identity_error);
        };
        match local_branch_state(&self.project_dir, branch).await {
            Ok(Some(sha)) => LocalBranchObservation {
                identity_error,
                local_branch: Some(branch.to_string()),
                local_head_sha: Some(sha),
            },
            Ok(None) => LocalBranchObservation {
                identity_error,
                ..LocalBranchObservation::without_branch(None)
            },
            Err(error) => LocalBranchObservation {
                identity_error: Some(format!("read local branch: {error}")),
                ..LocalBranchObservation::without_branch(None)
            },
        }
    }

    pub(super) async fn inspect_remote_branch(
        &self,
        resolver_only: bool,
        repository: Option<&RepositoryIdentity>,
        branch: Option<&str>,
    ) -> RemoteBranchObservation {
        if resolver_only {
            return RemoteBranchObservation::default();
        }
        let (Some(repository), Some(branch)) = (repository, branch) else {
            return RemoteBranchObservation::default();
        };
        match remote_branch_state(&self.project_dir, &repository.remote_name, branch).await {
            Ok(head_sha) => RemoteBranchObservation {
                branch: head_sha.as_ref().map(|_| branch.to_string()),
                head_sha,
                error: None,
            },
            Err(error) => RemoteBranchObservation {
                error: Some(error.to_string()),
                ..RemoteBranchObservation::default()
            },
        }
    }
}

impl LocalBranchObservation {
    fn without_branch(identity_error: Option<String>) -> Self {
        Self {
            identity_error,
            local_branch: None,
            local_head_sha: None,
        }
    }
}
