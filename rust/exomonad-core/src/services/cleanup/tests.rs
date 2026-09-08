use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::{BirthBranch, Slug};
use crate::services::agent_control::{AgentResolver, AgentType, Topology};
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::mutex_registry::MutexRegistry;
use crate::services::repo::RepositoryIdentity;
use std::path::PathBuf;
use std::sync::Arc;

fn identity(topology: Topology) -> AgentIdentityRecord {
    AgentIdentityRecord {
        agent_name: crate::domain::AgentName::try_from_str("stale-codex").unwrap(),
        slug: Slug::try_from_str("stale").unwrap(),
        agent_type: AgentType::Codex,
        birth_branch: BirthBranch::try_from_str("main.stale").unwrap(),
        parent_branch: BirthBranch::try_from_str("main").unwrap(),
        working_dir: PathBuf::from(".exo/worktrees/stale-codex"),
        display_name: "🤖 stale-codex".to_string(),
        topology,
        model: None,
        effort: None,
        ledger_owned: false,
        slice_id: Some("1069".to_string()),
    }
}

#[test]
fn cleanup_defaults_to_a_non_mutating_sweep() {
    let request = CleanupRequest::default();
    assert!(request.sweep);
    assert!(!request.apply);
}

#[test]
fn target_validation_rejects_paths_and_ambiguous_requests() {
    assert!(CleanupRequest {
        target: Some("a/b".to_string()),
        sweep: false,
        apply: false,
    }
    .validate()
    .is_err());
    assert!(CleanupRequest {
        target: Some("agent".to_string()),
        sweep: true,
        apply: false,
    }
    .validate()
    .is_err());
}

#[test]
fn candidate_decision_requires_merged_pr_and_matching_base() {
    let identity = identity(Topology::WorktreePerAgent);
    let repo = RepositoryIdentity {
        owner: crate::domain::GithubOwner::try_from_str("owner").unwrap(),
        repo: crate::domain::GithubRepo::try_from_str("repo").unwrap(),
        base_branch: "main".to_string(),
        forge_host: "forgejo.test".to_string(),
        remote_url: "https://forgejo.test/owner/repo.git".to_string(),
        remote_name: "origin".to_string(),
    };
    let pr = CleanupPullRequest {
        number: 1,
        head_ref: identity.birth_branch.to_string(),
        base_ref: "main".to_string(),
        state: "closed".to_string(),
        merged: true,
        head_sha: None,
        merge_commit_sha: Some("merge".to_string()),
    };
    assert_eq!(
        candidate_decision(DecisionContext {
            identity: &identity,
            liveness: &CleanupLiveness::Dead,
            dirty: Some(false),
            protected: false,
            identity_drift: false,
            repository: Some(&repo),
            repository_error: None,
            remote_error: None,
            pull_request: Some(&pr),
            pr_error: None,
            head_matches_pull_request: None,
        }),
        CleanupDecision::Cleanable
    );
    let mut wrong_base = pr;
    wrong_base.base_ref = "release".to_string();
    assert!(!candidate_decision(DecisionContext {
        identity: &identity,
        liveness: &CleanupLiveness::Dead,
        dirty: Some(false),
        protected: false,
        identity_drift: false,
        repository: Some(&repo),
        repository_error: None,
        remote_error: None,
        pull_request: Some(&wrong_base),
        pr_error: None,
        head_matches_pull_request: None,
    })
    .is_cleanable());
}

#[test]
fn duplicate_local_branches_are_refused() {
    let make_candidate = |id: &str| CleanupCandidate {
        id: id.to_string(),
        managed: true,
        agent_name: id.to_string(),
        issue: None,
        agent_dir: PathBuf::from(".exo/agents").join(id),
        worktree_path: Some(PathBuf::from(".exo/worktrees").join(id)),
        local_branch: Some("main.branch".to_string()),
        local_head_sha: None,
        remote_branch: None,
        remote_head_sha: None,
        pull_request: None,
        liveness: CleanupLiveness::Dead,
        dirty: Some(false),
        protected: false,
        identity_drift: false,
        head_matches_pull_request: None,
        identity: identity(Topology::WorktreePerAgent),
        decision: CleanupDecision::Cleanable,
    };
    let mut candidates = vec![make_candidate("a"), make_candidate("b")];
    refuse_duplicate_branches(&mut candidates);
    assert!(candidates
        .iter()
        .all(|candidate| !candidate.decision.is_cleanable()));
}

#[tokio::test]
async fn dry_run_does_not_remove_shared_agent_directory() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let mut record = identity(Topology::SharedDir);
    record.working_dir = PathBuf::from(".");
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let service = VerifiedCleanupService::new(
        temp.path(),
        Arc::new(AgentResolver::load(temp.path().to_path_buf()).await),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
    );
    let receipt = service.run(&CleanupRequest::default()).await.unwrap();
    assert!(receipt.dry_run);
    assert!(agent_dir.exists());
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::WouldClean);
}

#[tokio::test]
async fn apply_is_idempotent_for_shared_agent_directory() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let mut record = identity(Topology::SharedDir);
    record.working_dir = PathBuf::from(".");
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let service = VerifiedCleanupService::new(
        temp.path(),
        Arc::new(AgentResolver::load(temp.path().to_path_buf()).await),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
    );
    let request = CleanupRequest {
        apply: true,
        ..CleanupRequest::default()
    };
    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(!agent_dir.exists());
    let second = service.run(&request).await.unwrap();
    assert!(second.entries.is_empty());
}
