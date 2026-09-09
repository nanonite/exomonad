use super::types::*;
use crate::services::agent_control::Topology;
use crate::services::repo::RepositoryIdentity;
use anyhow::Result;
use std::collections::HashMap;

#[derive(Clone)]
pub(super) struct DecisionContext<'a> {
    pub(super) identity: Option<&'a crate::services::agent_resolver::AgentIdentityRecord>,
    pub(super) identity_error: Option<&'a str>,
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
    pub(super) remote_head_matches_pull_request: Option<bool>,
    pub(super) merge_commit_reachable: Option<Result<bool, String>>,
    pub(super) target_error: Option<&'a str>,
    pub(super) resolver_only: bool,
    pub(super) recovery_receipt: bool,
    pub(super) allow_no_pr: bool,
    pub(super) discard_dirty: bool,
}

pub(super) fn candidate_decision(context: DecisionContext<'_>) -> CleanupDecision {
    if let Some(decision) = reject_identity(&context) {
        return decision;
    }
    if let Some(decision) = reject_liveness(&context) {
        return decision;
    }
    if let Some(decision) = reject_candidate_state(&context) {
        return decision;
    }
    if context.resolver_only {
        return CleanupDecision::Cleanable;
    }
    pull_request_decision(&context)
}

fn reject_identity(context: &DecisionContext<'_>) -> Option<CleanupDecision> {
    if let Some(error) = context.identity_error {
        return Some(CleanupDecision::refusal(error));
    }
    let identity = context.identity?;
    if identity.ledger_owned {
        return Some(CleanupDecision::refusal(
            "ledger-owned agent requires controller reconciliation",
        ));
    }
    if identity.topology == Topology::Unspecified {
        return Some(CleanupDecision::refusal("agent topology is unspecified"));
    }
    context
        .identity_drift
        .then(|| CleanupDecision::refusal("agent identity or worktree path drifted"))
}

fn reject_liveness(context: &DecisionContext<'_>) -> Option<CleanupDecision> {
    match context.liveness {
        CleanupLiveness::Live => Some(CleanupDecision::refusal("agent is live")),
        CleanupLiveness::Unknown => Some(CleanupDecision::refusal("agent liveness is unknown")),
        CleanupLiveness::Dead => None,
    }
}

fn reject_candidate_state(context: &DecisionContext<'_>) -> Option<CleanupDecision> {
    match context.dirty {
        Some(false) => {}
        Some(true) if context.discard_dirty => {}
        Some(true) => {
            return Some(CleanupDecision::refusal(
                "worktree is dirty; discard_dirty authorization is required",
            ))
        }
        None => return Some(CleanupDecision::refusal("worktree is dirty or unavailable")),
    }
    if context.protected {
        return Some(CleanupDecision::refusal(
            "branch is protected or is the current/base branch",
        ));
    }
    if context.resolver_only && !context.recovery_receipt {
        return Some(CleanupDecision::refusal(
            "resolver-only cleanup lacks a matching in-progress receipt",
        ));
    }
    None
}

fn pull_request_decision(context: &DecisionContext<'_>) -> CleanupDecision {
    let Some(identity) = context.identity else {
        return CleanupDecision::refusal("managed identity is missing or malformed");
    };
    if identity.topology != Topology::WorktreePerAgent {
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
        if context.allow_no_pr {
            return CleanupDecision::Cleanable;
        }
        return CleanupDecision::refusal("pull-request ownership is not verified");
    };
    let Some(repository) = context.repository else {
        return CleanupDecision::refusal("repository identity is unavailable");
    };
    pull_request_safety_decision(context, identity, repository, pr)
}

fn pull_request_safety_decision(
    context: &DecisionContext<'_>,
    identity: &crate::services::agent_resolver::AgentIdentityRecord,
    repository: &RepositoryIdentity,
    pr: &CleanupPullRequest,
) -> CleanupDecision {
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
    if pr.base_ref != repository.base_branch {
        return CleanupDecision::refusal("pull request targets a different base branch");
    }
    if pr.head_ref != identity.birth_branch.as_str() {
        return CleanupDecision::refusal("pull request head does not match the managed branch");
    }
    if pr.head_sha.as_deref().is_none_or(str::is_empty) {
        return CleanupDecision::refusal("pull request head SHA is unavailable");
    }
    if let Some(error) = context.target_error {
        return CleanupDecision::refusal(format!(
            "configured target branch is unavailable: {error}"
        ));
    }
    match &context.merge_commit_reachable {
        Some(Ok(true)) => {}
        Some(Ok(false)) => {
            return CleanupDecision::refusal(
                "pull request merge commit is not reachable from the fetched target branch",
            )
        }
        Some(Err(error)) => {
            return CleanupDecision::refusal(format!(
                "pull request merge reachability is unverifiable: {error}"
            ))
        }
        None => {
            return CleanupDecision::refusal(
                "pull request merge commit reachability is unavailable",
            )
        }
    }
    reject_head_mismatch(context).unwrap_or(CleanupDecision::Cleanable)
}

fn reject_head_mismatch(context: &DecisionContext<'_>) -> Option<CleanupDecision> {
    if context.head_matches_pull_request == Some(false) {
        return Some(CleanupDecision::refusal(
            "managed branch head differs from the pull-request head",
        ));
    }
    (context.remote_head_matches_pull_request == Some(false)).then(|| {
        CleanupDecision::refusal("configured remote branch head differs from the pull-request head")
    })
}

pub(super) fn classify_routing_target(result: Result<bool>) -> CleanupLiveness {
    match result {
        Ok(true) => CleanupLiveness::Live,
        Ok(false) => CleanupLiveness::Dead,
        Err(_) => CleanupLiveness::Unknown,
    }
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
