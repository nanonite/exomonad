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
    dirty_evidence: Option<CleanupDirtyEvidence>,
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
    branch: Option<CleanupBranchEvidence>,
    delete_remote_branch: bool,
    allow_no_pr: bool,
    discard_dirty: bool,
}

#[derive(Clone, Copy)]
pub(super) struct InspectionContext<'a> {
    pub(super) repository: Option<&'a RepositoryIdentity>,
    pub(super) repository_error: Option<&'a str>,
    pub(super) current_branch: Option<&'a str>,
    pub(super) fetched_target: Option<&'a CleanupTargetBranch>,
    pub(super) target_error: Option<&'a str>,
    pub(super) delete_remote_branch: bool,
    pub(super) allow_no_pr: bool,
    pub(super) discard_dirty: bool,
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
        let branch = branch_evidence(&local, &remote, &pull_request, context, &decision);
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
            dirty_evidence: observed.worktree.dirty_evidence.clone(),
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
            branch,
            delete_remote_branch: context.delete_remote_branch,
            allow_no_pr: context.allow_no_pr,
            discard_dirty: context.discard_dirty,
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
            branch: self.branch,
            delete_remote_branch: self.delete_remote_branch,
            dirty_evidence: self.dirty_evidence,
            allow_no_pr: self.allow_no_pr,
            discard_dirty: self.discard_dirty,
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
        merge_commit_reachable: observations.pull_request.merge_commit_reachable.clone(),
        target_error: context.target_error,
        resolver_only: observations.observed.resolver_only,
        recovery_receipt: observations.observed.recovery_receipt,
        allow_no_pr: context.allow_no_pr,
        discard_dirty: context.discard_dirty,
    })
}

fn branch_evidence(
    local: &LocalBranchObservation,
    remote: &RemoteBranchObservation,
    pull_request: &PullRequestObservation,
    context: InspectionContext<'_>,
    decision: &CleanupDecision,
) -> Option<CleanupBranchEvidence> {
    let branch = local.local_branch.clone().or_else(|| {
        pull_request
            .request
            .as_ref()
            .map(|pull_request| pull_request.head_ref.clone())
    })?;
    let local_action = if local.local_branch.is_some() {
        CleanupBranchAction {
            status: CleanupBranchActionStatus::WouldDelete,
            reason: None,
        }
    } else {
        CleanupBranchAction {
            status: CleanupBranchActionStatus::AlreadyAbsent,
            reason: Some("managed local branch is already absent".to_string()),
        }
    };
    let remote_action = if !context.delete_remote_branch {
        CleanupBranchAction {
            status: CleanupBranchActionStatus::NotRequested,
            reason: Some("remote branch deletion was not requested".to_string()),
        }
    } else if let Some(error) = &remote.error {
        CleanupBranchAction {
            status: CleanupBranchActionStatus::Refused,
            reason: Some(error.clone()),
        }
    } else if remote.head_sha.is_some() {
        CleanupBranchAction {
            status: CleanupBranchActionStatus::WouldDelete,
            reason: None,
        }
    } else {
        CleanupBranchAction {
            status: CleanupBranchActionStatus::AlreadyAbsent,
            reason: Some("managed remote branch is already absent".to_string()),
        }
    };
    let mut evidence = CleanupBranchEvidence {
        branch: Some(branch),
        local_head_sha: local.local_head_sha.clone(),
        remote_name: context
            .fetched_target
            .map(|target| target.remote_name.clone()),
        remote_branch: remote.branch.clone(),
        remote_head_sha: remote.head_sha.clone(),
        target_branch: context.fetched_target.map(|target| target.branch.clone()),
        target_head_sha: context.fetched_target.map(|target| target.head_sha.clone()),
        merge_commit_reachable: pull_request
            .merge_commit_reachable
            .as_ref()
            .and_then(|result| result.as_ref().ok().copied()),
        local: local_action,
        remote: remote_action,
    };
    if let Some(reason) = decision.reason() {
        evidence.local = CleanupBranchAction {
            status: CleanupBranchActionStatus::Refused,
            reason: Some(reason.to_string()),
        };
        if context.delete_remote_branch {
            evidence.remote = CleanupBranchAction {
                status: CleanupBranchActionStatus::Refused,
                reason: Some(reason.to_string()),
            };
        }
    }
    Some(evidence)
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
