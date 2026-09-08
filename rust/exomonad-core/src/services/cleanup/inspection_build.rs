use super::inspection_branch::{LocalBranchObservation, RemoteBranchObservation};
use super::inspection_collect::{ObservedResource, PullRequestObservation};
use super::support::*;
use super::types::*;
use crate::services::repo::RepositoryIdentity;

pub(super) struct CandidateFacts {
    id: String,
    agent_dir: std::path::PathBuf,
    worktree_path: Option<std::path::PathBuf>,
    local_branch: Option<String>,
    local_head_sha: Option<String>,
    remote_branch: Option<String>,
    remote_head_sha: Option<String>,
    pull_request: Option<CleanupPullRequest>,
    liveness: CleanupLiveness,
    dirty: Option<bool>,
    protected: bool,
    identity_drift: bool,
    identity_error: Option<String>,
    head_matches_pull_request: Option<bool>,
    remote_head_matches_pull_request: Option<bool>,
    identity: Option<crate::services::agent_resolver::AgentIdentityRecord>,
    resolver_only: bool,
    recovery_receipt: bool,
    agent_name: String,
    issue: Option<String>,
    decision: CleanupDecision,
}

pub(super) struct InspectionContext<'a> {
    pub(super) repository: Option<&'a RepositoryIdentity>,
    pub(super) repository_error: Option<&'a str>,
    pub(super) current_branch: Option<&'a str>,
}

struct DecisionObservations<'a> {
    observed: &'a ObservedResource,
    local: &'a LocalBranchObservation,
    remote: &'a RemoteBranchObservation,
    pull_request: &'a PullRequestObservation,
    liveness: &'a CleanupLiveness,
    protected: bool,
}

impl CandidateFacts {
    pub(super) fn from_observations(
        observed: ObservedResource,
        local: LocalBranchObservation,
        remote: RemoteBranchObservation,
        pull_request: PullRequestObservation,
        liveness: CleanupLiveness,
        context: InspectionContext<'_>,
    ) -> Self {
        let protected = protected_branch(
            observed.worktree.branch_name.as_deref(),
            context.current_branch,
            context.repository,
        );
        let decision = decision_for(
            DecisionObservations {
                observed: &observed,
                local: &local,
                remote: &remote,
                pull_request: &pull_request,
                liveness: &liveness,
                protected,
            },
            context,
        );
        let agent_name = observed
            .identity
            .as_ref()
            .map(|identity| identity.agent_name.to_string())
            .unwrap_or_else(|| observed.id.clone());
        Self {
            id: observed.id,
            agent_dir: observed.agent_dir,
            worktree_path: observed.worktree.worktree_path,
            local_branch: local.local_branch,
            local_head_sha: local.local_head_sha,
            remote_branch: remote.branch,
            remote_head_sha: remote.head_sha,
            pull_request: pull_request.request,
            liveness,
            dirty: observed.worktree.dirty,
            protected,
            identity_drift: observed.worktree.identity_drift,
            identity_error: local.identity_error,
            head_matches_pull_request: pull_request.head_matches,
            remote_head_matches_pull_request: pull_request.remote_head_matches,
            identity: observed.identity,
            resolver_only: observed.resolver_only,
            recovery_receipt: observed.recovery_receipt,
            agent_name,
            issue: observed.issue,
            decision,
        }
    }

    pub(super) fn into_candidate(self) -> CleanupCandidate {
        CleanupCandidate {
            id: self.id,
            managed: true,
            resolver_only: self.resolver_only,
            recovery_receipt: self.recovery_receipt,
            agent_name: self.agent_name,
            issue: self.issue,
            agent_dir: self.agent_dir,
            worktree_path: self.worktree_path,
            local_branch: self.local_branch,
            local_head_sha: self.local_head_sha,
            remote_branch: self.remote_branch,
            remote_head_sha: self.remote_head_sha,
            pull_request: self.pull_request,
            liveness: self.liveness,
            dirty: self.dirty,
            protected: self.protected,
            identity_drift: self.identity_drift,
            identity_error: self.identity_error,
            head_matches_pull_request: self.head_matches_pull_request,
            remote_head_matches_pull_request: self.remote_head_matches_pull_request,
            identity: self.identity,
            decision: self.decision,
        }
    }
}

fn decision_for(
    observations: DecisionObservations<'_>,
    context: InspectionContext<'_>,
) -> CleanupDecision {
    candidate_decision(DecisionContext {
        identity: observations.observed.identity.as_ref(),
        identity_error: observations.local.identity_error.as_deref(),
        liveness: observations.liveness,
        dirty: observations.observed.worktree.dirty,
        protected: observations.protected,
        identity_drift: observations.observed.worktree.identity_drift,
        repository: context.repository,
        repository_error: context.repository_error,
        remote_error: observations.remote.error.as_deref(),
        pull_request: observations.pull_request.request.as_ref(),
        pr_error: observations.pull_request.error.as_deref(),
        head_matches_pull_request: observations.pull_request.head_matches,
        remote_head_matches_pull_request: observations.pull_request.remote_head_matches,
        resolver_only: observations.observed.resolver_only,
        recovery_receipt: observations.observed.recovery_receipt,
    })
}

fn protected_branch(
    branch: Option<&str>,
    current_branch: Option<&str>,
    repository: Option<&RepositoryIdentity>,
) -> bool {
    branch.is_some_and(|branch| {
        branch == current_branch.unwrap_or("")
            || repository.is_some_and(|repository| branch == repository.base_branch)
            || matches!(branch, "main" | "master" | "trunk" | "develop")
    })
}
