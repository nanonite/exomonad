use super::discovery::DiscoveredResource;
use super::inspection_branch::{LocalBranchObservation, RemoteBranchObservation};
use super::inspection_build::{CandidateFacts, InspectionContext};
use super::inspection_observation::WorktreeObservation;
use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::repo::RepositoryIdentity;
use std::path::{Path, PathBuf};

pub(super) struct ObservedResource {
    pub(super) id: String,
    pub(super) agent_dir: PathBuf,
    pub(super) identity: Option<AgentIdentityRecord>,
    pub(super) identity_error: Option<String>,
    pub(super) resolver_only: bool,
    pub(super) recovery_receipt: bool,
    pub(super) recovered_provenance: Option<CleanupRecoveredProvenance>,
    pub(super) worktree: WorktreeObservation,
    pub(super) issue: Option<String>,
}

pub(super) struct PullRequestObservation {
    pub(super) request: Option<CleanupPullRequest>,
    pub(super) error: Option<String>,
    pub(super) head_matches: Option<bool>,
    pub(super) remote_head_matches: Option<bool>,
    pub(super) merge_commit_reachable: Option<Result<bool, String>>,
}

impl VerifiedCleanupService {
    pub(super) async fn inspect_candidate_facts(
        &self,
        resource: DiscoveredResource,
        context: InspectionContext<'_>,
    ) -> CandidateFacts {
        let repository = context.repository;
        let observed = self.observe_resource(resource).await;
        let (local, remote) = self
            .inspect_branch_observations(&observed, repository)
            .await;
        let pull_request = self
            .inspect_pull_request(&observed, context, &local, &remote)
            .await;
        let liveness = self
            .candidate_liveness(
                observed.resolver_only,
                observed.recovery_receipt,
                &observed.agent_dir,
                observed.worktree.worktree_path.as_ref(),
                observed.identity.as_ref(),
                observed.recovered_provenance.is_some(),
            )
            .await;
        CandidateFacts::from_observations(observed, local, remote, pull_request, liveness, context)
    }

    async fn inspect_branch_observations(
        &self,
        observed: &ObservedResource,
        repository: Option<&RepositoryIdentity>,
    ) -> (LocalBranchObservation, RemoteBranchObservation) {
        let local = self
            .inspect_local_branch(
                observed.resolver_only,
                observed.worktree.branch_name.as_deref(),
                observed.identity_error(),
            )
            .await;
        let remote = self
            .inspect_remote_branch(
                observed.resolver_only,
                repository,
                observed.worktree.branch_name.as_deref(),
            )
            .await;
        (local, remote)
    }

    async fn observe_resource(&self, resource: DiscoveredResource) -> ObservedResource {
        let DiscoveredResource {
            id,
            agent_dir,
            worktree_path,
            identity,
            identity_error,
            resolver_only,
            recovery_receipt,
            recovered_provenance,
        } = resource;
        let worktree = self
            .observe_worktree(
                &id,
                worktree_path,
                identity.as_ref(),
                identity_error.as_deref(),
                recovered_provenance.as_ref(),
            )
            .await;
        let issue = read_active_issue(&agent_dir).await;
        ObservedResource {
            id,
            agent_dir,
            identity,
            identity_error,
            resolver_only,
            recovery_receipt,
            recovered_provenance,
            worktree,
            issue,
        }
    }

    async fn inspect_pull_request(
        &self,
        observed: &ObservedResource,
        context: InspectionContext<'_>,
        local: &LocalBranchObservation,
        remote: &RemoteBranchObservation,
    ) -> PullRequestObservation {
        let repository = context.repository;
        let repository_error = context.repository_error;
        let fetched_target = context.fetched_target;
        let target_error = context.target_error;
        let (request, error) = self
            .pull_request_state(
                observed.worktree.branch_name.as_deref(),
                observed.worktree.worktree_path.is_some() && !observed.resolver_only,
                repository,
                repository_error,
            )
            .await;
        let (head_matches, remote_head_matches) =
            pull_request_head_matches(&local.local_head_sha, &remote.head_sha, &request);
        let merge_commit_reachable = self
            .inspect_merge_reachability(request.as_ref(), fetched_target, target_error)
            .await;
        PullRequestObservation {
            request,
            error,
            head_matches,
            remote_head_matches,
            merge_commit_reachable,
        }
    }

    async fn inspect_merge_reachability(
        &self,
        pull_request: Option<&CleanupPullRequest>,
        fetched_target: Option<&CleanupTargetBranch>,
        target_error: Option<&str>,
    ) -> Option<Result<bool, String>> {
        let pull_request = pull_request?;
        let merge_commit = pull_request.merge_commit_sha.as_deref()?;
        let target = fetched_target?;
        Some(
            merge_commit_reachable(&self.project_dir, merge_commit, target)
                .await
                .map_err(|error| target_error.map_or_else(|| error.to_string(), ToOwned::to_owned)),
        )
    }

    async fn candidate_liveness(
        &self,
        resolver_only: bool,
        recovery_receipt: bool,
        agent_dir: &Path,
        worktree_path: Option<&PathBuf>,
        identity: Option<&AgentIdentityRecord>,
        recovered_provenance: bool,
    ) -> CleanupLiveness {
        if recovered_provenance {
            let Some(identity) = identity else {
                return CleanupLiveness::Unknown;
            };
            return self.recovered_liveness(identity).await;
        }
        if resolver_only
            && recovery_receipt
            && !agent_dir.exists()
            && worktree_path.is_none_or(|path| !path.exists())
        {
            CleanupLiveness::Dead
        } else {
            self.liveness(agent_dir).await
        }
    }
}

impl ObservedResource {
    fn identity_error(&self) -> Option<String> {
        self.identity_error.clone()
    }
}

fn pull_request_head_matches(
    local_head_sha: &Option<String>,
    remote_head_sha: &Option<String>,
    pull_request: &Option<CleanupPullRequest>,
) -> (Option<bool>, Option<bool>) {
    let local_matches = match (local_head_sha, pull_request) {
        (Some(local), Some(pr)) => Some(pr.head_sha.as_ref() == Some(local)),
        _ => None,
    };
    let remote_matches = match (remote_head_sha, pull_request) {
        (Some(remote), Some(pr)) => Some(pr.head_sha.as_ref() == Some(remote)),
        _ => None,
    };
    (local_matches, remote_matches)
}
